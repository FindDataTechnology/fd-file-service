"""文本抽取编排（任务 5.2 / 5.3）：事件 → 文本产物，失败留痕且不坏文件。

编排只做四件事：**取字节 → 抽文本 → 存产物 → 处置结果**。它不知道具体格式怎么
解析（``parsers/`` 管），也不知道事件从哪来（``events.py`` 管）。

## 产物存哪里：**同一个桶**，键 ``<owner_hash>/<file_id>.txt``

候选与取舍：

- **SQLite 表**：元数据已在 SQLite（design D5），产物放进同一个文件看起来顺手。
  但文本产物是**几 KB 到几 MB** 量级，比元数据行大 2~3 个数量级；塞进去会让
  「文件即备份」的优点（拷一个文件就完事）退化——数据库文件膨胀、每次备份都在
  重复复制此前抽过的全部文本，且 SQLite 单写者限制会让「存产物」与「元数据写入」
  互相排队。❌ 不合适。
- **本地磁盘**：零依赖，但解析组件在设计上跑在 ``存储主机``（D10），
  服务在云侧——**两者不是同一台机**。写本地盘等于产物散在两台机器上，检索方
  拿不到，也违反「存储抽象」（D11：一律经 S3 接口，换存储只改端点）。❌
- **同一个 S3 桶（选它）**：字节与产物同处一个存储面，取产物就是一次预签名 GET，
  与取原文件完全同构（可复用 ``Storage.presign_get``）；生命周期、备份、权限
  模型只有一套；且键前缀 ``<owner_hash>/`` 与 design D3 的 owner 前缀一致——
  按前缀配生命周期规则（如 90 天后自动清产物）是对象存储原生能力，不用自己写
  清理任务。代价：产物是**可重建**数据，多占一份空间——但它随时可重跑生成，
  不像原文件那样不可再生；且它是检索管线的直接输入，放对象存储最省事。

产物键为什么是 ``<file_id>.txt`` 而不是 ``<sha256>.txt``：file_id 是**交付单位**
（一份字节可被多个 file_id 引用），用它做键不会因去重而把不同来源的记录合并；
也不会因为某一引用的计数变动，被别的文件的删除误伤。

## 「失败留痕」的四条不变量（任务 5.3）

1. 解析失败 **不影响原文件**：产物写在同一桶但**不同键**——任何解析路径都
   **不会写/删原对象的键**（``delete_parsed`` 之外无任何 delete 调用）。
2. 失败必须有**可查记录**：写一条失败留痕（``file_parses`` 表 + 日志），
   含失败阶段与错误摘要；同时 nack 事件，使其在重试预算内自动重排队。
3. 失败不留半成品：产物写入放在**抽取成功之后**，只有拿到非空文本才 PUT。
4. **空文本不等于失败**：影印型 PDF 抽不出文本是 v1 的预期行为（OCR 属 v2）。
   抽到空串会被记为 ``empty`` 而非 ``failed`` ——否则扫描件会误进死信、反复重试
   直到耗尽预算，把「还没做的功能」伪装成「系统故障」。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

# --------------------------------------------------------------------------- #
# 环境变量（本模块自有）
# --------------------------------------------------------------------------- #

ENV_ENABLED = "FILE_PARSE_ENABLED"
ENV_DB_URL = "FILE_PARSE_DB_URL"
ENV_MAX_BYTES = "FILE_PARSE_MAX_BYTES"
ENV_TEXT_PREFIX = "FILE_PARSE_TEXT_PREFIX"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def enabled() -> bool:
    """解析是否启用（``FILE_PARSE_ENABLED``）。默认开——关掉等于停掉整条管线。"""
    return _env_bool(ENV_ENABLED, True)


def max_parse_bytes() -> int:
    """超过此体积直接跳过（~50MB），避免单文件拖垮 worker。"""
    return _env_int(ENV_MAX_BYTES, 50 * 1024 * 1024)


def _parse_db_url() -> str:
    explicit = os.environ.get(ENV_DB_URL, "").strip()
    if explicit:
        return explicit
    from .config import settings

    return str(settings.file_database_url)


# --------------------------------------------------------------------------- #
# 留痕表
# --------------------------------------------------------------------------- #


class ParseRecord(Base):
    """每次解析尝试一条（成功与失败都留痕，任务 5.3「失败留痕可查」）。

    **一张表记录成败两边**，而不是只建失败表：``status in ('failed','empty')``
    就是失败面。理由——成功后也需要知道「用的哪个后端、产物键是什么、抽了多少字」，
    否则排障时只能对着空产物键猜。统一留痕也让「重跑覆盖」有据可循。
    """

    __tablename__ = "file_parses"
    __table_args__ = (
        # 排障主查询：某文件最近几次尝试。
        Index("ix_file_parses_file_id", "file_id"),
        Index("ix_file_parses_owner_status", "owner", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    #: gotenberg | pypdf | pdfminer | pymupdf | storage | none
    engine: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: ok | empty | failed | skipped_too_large
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 失败/流水所在阶段：download / convert / extract / upload / unsupported
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    text_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: 抽取后字数（0 表示空文本）
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


def event_ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        return utcnow()


# --------------------------------------------------------------------------- #
# 结果类型
# --------------------------------------------------------------------------- #


@dataclass
class ParseResult:
    file_id: str
    owner: str
    status: str  # ok | empty | failed | skipped_too_large
    text_key: str | None = None
    char_count: int = 0
    engine: str = ""
    stage: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        """是否成功留下可用产物（空文本不算 ok，但也不是失败）。"""
        return self.status == "ok"


STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped_too_large"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def parse_file(
    *,
    session,
    storage,
    owner: str,
    file_id: str,
    mime: str,
    size: int,
    filename: str | None = None,
    gotenberg: Any | None = None,
    storage_key: str | None = None,
) -> ParseResult:
    """对一个已确认文件跑文本抽取，返回结果（**不抛**，失败体现在返回值里）。

    :param session: SQLAlchemy Session（留痕表写入）
    :param storage: ``storage.Storage``（取字节、存产物）
    :param gotenberg: Gotenberg 客户端；``None`` = 按需用 env 构造
    :param storage_key: 事件载荷里记录的桶键。**必须传**——秒传（去重命中的
        文件）的字节挂在**原文件**的键上，按 ``<owner_hash>/<file_id>``
        现拼会永远取不到字节。缺省时才回退现拼（兼容旧调用方）。

    为什么不抛而是返回值：这是**worker 循环的主干**。异常在这里就终结，
    worker 便少了「失败是否属于预期」的信息（略过超大文件 / 空文本 ≠ 故障），
    从而把可恢复的跳过误判成重试耗尽。返回结构化结果让调用方按 status 决定
    ack 还是 nack。
    """
    started = _now_ms()
    size = int(size or 0)

    def finish(result: ParseResult) -> ParseResult:
        result.duration_ms = _now_ms() - started
        _record(session, storage=storage, result=result, size=size)
        if result.status == STATUS_FAILED:
            _log(f"FAIL {file_id} stage={result.stage} owner={owner}: {result.error}")
        else:
            _log(f"{result.status.upper()} {file_id} owner={owner} chars={result.char_count}")
        return result

    # ---- 闸门 ----
    if not enabled():
        return finish(
            ParseResult(
                file_id,
                owner,
                STATUS_FAILED,
                stage="disabled",
                error="解析未启用 (FILE_PARSE_ENABLED=0)",
            )
        )
    if max_parse_bytes() and size > max_parse_bytes():
        return finish(
            ParseResult(
                file_id,
                owner,
                STATUS_SKIPPED,
                stage="download",
                error=f"体积 {size} 超过上限 {max_parse_bytes()}",
            )
        )

    # ---- 1. 取字节 ----
    key = storage_key or f"{_owner_hash(owner)}/{file_id}"
    try:
        raw = storage.get_object(key)
    except Exception as exc:
        return finish(
            ParseResult(file_id, owner, STATUS_FAILED, stage="download", error=f"{exc!r}")
        )
    if raw is None:
        return finish(
            ParseResult(
                file_id, owner, STATUS_FAILED, stage="download", error=f"存储中找不到对象 key={key}"
            )
        )

    # ---- 2. 决定管线 ----
    from .parsers import gotenberg as gb
    from .parsers import pdf_text

    ext = _ext_of(filename, mime)
    needs_office = ext in gb.OFFICE_EXTENSIONS or _is_office_mime(mime)

    pdf_bytes: bytes
    engine_label: str
    if needs_office:
        client = gotenberg or gb.Client.from_env()
        try:
            pdf_bytes = client.to_pdf(raw, _safe_filename(filename, ext))
        except Exception as exc:
            return finish(
                ParseResult(file_id, owner, STATUS_FAILED, stage="convert", error=f"{exc!r}")
            )
        engine_label = gotenberg_engine_label(client)
    else:
        # 非 Office：假定本身就是 PDF（也接受 mime 就是 application/pdf）
        if not raw.startswith(b"%PDF"):
            return finish(
                ParseResult(
                    file_id,
                    owner,
                    STATUS_FAILED,
                    stage="convert",
                    error=f"不支持的类型 mime={mime} ext={ext}（v1 仅 PDF/Office）",
                )
            )
        pdf_bytes = raw
        engine_label = "none"

    # ---- 3. PDF → 文本 ----
    try:
        text, backend = pdf_text.extract_text(pdf_bytes)
    except pdf_text.NoExtractorAvailable as exc:
        return finish(ParseResult(file_id, owner, STATUS_FAILED, stage="extract", error=str(exc)))
    except Exception as exc:
        return finish(ParseResult(file_id, owner, STATUS_FAILED, stage="extract", error=f"{exc!r}"))

    text = pdf_text.normalize(text)
    if engine_label == "none":
        engine_label = backend

    if not text:
        # 空文本：可能是影印件（v1 无 OCR）→ 留痕但不算失败，不重试。
        return finish(
            ParseResult(
                file_id,
                owner,
                STATUS_EMPTY,
                stage="extract",
                engine=engine_label,
                error="抽取结果为空（可能为扫描件，OCR 属 v2）",
            )
        )

    # ---- 4. 存产物（只有拿到非空文本才写）----
    text_key = text_key_for(owner, file_id)
    encoded = text.encode("utf-8")
    try:
        storage.put_object(text_key, encoded, "text/plain; charset=utf-8")
    except Exception as exc:
        return finish(
            ParseResult(
                file_id, owner, STATUS_FAILED, stage="upload", engine=engine_label, error=f"{exc!r}"
            )
        )

    return finish(
        ParseResult(
            file_id,
            owner,
            STATUS_OK,
            text_key=text_key,
            char_count=len(text),
            engine=engine_label,
            stage="upload",
        )
    )


def handle_event(session, storage, event, *, gotenberg: Any | None = None) -> ParseResult:
    """消费一条 ``file.confirmed`` 事件 → 跑一遍抽取。

    字段完整性在这里**显式校验**：任务 5.1 要求消费端收到的事件字段完整，
    与其让缺字段的事件在下游以 ``KeyError``/空 mime 的方式半路炸掉，
    不如立刻报出缺了什么。
    """
    missing = event.missing_fields()
    if missing:
        return ParseResult(
            file_id=getattr(event, "file_id", "?"),
            owner=getattr(event, "owner", "?"),
            status=STATUS_FAILED,
            stage="payload",
            error=f"事件缺必填字段: {', '.join(missing)}",
        )
    return parse_file(
        session=session,
        storage=storage,
        owner=event.owner,
        file_id=event.file_id,
        mime=event.mime,
        size=getattr(event, "size", 0) or 0,
        filename=(event.extra or {}).get("filename"),
        gotenberg=gotenberg,
        storage_key=event.storage_key,
    )


# --------------------------------------------------------------------------- #
# 查询与维护
# --------------------------------------------------------------------------- #


def text_key_for(owner: str, file_id: str) -> str:
    """产物键：``<prefix><owner_hash>/<file_id>.txt``。

    用 ``file_id`` 而非 ``sha256`` 见模块 docstring。``FILE_PARSE_TEXT_PREFIX``
    可加一个公共前缀（如 ``parsed/``），便于对产物整体做生命周期规则。
    """
    prefix = os.environ.get(ENV_TEXT_PREFIX, "").strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}{_owner_hash(owner)}/{file_id}.txt"


def recent_parses(session, owner: str | None = None, limit: int = 20) -> list[ParseRecord]:
    q = select(ParseRecord).order_by(ParseRecord.id.desc()).limit(limit)
    if owner is not None:
        q = q.where(ParseRecord.owner == owner)
    return list(session.scalars(q))


def failures(session, owner: str | None = None, limit: int = 50) -> list[ParseRecord]:
    """失败留痕：任务 5.3 的排障面。"""
    q = (
        select(ParseRecord)
        .where(ParseRecord.status == STATUS_FAILED)
        .order_by(ParseRecord.id.desc())
        .limit(limit)
    )
    if owner is not None:
        q = q.where(ParseRecord.owner == owner)
    return list(session.scalars(q))


def init_parse_db() -> None:
    """建留痕表（幂等）。worker 启动时调用。"""
    from sqlalchemy import create_engine

    url = _parse_db_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, future=True)
    Base.metadata.create_all(engine, tables=[ParseRecord.__table__])


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #


def _record(session, *, storage, result: ParseResult, size: int) -> None:
    """写留痕。留痕失败**不能**掩盖原解析结果——只 log，不抛。"""
    try:
        session.add(
            ParseRecord(
                file_id=result.file_id,
                owner=result.owner,
                engine=result.engine,
                status=result.status,
                stage=result.stage,
                text_key=result.text_key,
                char_count=result.char_count,
                text_bytes=size,
                duration_ms=result.duration_ms,
                error=(result.error or "")[:2000] or None,
            )
        )
        session.commit()
    except Exception as exc:  # 留痕失败不得改变主结果
        try:
            session.rollback()
        except Exception:
            pass
        _log(f"留痕写入失败 file={result.file_id}: {exc!r}")


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _owner_hash(owner: str) -> str:
    from .storage import owner_hash

    return owner_hash(owner)


def gotenberg_engine_label(client: Any) -> str:
    try:
        base = client.base_url
    except Exception:
        base = "?"
    return f"gotenberg@{base}"


#: mime → 扩展名（用于给 LibreOffice 猜输入格式）。
_MIME_EXT = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
    "application/rtf": ".rtf",
}

_OFFICE_MIMES = frozenset(k for k in _MIME_EXT if k != "application/pdf")


def _ext_of(filename: str | None, mime: str) -> str:
    if filename and "." in filename:
        cand = "." + filename.rsplit(".", 1)[1].lower()
        if len(cand) > 1 and len(cand) <= 8:
            return cand
    return _MIME_EXT.get(mime.strip().lower(), "")


def _is_office_mime(mime: str) -> bool:
    return mime.strip().lower() in _OFFICE_MIMES


def _safe_filename(filename: str | None, ext: str) -> str:
    """给 Gotenberg 的输入文件名必须有扩展名——LibreOffice 靠它选 filter。"""
    base = (filename or "input").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not ext:
        return base
    if base.lower().endswith(ext):
        return base
    return f"{base}{ext}"


def _log(msg: str) -> None:
    print(f"[parse] {msg}", flush=True)


__all__ = [
    "STATUS_EMPTY",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "ParseRecord",
    "ParseResult",
    "enabled",
    "failures",
    "handle_event",
    "init_parse_db",
    "max_parse_bytes",
    "parse_file",
    "recent_parses",
    "text_key_for",
]
