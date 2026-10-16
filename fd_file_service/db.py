"""元数据模型（变更任务 1.2）：两张表，而非一张。

为什么是两张表
--------------
任务书给的单表 ``file_assets`` 同时要求两件互斥的事：

- ``(owner, sha256)`` 唯一（同域同内容只一条字节记录）
- 去重后「一个字节对象 + 多条文件记录」——即同内容上传 N 次要返回 N 个
  file_id，每个 file_id 可独立删除

这两条在一张表里无法共存：唯一约束会拒掉第二个 file_id，去掉约束则
「只一条字节记录」没有强制力（只能靠应用代码自觉，而应用代码有并发）。

所以按**职责**拆成两张表：

- ``file_objects``：**字节对象**（去重单位）。``(owner, sha256)`` 唯一的
  数据库约束正落在这里——「同域同内容只有一份字节」由数据库保证。
  ``ref_count`` 是引用它的**存活**文件记录数。
- ``file_assets``：**文件记录**（交付单位）。每次上传一条，``file_id`` 唯一；
  经 ``object_id`` 指向字节对象。删一条文件记录 = 打 ``deleted_at``，
  只有最后一条引用被清除（保留期满后）才真正删字节。

这样：唯一约束在字节层、多条记录在文件层，两个不变量各自有强制力；
配额按 ``file_objects.size`` 求和即为「去重后字节」，无需 DISTINCT。
代价是列表需一次 join（SQLite 上万级行无感）。

状态机（file_assets）
--------------------
    pending   confirmed_at IS NULL                  → 不计配额、不出现在列表
    live      confirmed_at NOT NULL, deleted_at NULL → 计入配额、出现在列表
    deleted   deleted_at NOT NULL, purged_at NULL    → 隐藏、保留期内可恢复
    purged    purged_at NOT NULL                     → 记录留痕，字节已释放

时间戳一律 **naive UTC**（SQLite 无时区类型），比较函数见 ``utcnow()``。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy import (
    event as sa_event,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """naive UTC 时间戳——SQLite DateTime 比较的统一基准。"""
    return datetime.now(UTC).replace(tzinfo=None)


class FileObject(Base):
    """字节对象：一个 owner 域内的一份去重字节。"""

    __tablename__ = "file_objects"
    __table_args__ = (
        # 「同域同内容只有一份字节」——数据库强制，不靠应用自觉。
        UniqueConstraint("owner", "sha256", name="uq_file_objects_owner_sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(String, index=True, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 桶内键：``<owner_hash>/<file_id>``（首个引用它的文件的 id）。
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    #: 存活引用数；降到 0 才允许删字节。
    ref_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    #: 字节已从存储删除；非空即不占配额。
    purged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FileAsset(Base):
    """文件记录：一次上传一条，独立可删。"""

    __tablename__ = "file_assets"
    __table_args__ = (UniqueConstraint("file_id", name="uq_file_assets_file_id"),)

    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String, index=True, nullable=False)
    #: 未确认（pending）时为空——此时字节可能已在桶里，但不计入任何账。
    object_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("file_objects.id"), nullable=True, index=True
    )
    # 下列字段在记录上冗余一份：列表/响应不必 join 即可给出 sha256/size/mime，
    # 且 pending 期（尚无 object）也要能给出键——凭证签发时就已确定，不会漂移。
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: deleted_at + FILE_SOFT_DELETE_DAYS；到此才真正释放字节。
    purge_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 已硬删除（记录留痕，字节已释放）。
    purged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def status(self) -> str:
        if self.purged_at is not None:
            return "purged"
        if self.deleted_at is not None:
            return "deleted"
        if self.confirmed_at is None:
            return "pending"
        return "live"


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine(url: str | None = None, **kwargs: Any):
    """建/取 engine。SQLite 需 ``check_same_thread=False``（uvicorn 多线程）。"""
    global _engine, _SessionLocal
    from .config import settings

    target = url or settings.file_database_url
    if _engine is not None and str(_engine.url) == target and not kwargs:
        return _engine
    connect_args = {"check_same_thread": False} if target.startswith("sqlite") else {}
    _engine = create_engine(target, connect_args=connect_args, future=True)
    if target.startswith("sqlite"):
        # 元数据库此后由**两个进程**共写（HTTP 服务 + parse_worker）：
        # WAL 让读不阻塞写；busy_timeout 让偶发写写竞争等锁而非立刻报
        # "database is locked"。事件表在 events.py 侧独立设过 busy_timeout，
        # 这里补的是元数据表（file_assets/file_objects）这条路径。
        sa_event.listens_for(_engine, "connect")(
            lambda dbapi_conn, _: (
                dbapi_conn.execute("PRAGMA journal_mode=WAL"),
                dbapi_conn.execute("PRAGMA busy_timeout=5000"),
            )
        )
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db(url: str | None = None) -> None:
    """建表（幂等）。"""
    Base.metadata.create_all(get_engine(url))


def SessionLocal() -> Session:
    """取一个新 Session；未 init 过则按配置惰性建库。"""
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()
