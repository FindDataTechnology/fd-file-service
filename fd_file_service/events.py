"""文件事件总线（变更任务 5.1）：进程内发布/订阅 + 可插拔持久化后端。

为什么是「SQLite 表」而不是文件日志或纯内存队列
----------------------------------------------
候选与结论（三条硬约束：**零新中间件**、**消费端是独立进程**、**v1 单实例**）：

1. **纯内存 ``asyncio.Queue`` / ``queue.Queue``**：写完小孩正的模型最快，但它
   **跨不了进程**。本变更的解析消费端是独立脚本（``scripts/parse_worker.py``），
   要能单独起停、能在服务未运行时也处理积压事件。进程一停事件即失——上传确认后
   解析没跑完就重启，事件就永久丢了。❌ 不可用作唯一持久化手段。
2. **追加写 JSONL 文件日志**：无新依赖、肉眼可查。但它**没法安全地做「认领」**——
   多消费端（或消费端与查看者）并发时，需要另加偏移文件/锁文件；崩溃后
   「处理到一半」的事件无法区分。要可靠性就得自己重实现一个队列。❌ 简单但不可靠。
3. **SQLite 队列表（选它）**：服务已有 SQLite（design D5），**零新增基础设施**；
   - 持久：跨重启、跨进程，服务与 worker 不必同时存活；
   - **可用 SQL 做原子认领**：带租约（visibility timeout）的认领，worker 崩了
     事件在租约到期后自动回到待处理，不会丢也不会重复投递；
   - 幂等/重试：``attempts`` 字段天然支持有限次重试，且事件可被**查询**
     （验证任务 5.1「事件字段完整」、5.3「失败留痕可查」都要能查）；
   - 与元数据同一份文件，备份不用多一处。
   代价：写入受 SQLite 单写者限制——v1 单实例、事件量远低于元数据量，可接受。

因此：**结构上仍是「进程内事件总线」**（``EventBus.publish`` 同步扇出给本进程订阅者，
用于将来在同一进程里挂其他消费者），**持久化由可插拔后端负责**；默认 ``sqlite``，
测试可用 ``memory``。将来若要 Kafka/Redis流式黔队，只需实现同一个 ``Backend`` 协议
并改 ``FILE_EVENT_BACKEND``——不需要动调用方。
"""

from __future__ import annotations

