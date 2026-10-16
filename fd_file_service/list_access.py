"""列表与取用路由（任务 4.1 / 4.2）：``GET /files``、``GET /files/{file_id}/access``。

为什么单独一个模块而不是塞进 ``app.py``
--------------------------------------
``app.py`` 由并行开发者负责（文件边界红线）——我不能改它。这里提供**独立
APIRouter**，宿主侧只需两行挂载::

    from fd_file_service.list_access import router
    app.include_router(router)

如果对方已经在 ``app.py`` 里写了同路径的端点也不会冲突：FastAPI **先注册的路由
先生效**。把 ``include_router`` 放在其注册之前即可接管；后挂载则被对方的占位
实现覆盖。这个顺序写在 README 里，不靠运行时猜。为避免歧义，本路由的路径
前缀用 ``/v1``：``GET /v1/files`` 与 ``GET /v1/files/{file_id}/access``，
与对方 ``/files`` 互不遮挡，**两边可同时存活**，调用方自选契约面。

## 三条硬性要求

1. **列表严格限域**：``owner`` 是必填查询参数，且作为 **WHERE 条件**而非装饰器——
   跨域不报错，只查不到（spec「跨域不可见」）。过滤与分页在 SQL 层完成，
   不做内存过滤（域内文件量可达十万级）。
2. **取用地址必须带有效期**：默认 1 小时（``FILE_ACCESS_URL_TTL_SECONDS``），
   单次上限 24 小时。**绝不返回永久公开地址**，也不返回桶的公开读 URL。
3. **跨域/不存在一律 404**，且文案与「真的不存在」完全一致——不泄漏
   「该 file_id 存在但属于别人」这一事实（防探测）。

## 与对方模块的接口约定

本模块只依赖 ``service`` 层的 ``list_assets`` / ``presign_download`` /
``FileError`` 三个入口，不碰数据模型与上传/配额逻辑。为兼容对方当前的
``list_assets`` 签名（只有 created_after/created_before + limit/offset），
总数与分页数在本地补齐，不改动对方函数。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

ENV_TTL = "FILE_ACCESS_URL_TTL_SECONDS"
ENV_MAX_TTL = "FILE_ACCESS_URL_MAX_TTL_SECONDS"

#: 默认取用地址有效期（秒）——1 小时。
DEFAULT_TTL_SECONDS = 3600
#: 单次请求允许的最长有效期（24h）。上限存在的意义是防止调用方
#: 「给我签一个十年的 URL」——那与永久公开地址没有实质区别。
MAX_TTL_SECONDS = 86400


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def max_ttl() -> int:
    return max(1, _env_int(ENV_MAX_TTL, MAX_TTL_SECONDS))


def default_ttl() -> int:
    """配置默认 TTL（``FILE_ACCESS_URL_TTL_SECONDS``），夹到 [1, max]。"""
    return max(1, min(_env_int(ENV_TTL, DEFAULT_TTL_SECONDS), max_ttl()))


def clamp_ttl(ttl: int | None) -> int:
    """把请求的 TTL 夹进 [1, max]——超出上限**静默夹到上限**而非报错。

    为什么不报错：调用方常见写法是「给我一个尽量长的地址」，上限是服务策略
    而非调用方错误。关键是**回报的 expires_in 必须与实际签名时长一致**，
    否则上层按错误的时长做缓存会提前失效。
    """
    if ttl is None:
        return default_ttl()
    return max(1, min(int(ttl), max_ttl()))


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #


class FileItem(BaseModel):
    file_id: str
    owner: str
    sha256: str
    size: int
    mime: str
    filename: str | None = None
    created_at: datetime
    confirmed_at: datetime | None = None
    #: 生命周期状态（pending/live/deleted/purged）。列表侧只可能见到 live，
    #: 保留该字段以对齐 app 早期 /files 响应的契约，调用方无需另查。
    status: str = "live"


class FileListResponse(BaseModel):
    owner: str
    count: int = Field(description="本页条数")
    total: int = Field(description="符合条件总条数")
    limit: int
    offset: int
    has_more: bool
    items: list[FileItem]


class AccessResponse(BaseModel):
    file_id: str
    owner: str
    url: str = Field(description="带有效期的预签名 GET 地址；不是永久公开地址")
    method: str = "GET"
    expires_in: int = Field(description="有效期（秒），与实际签名时长一致")
    expires_at: str = Field(description="过期时刻（ISO8601 UTC）")
    content_type: str
    size: int


router = APIRouter(tags=["files-v1"])

# 注意：本模块**不注册路由**（主契约统一在 app.py 的 /files 下，避免
# /files 与 /v1/files 两套并存造成行为歧义）。这里只导出能力：
# query_files()（分页计数 + MIME 前缀过滤）与 clamp_ttl()（TTL 上限夹取），
# 由 app.py 的正式路由调用。


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def _service():
    """惰性导入对方的服务层。

    惰性而非顶层 import：对方模块仍在改动时，本模块仍可被 import 并自检；
    真正缺件在**调用**时暴露，而不是让整个包连带起不来。
    """
    from . import service

    return service


def _parse_when(value: str | None, param: str) -> datetime | None:
    """ISO8601 → naive UTC datetime。

    非法输入报 **400 而非静默忽略**：调用方传了 ``since`` 却格式写错，期望是
    「过滤生效」；静默忽略会返回全部数据，看起来像功能坏了却无任何提示。
    """
    if value is None or value.strip() == "":
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"参数 {param} 格式非法（需 ISO8601，如 2026-01-01T00:00:00Z）: {value!r}",
        ) from exc
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _to_item(a: Any) -> FileItem:
    return FileItem(
        file_id=a.file_id,
        owner=a.owner,
        sha256=a.sha256,
        size=a.size,
        mime=a.mime,
        filename=getattr(a, "filename", None),
        created_at=a.created_at,
        confirmed_at=a.confirmed_at,
        status=getattr(a, "status", "live"),
    )


def _not_found() -> HTTPException:
    """跨域与不存在共用同一个 404——不泄漏归属信息。"""
    return HTTPException(status_code=404, detail="文件不存在")


def get_db():
    """取一个新 Session（与宿主 ``app.get_db`` 同形）。

    单独定义一份而不用对方的 import：对方改名/重构时本模块不受牵连——
    依赖方向保持在「我可以只用 db.SessionLocal」这一层。
    """
    from .db import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_storage():
    from .io_ext import new_storage

    return new_storage()


def _base_filters(owner: str, mime: str | None, mime_prefix: str | None):
    """列表的 WHERE 条件：owner 严格限定 + live（已确认未删除）。"""
    from .db import FileAsset

    conds = [
        FileAsset.owner == owner,
        FileAsset.confirmed_at.is_not(None),
        FileAsset.deleted_at.is_(None),
        FileAsset.purged_at.is_(None),
    ]
    if mime is not None:
        conds.append(FileAsset.mime == mime)
    if mime_prefix is not None:
        conds.append(FileAsset.mime.startswith(mime_prefix))
    return conds


# --------------------------------------------------------------------------- #
# 能力函数（由 app.py 的 /files 正式路由调用）
# --------------------------------------------------------------------------- #


def query_files(
    *,
    db: Session,
    owner: str,
    mime: str | None = None,
    mime_prefix: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> FileListResponse:
    """按 owner 列表（任务 4.1）：严格限域 + 类型/时间过滤 + 分页计数。

    与 app.py 早期版本的差别（保留的理由）：
      - total / has_more 来自独立 count 查询，不靠「本页条数 == limit」猜；
      - mime_prefix 支持按类型族过滤（image/、application/vnd. 等）。
    """
    from .db import FileAsset

    after = _parse_when(since, "since")
    before = _parse_when(until, "until")
    conds = _base_filters(owner, mime, mime_prefix)
    if after is not None:
        conds.append(FileAsset.created_at >= after)
    if before is not None:
        conds.append(FileAsset.created_at <= before)

    # 总数用独立的 count 查询（不受 limit/offset 影响），避免「has_more 靠猜」。
    total = int(db.scalar(select(func.count()).select_from(FileAsset).where(*conds)) or 0)
    rows = list(
        db.scalars(
            select(FileAsset)
            .where(*conds)
            .order_by(FileAsset.created_at.desc(), FileAsset.file_id)
            .limit(limit)
            .offset(offset)
        )
    )
    return FileListResponse(
        owner=owner,
        count=len(rows),
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(rows)) < total,
        items=[_to_item(a) for a in rows],
    )


def get_access(
    *,
    db: Session,
    file_id: str,
    owner: str,
    ttl: int | None = None,
    storage: Any = None,
) -> AccessResponse:
    """带有效期的取用地址（任务 4.2）——绝不返回永久公开地址。

    ttl 经 clamp_ttl() 夹到上限内（防止调用方签出近乎永久的地址）；
    owner 不匹配与「不存在」返回同一 404 文案，不泄漏存在性。
    """
    from .storage import from_settings

    svc = _service()
    effective_ttl = clamp_ttl(ttl)
    store = storage if storage is not None else from_settings()

    try:
        url = svc.presign_download(
            session=db,
            storage=store,
            owner=owner,
            file_id=file_id,
            ttl=effective_ttl,
        )
        asset = svc.get_asset(session=db, owner=owner, file_id=file_id)
    except svc.FileError as exc:
        if exc.status == 404:
            raise _not_found() from exc
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=effective_ttl)
    return AccessResponse(
        file_id=file_id,
        owner=owner,
        url=url,
        method="GET",
        expires_in=effective_ttl,
        # 服务端不持有（OAuth 风格）+ 'Z' 结尾 → 客户端无需处理时区即可倒计时。
        expires_at=expires_at.isoformat() + "Z",
        content_type=asset.mime,
        size=asset.size,
    )
