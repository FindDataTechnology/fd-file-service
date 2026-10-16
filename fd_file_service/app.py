"""FastAPI 入口：`uvicorn fd_file_service.app:app`（变更任务 1.1）。

路由面（本变更任务 1.1-1.4 / 2.1-2.2 / 3.1-3.4 / 7.1；列表/取用接口的完整
过滤面与事件解析管线归并行 change，此处只给验证所需的取用地址端点）：

    GET  /health                        健康检查
    POST /files/upload-url              申请上传凭据（预签名，字节不经服务）
    POST /files/{file_id}/confirm       确认上传 → 计入配额与列表
    GET  /files                         按 owner 列表（严格限域）
    GET  /files/{file_id}/download-url  带有效期的取用地址
    DELETE /files/{file_id}             软删除（保留期可恢复）
    POST /files/{file_id}/restore       保留期内恢复
    GET  /quota                         当前 owner 用量/上限
    POST /maintenance/purge             跑一轮保留期满释放

``owner`` 一律由调用方显式传入（不透明字符串，D2）——服务不解析、不校验
其结构，也不从请求头推断身份（鉴权归网关，本服务只做域隔离）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import __version__
from .config import settings
from .db import SessionLocal, init_db
from .service import (
    FileError,
    confirm_upload,
    list_deleted,
    purge_expired,
    quota_state,
    request_upload,
    restore,
    soft_delete,
)
from .storage import from_settings

_storage = None


def get_storage():
    global _storage
    if _storage is None:
        _storage = from_settings()
    return _storage


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="fd-file-service",
    version=__version__,
    description="统一文件服务：owner 域隔离 + 预签名直传 + 配额/去重/软删除",
    lifespan=lifespan,
)


@app.exception_handler(FileError)
async def _file_error_handler(request: Request, exc: FileError) -> JSONResponse:
    """业务错误统一形态：code + message + 上下文字段（配额超限含用量/上限）。"""
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message, **exc.extra}},
    )


# ------------------------------------------------------------------ schema


class UploadUrlRequest(BaseModel):
    owner: str = Field(..., description="不透明 owner 域串，如 wire:cust_x")
    size: int = Field(..., gt=0)
    mime: str = "application/octet-stream"
    sha256: str = Field(..., min_length=64, max_length=64)
    filename: str | None = None
    #: 默认走 POST 策略（可把大小与类型钉进签名）；false 退化为纯 PUT URL。
    use_post_policy: bool = True


class UploadUrlResponse(BaseModel):
    file_id: str
    upload_url: str
    method: str
    fields: dict[str, Any] | None = None
    storage_key: str
    expires_in: int
    dedup: bool


class AssetOut(BaseModel):
    file_id: str
    owner: str
    sha256: str
    size: int
    mime: str
    filename: str | None
    status: str
    created_at: datetime
    confirmed_at: datetime | None
    deleted_at: datetime | None
    purge_after: datetime | None


def _out(a) -> AssetOut:
    return AssetOut(
        file_id=a.file_id,
        owner=a.owner,
        sha256=a.sha256,
        size=a.size,
        mime=a.mime,
        filename=a.filename,
        status=a.status,
        created_at=a.created_at,
        confirmed_at=a.confirmed_at,
        deleted_at=a.deleted_at,
        purge_after=a.purge_after,
    )


# -------------------------------------------------------------------- 路由


@app.get("/health")
def health() -> dict:
    """健康检查（任务 1.1 验收项）。"""
    return {"status": "ok", "service": "fd-file-service", "version": __version__}


@app.post("/files/upload-url", response_model=UploadUrlResponse)
def upload_url(body: UploadUrlRequest, db: Session = Depends(get_db)) -> UploadUrlResponse:
    grant = request_upload(
        session=db,
        storage=get_storage(),
        owner=body.owner,
        size=body.size,
        mime=body.mime,
        sha256=body.sha256,
        filename=body.filename,
        use_post_policy=body.use_post_policy,
    )
    return UploadUrlResponse(
        file_id=grant.file_id,
        upload_url=grant.upload_url,
        method=grant.method,
        fields=grant.fields,
        storage_key=grant.storage_key,
        expires_in=grant.expires_in,
        dedup=grant.dedup,
    )


@app.post("/files/{file_id}/confirm", response_model=AssetOut)
def confirm(
    file_id: str,
    owner: str = Query(..., description="必须与申请时一致"),
    db: Session = Depends(get_db),
) -> AssetOut:
    asset = confirm_upload(session=db, storage=get_storage(), owner=owner, file_id=file_id)
    return _out(asset)


@app.get("/files")
def list_files(
    owner: str = Query(..., description="不透明 owner 域串；跨域文件永不出现"),
    mime: str | None = Query(None, description="按 MIME 精确过滤"),
    mime_prefix: str | None = Query(
        None, description="按 MIME 前缀过滤，如 image/ 或 application/vnd."
    ),
    created_after: str | None = None,
    created_before: str | None = None,
    since: str | None = Query(None, description="ISO8601；created_at >= 该时刻"),
    until: str | None = Query(None, description="ISO8601；created_at <= 该时刻"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """按 owner 列表——跨域记录永不出现（任务 1.3 / 4.1）。

    分页计数与 MIME 前缀过滤复用 list_access.query_files()：total/has_more
    来自独立 count 查询，不靠「本页条数 == limit」猜。created_after/
    created_before 为早期参数名，与 since/until 等价，两者并存以免破坏
    既有调用方。
    """
    from .list_access import query_files

    resp = query_files(
        db=db,
        owner=owner,
        mime=mime,
        mime_prefix=mime_prefix,
        since=since if since is not None else created_after,
        until=until if until is not None else created_before,
        limit=limit,
        offset=offset,
    )
    return resp.model_dump(mode="json")


@app.get("/files/trash")
def trash(owner: str = Query(...), db: Session = Depends(get_db)) -> dict:
    """回收站（保留期内）。"""
    items = list_deleted(session=db, owner=owner)
    return {"owner": owner, "count": len(items), "items": [_out(a) for a in items]}


@app.get("/files/{file_id}/download-url")
def download_url(
    file_id: str,
    owner: str = Query(...),
    ttl: int | None = Query(
        None, description="有效期秒数；缺省取 FILE_ACCESS_URL_TTL_SECONDS，上限由上配置夹取"
    ),
    db: Session = Depends(get_db),
) -> dict:
    """带有效期的取用地址——不返回永久公开地址（spec「取用地址有实效」）。

    有效期经 list_access.clamp_ttl() 夹到上限内（防调用方签出近乎永久的
    地址）；owner 不匹配与「不存在」返回同一 404 文案，不泄漏存在性。
    """
    from .list_access import get_access

    resp = get_access(db=db, file_id=file_id, owner=owner, ttl=ttl, storage=get_storage())
    return resp.model_dump(mode="json")


@app.delete("/files/{file_id}", response_model=AssetOut)
def delete_file(file_id: str, owner: str = Query(...), db: Session = Depends(get_db)) -> AssetOut:
    """软删除：打标记 + 隐藏，保留期内可恢复（任务 3.4）。"""
    return _out(soft_delete(session=db, owner=owner, file_id=file_id))


@app.post("/files/{file_id}/restore", response_model=AssetOut)
def restore_file(file_id: str, owner: str = Query(...), db: Session = Depends(get_db)) -> AssetOut:
    return _out(restore(session=db, owner=owner, file_id=file_id))


@app.get("/quota")
def quota(owner: str = Query(...), db: Session = Depends(get_db)) -> dict:
    return quota_state(session=db, owner=owner)


@app.post("/maintenance/purge")
def purge(owner: str | None = None, db: Session = Depends(get_db)) -> dict:
    """跑一轮保留期满释放（引用计数归零才删字节）。"""
    return purge_expired(session=db, storage=get_storage(), owner=owner)


@app.get("/config")
def show_config() -> dict:
    """生效配置自检（不含凭据）——任务 7.1「配置项生效」的验证面。"""
    return {
        "bucket": settings.file_s3_bucket,
        "endpoint": settings.file_s3_endpoint,
        "region": settings.file_s3_region,
        "addressing_style": settings.file_s3_addressing_style,
        "quota_free_bytes": settings.file_quota_free_bytes,
        "quota_basic_bytes": settings.file_quota_basic_bytes,
        "quota_standard_bytes": settings.file_quota_standard_bytes,
        "quota_default_tier": settings.file_quota_default_tier,
        "soft_delete_days": settings.file_soft_delete_days,
        "max_upload_bytes": settings.file_max_upload_bytes,
        "database_url": settings.file_database_url,
    }