import json
import os
import secrets
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    Text,
    select,
)
from sqlalchemy import (
    event as sa_event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

# --------------------------------------------------------------------------- #
# 环境变量（本模块自有；不写进 config.py，避免与并行开发者的文件冲突）
# --------------------------------------------------------------------------- #

ENV_BACKEND = "FILE_EVENT_BACKEND"  # sqlite | memory
ENV_DB_URL = "FILE_EVENT_DB_URL"  # 默认跟随服务元数据库
ENV_MAX_ATTEMPTS = "FILE_EVENT_MAX_ATTEMPTS"
ENV_LEASE_SECONDS = "FILE_EVENT_LEASE_SECONDS"


def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:  # 配错降级为默认值，不因配置问题起不来
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _event_db_url() -> str:
    """事件库 URL：默认与元数据同库（同一 SQLite 文件，不多一处备份面）。"""
    explicit = _env_str(ENV_DB_URL, "")
    if explicit:
        return explicit
    from .config import settings

    return str(settings.file_database_url)


def max_attempts() -> int:
    return _env_int(ENV_MAX_ATTEMPTS, 3)


def lease_seconds() -> int:
    return _env_int(ENV_LEASE_SECONDS, 300)


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #

EVENT_FILE_CONFIRMED = "file.confirmed"
EVENT_PARSE_FAILED = "file.parse_failed"
EVENT_PARSE_SUCCEEDED = "file.parse_succeeded"


@dataclass(frozen=True)
class FileEvent:
    """文件事件载荷。

    任务 5.1 规定的必填字段：``file_id`` / ``owner`` / ``sha256`` / ``mime`` /
    ``storage_key`` / ``event_type`` / ``ts``；``size`` 与自由 ``extra`` 为附加。
    消费端应按缺失字段报错，而不是静默补默认值——字段不完整的事件无法处理。
    """

    file_id: str
    owner: str
    sha256: str
    mime: str
    storage_key: str
    event_type: str = EVENT_FILE_CONFIRMED
    ts: str = ""
    size: int = 0
    #: 扩展位（如 filename）；保持字典而非嵌套模型，避免载荷结构随业务膨胀。
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # frozen dataclass：用 object.__setattr__ 补默认时间戳。
        if not self.ts:
            object.__setattr__(self, "ts", datetime.now(UTC).replace(microsecond=0).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileEvent:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        extra = data.get("extra") or {}
        known["extra"] = dict(extra) if isinstance(extra, dict) else {}
        return cls(**known)

    def missing_fields(self) -> list[str]:
        """缺哪些必填字段（空 = 字段完整）。供消费端与验证脚本断言。"""
        missing = [
            name
            for name in ("file_id", "owner", "sha256", "mime", "storage_key", "event_type", "ts")
            if not getattr(self, name)
        ]
        return missing


class FileEventRecord(Base):
    """持久化事件 + 队列状态。

    一张表兼任两职（事件留痕 & 待处理队列）：v1 事件量小，且「处理状态」本来就是
    事件自身的属性。若将来量涨，再按 ``status`` 拆分区/表，届时不需要改 API。
    """

    __tablename__ = "file_events"
    __table_args__ = (
        # 认领查询的固定形状：先筛状态再排序取最小 id。
        Index("ix_file_events_status_id", "status", "id"),
        Index("ix_file_events_file_id", "file_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    file_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    #: 完整载荷（JSON）——即使上游字段增删，已入库事件仍可回放。
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    # ---- 队列状态 ----
    #: pending → claimed → done / dead
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 认领者标识（一次认领一个随机串），用于「认领后取回自己的那条」而不依赖 id 竞猜。
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 租约到期时间；worker 崩溃后到期，事件自动回到可认领状态。
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_event(self) -> FileEvent:
        return FileEvent.from_dict(json.loads(self.payload_json))


# --------------------------------------------------------------------------- #
# 可插拔后端
# --------------------------------------------------------------------------- #


class Backend(ABC):
    """后端协议：只三个动作——追加、认领、结案。"""

    @abstractmethod
    def enqueue(self, event: FileEvent) -> int:
        """入队，返回事件行 id。"""

    @abstractmethod
    def claim(self) -> tuple[int, FileEvent] | None:
        """原子认领一条待处理事件；无则 ``None``。

        返回 ``(row_id, event)`` —— row_id 供 ack/nack 精确指向这一行。
        """

    @abstractmethod
    def ack(self, row_id: int) -> None:
        """处理成功 → done。"""

    @abstractmethod
    def nack(self, row_id: int, error: str) -> str:
        """处理失败：次数未超则返回 ``retry`` 立即重排队，超限返回 ``dead`` 进入死信。"""


class SQLiteBackend(Backend):
    """SQLite 后端（默认）。

    原子认领用「一次性随机数」两语句法：

    1. ``UPDATE ... WHERE id = (SELECT id ... LIMIT 1)`` 是**单条**语句，在 SQLite
       里持有写锁直至完成，两个进程不会认领到同一行；
    2. 被更新的行带上本进程独有的 ``lease_owner`` 随机数，随后按它查回自己的行——
       不必知道被更新的 id，因此无竞猜、无窗口。

    相比「SELECT 再 UPDATE」两语句法：那种写法在非 IMMEDIATE 事务下会双认领。
    这里不依赖方言特定的 ``RETURNING``，也不改全局隔离级别。
    """

    def __init__(self, url: str | None = None):
        from sqlalchemy import create_engine

        target = url or _event_db_url()
        connect_args = {"check_same_thread": False} if target.startswith("sqlite") else {}
        self.engine: Engine = create_engine(target, connect_args=connect_args, future=True)
        Base.metadata.create_all(self.engine, tables=[FileEventRecord.__table__])
        if target.startswith("sqlite"):
            # 会话级：等锁而不是立刻报错（不改全局 journal_mode，避免影响元数据库的行为）。
            sa_event.listens_for(self.engine, "connect")(
                lambda dbapi_conn, _: dbapi_conn.execute("PRAGMA busy_timeout=5000")
            )

    # ---- 内部 ----
    def _insert(self, event: FileEvent) -> int:
        from sqlalchemy.orm import Session

        with Session(self.engine, expire_on_commit=False) as s:
            rec = FileEventRecord(
                event_type=event.event_type,
                file_id=event.file_id,
                owner=event.owner,
                payload_json=json.dumps(event.to_dict(), ensure_ascii=False),
                ts=_parse_ts(event.ts),
                status="pending",
                attempts=0,
            )
            s.add(rec)
            s.commit()
            return int(rec.id)

    def enqueue(self, event: FileEvent) -> int:
        return self._insert(event)

    def claim(self) -> tuple[int, FileEvent] | None:
        from sqlalchemy import text

        nonce = secrets.token_hex(16)
        now = utcnow()
        lease_until = now + timedelta(seconds=lease_seconds())
        limit = max_attempts()

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE file_events SET status='claimed', lease_owner=:nonce, "
                    "claimed_until=:until, attempts=attempts + 1 "
                    "WHERE id = ("
                    "  SELECT id FROM file_events WHERE "
                    "    (status='pending' OR (status='claimed' AND claimed_until IS NOT NULL "
                    "     AND claimed_until < :now)) "
                    "    AND attempts < :limit "
                    "  ORDER BY id LIMIT 1)"
                ),
                {"nonce": nonce, "until": lease_until, "now": now, "limit": limit},
            )
            row = (
                conn.execute(
                    text("SELECT id, payload_json FROM file_events WHERE lease_owner=:nonce"),
                    {"nonce": nonce},
                )
                .mappings()
                .first()
            )

        if row is None:
            return None
        return int(row["id"]), FileEvent.from_dict(json.loads(row["payload_json"]))

    def ack(self, row_id: int) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE file_events SET status='done', completed_at=:now, "
                    "last_error=NULL WHERE id=:id"
                ),
                {"id": row_id, "now": utcnow()},
            )

    def nack(self, row_id: int, error: str) -> str:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            row = (
                conn.execute(text("SELECT attempts FROM file_events WHERE id=:id"), {"id": row_id})
                .mappings()
                .first()
            )
            attempts = int(row["attempts"]) if row else 0
            if attempts < max_attempts():
                conn.execute(
                    text(
                        "UPDATE file_events SET status='pending', lease_owner=NULL, "
                        "claimed_until=NULL, last_error=:err WHERE id=:id"
                    ),
                    {"id": row_id, "err": error[:2000]},
                )
                return "retry"
            conn.execute(
                text(
                    "UPDATE file_events SET status='dead', lease_owner=NULL, "
                    "claimed_until=NULL, last_error=:err WHERE id=:id"
                ),
                {"id": row_id, "err": error[:2000]},
            )
            return "dead"

    # ---- 查询（验证与排障用）----
    def recent(self, limit: int = 50) -> list[FileEventRecord]:
        from sqlalchemy.orm import Session

        with Session(self.engine, expire_on_commit=False) as s:
            return list(
                s.scalars(select(FileEventRecord).order_by(FileEventRecord.id.desc()).limit(limit))
            )

    def count_by_status(self) -> dict[str, int]:
        from sqlalchemy import func
        from sqlalchemy.orm import Session

        with Session(self.engine, expire_on_commit=False) as s:
            rows = s.execute(
                select(FileEventRecord.status, func.count()).group_by(FileEventRecord.status)
            ).all()
        return {str(status): int(n) for status, n in rows}

    def has_event_for(self, file_id: str) -> bool:
        """该 file_id 是否已有事件（**任何状态**都算已发布）。"""
        from sqlalchemy.orm import Session

        with Session(self.engine, expire_on_commit=False) as s:
            return (
                s.scalar(
                    select(FileEventRecord.id).where(FileEventRecord.file_id == file_id).limit(1)
                )
                is not None
            )


class MemoryBackend(Backend):
    """进程内后端：测试用 / 临时验证。重启即失，绝不用于生产事件。"""

    def __init__(self) -> None:
        self._rows: dict[int, FileEventRecord] = {}
        self._seq = 0

    def enqueue(self, event: FileEvent) -> int:
        self._seq += 1
        rec = FileEventRecord(
            id=self._seq,
            event_type=event.event_type,
            file_id=event.file_id,
            owner=event.owner,
            payload_json=json.dumps(event.to_dict(), ensure_ascii=False),
            ts=_parse_ts(event.ts),
            status="pending",
            attempts=0,
        )
        self._rows[self._seq] = rec
        return self._seq

    def claim(self) -> tuple[int, FileEvent] | None:
        for row_id in sorted(self._rows):
            rec = self._rows[row_id]
            if rec.status == "pending" and rec.attempts < max_attempts():
                rec.status = "claimed"
                rec.attempts += 1
                return row_id, rec.to_event()
        return None

    def ack(self, row_id: int) -> None:
        rec = self._rows.get(row_id)
        if rec:
            rec.status = "done"
            rec.completed_at = utcnow()

    def nack(self, row_id: int, error: str) -> str:
        rec = self._rows.get(row_id)
        if not rec:
            return "dead"
        rec.last_error = error[:2000]
        if rec.attempts < max_attempts():
            rec.status = "pending"
            return "retry"
        rec.status = "dead"
        return "dead"

    def has_event_for(self, file_id: str) -> bool:
        return any(r.file_id == file_id for r in self._rows.values())


# --------------------------------------------------------------------------- #
# 进程内总线
# --------------------------------------------------------------------------- #

Subscriber = Callable[[FileEvent], None]


class EventBus:
    """总线：每份事件既**持久化入队**（给独立进程的 worker），也**同步扇出**给本进程订阅者。

    扇出失败不影响入队，反之亦然——这两件事的失败域不同：入库失败必须让调用方知道
    （否则事件丢了却返回成功），订阅者失败则只记录（后续 worker 仍会从队列取出）。
    """

    def __init__(self, backend: Backend | None = None):
        self._backend = backend
        self._subscribers: list[Subscriber] = []

    @property
    def backend(self) -> Backend:
        if self._backend is None:
            self._backend = _default_backend()
        return self._backend

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    def publish(self, event: FileEvent) -> int:
        missing = event.missing_fields()
        if missing:
            raise ValueError(f"事件缺必填字段: {', '.join(missing)}")
        row_id = self.backend.enqueue(event)
        for fn in list(self._subscribers):
            try:
                fn(event)
            except Exception as exc:  # 订阅者不得拖垮发布路径
                _log(f"subscriber failed for {event.file_id}: {exc!r}")
        return row_id


_backend_singleton: Backend | None = None


def _default_backend() -> Backend:
    global _backend_singleton
    if _backend_singleton is None:
        kind = _env_str(ENV_BACKEND, "sqlite").lower()
        _backend_singleton = MemoryBackend() if kind == "memory" else SQLiteBackend()
    return _backend_singleton


def set_backend(backend: Backend | None) -> None:
    """切换/清空后端（测试用）。传 ``None`` 回到按环境变量构造的默认后端。"""
    global _backend_singleton
    _backend_singleton = backend


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def publish_file_confirmed(
    *,
    file_id: str,
    owner: str,
    sha256: str,
    mime: str,
    storage_key: str,
    size: int = 0,
    **extra: Any,
) -> int:
    """上传确认后调用的**唯一入口**（任务 5.1）。

    调用方（本仓的 upload 路由，属并行开发的另一模块）只需传这几个字段。
    返回事件行 id（可用于日志关联），失败会向上传播——宁可确认接口报错，
    也不能出现「文件已确认但事件丢了」的静默不一致。
    """
    ev = FileEvent(
        file_id=file_id,
        owner=owner,
        sha256=sha256,
        mime=mime,
        storage_key=storage_key,
        event_type=EVENT_FILE_CONFIRMED,
        size=int(size or 0),
        extra=dict(extra),
    )
    row_id = get_bus().publish(ev)
    _log(f"published {ev.event_type} row={row_id} file={file_id} owner={owner}")
    return row_id


def publish_file_confirmed_once(
    *,
    file_id: str,
    owner: str,
    sha256: str,
    mime: str,
    storage_key: str,
    size: int = 0,
    **extra: Any,
) -> int | None:
    """幂等发布（确认路径专用）：该 file_id 已有事件（任何状态）则跳过。

    为什么需要：确认接口是幂等的（重复确认直接返回既有记录），若每次都发布，
    重试会把同一文件塞进解析队列多次。而**跳过只在事件已存在时发生**——
    首次确认「入库成功但发布失败」后调用方拿到 5xx 重试，此时队列里没有该
    file_id，这条就会补发。恰好构成自愈，不构成重复。
    """
    if has_event_for(file_id):
        return None
    return publish_file_confirmed(
        file_id=file_id,
        owner=owner,
        sha256=sha256,
        mime=mime,
        storage_key=storage_key,
        size=size,
        **extra,
    )


def has_event_for(file_id: str) -> bool:
    """该 file_id 是否已有事件行（任何状态）。按当前总线后端分派。"""
    b = get_bus().backend
    if isinstance(b, SQLiteBackend):
        return b.has_event_for(file_id)
    if isinstance(b, MemoryBackend):
        return b.has_event_for(file_id)
    return False


def recent_events(limit: int = 50) -> Sequence[FileEventRecord]:
    b = get_bus().backend
    if isinstance(b, SQLiteBackend):
        return b.recent(limit)
    return []


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=None)
    except ValueError:
        return utcnow()


def _log(msg: str) -> None:
    print(f"[events] {msg}", flush=True)


__all__ = [
    "EVENT_FILE_CONFIRMED",
    "EVENT_PARSE_FAILED",
    "EVENT_PARSE_SUCCEEDED",
    "Backend",
    "EventBus",
    "FileEvent",
    "FileEventRecord",
    "MemoryBackend",
    "SQLiteBackend",
    "get_bus",
    "has_event_for",
    "lease_seconds",
    "max_attempts",
    "publish_file_confirmed",
    "publish_file_confirmed_once",
    "recent_events",
    "set_backend",
]
