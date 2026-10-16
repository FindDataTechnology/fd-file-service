"""文件服务核心逻辑（1.2 / 1.3 / 2.1 / 2.2 / 3.1 / 3.2 / 3.4）。

所有函数以 ``owner`` 为第一等参数——**域隔离不是装饰器，是每个查询的
WHERE 条件**（design D2）。跨域调用不会报错，只会查不到（spec「跨域不可见」）。

配额语义：按 owner，计**去重后**字节（存活字节对象的 size 之和）。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .db import FileAsset, FileObject, utcnow
from .storage import Storage, owner_hash


class FileError(Exception):
    """可预期业务错误；HTTP 层按 ``status`` 转成响应。"""

    def __init__(self, status: int, code: str, message: str, **extra: object):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def new_file_id() -> str:
    return uuid.uuid4().hex


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 配额（3.1）


def quota_usage(session: Session, owner: str) -> int:
    """该 owner 的去重后字节占用。

    只数**存活**字节对象（未 purge）且被**存活**文件记录引用（confirmed 且
    未删除）。未确认上传不计（2.2）；已软删但保留期内仍占用——保留期是给用户
    反悔的窗口，此时字节还在桶里，计入才不会超额。
    """
    return int(
        session.scalar(
            select(func.coalesce(func.sum(FileObject.size), 0)).where(
                FileObject.owner == owner,
                FileObject.purged_at.is_(None),
                FileObject.ref_count > 0,
            )
        )
        or 0
    )


def quota_state(session: Session, owner: str) -> dict:
    """用量 / 上限 / 档位；``-1`` 表示不限额。"""
    used = quota_usage(session, owner)
    limit = settings.quota_limit_bytes(owner)
    return {
        "owner": owner,
        "tier": settings.tier_of(owner),
        "used_bytes": used,
        "limit_bytes": limit,
        "remaining_bytes": -1 if limit < 0 else max(0, limit - used),
        "unlimited": limit < 0,
    }


def _check_quota(session: Session, owner: str, needed: int) -> None:
    """超限抛 413，报文含用量/上限/所需大小（spec「超限拒绝」）。"""
    limit = settings.quota_limit_bytes(owner)
    if limit < 0:
        return
    used = quota_usage(session, owner)
    if used + needed > limit:
        raise FileError(
            413,
            "quota_exceeded",
            f"配额不足：已用 {used} 字节，上限 {limit} 字节，需要 {needed} 字节",
            owner=owner,
            tier=settings.tier_of(owner),
            used_bytes=used,
            limit_bytes=limit,
            requested_bytes=needed,
            remaining_bytes=max(0, limit - used),
        )


# ------------------------------------------------------- 上传（2.1 / 2.2 / 3.2）


@dataclass
class UploadGrant:
    file_id: str
    upload_url: str
    #: POST 策略表单字段（预签名 POST）；纯 PUT 时为 None。
    fields: dict | None
    method: str
    storage_key: str
    expires_in: int
    dedup: bool


def request_upload(
    session: Session,
    storage: Storage,
    owner: str,
    size: int,
    mime: str,
    sha256: str,
    filename: str | None = None,
    use_post_policy: bool = True,
) -> UploadGrant:
    """申请上传凭据。

    - 校验大小 / 类型 / 配额
    - 命中同 owner 同 sha256 → 秒传（去重，不新建字节、不占配额）
    - 否则建 pending 记录 + 签发受限上传 URL

    **字节不经本进程**：只返回 URL。
    """
    if size <= 0:
        raise FileError(400, "invalid_size", "size 必须为正整数")
    if size > settings.file_max_upload_bytes:
        raise FileError(
            413,
            "file_too_large",
            f"单文件 {size} 字节超过上限 {settings.file_max_upload_bytes} 字节",
            size=size,
            max_bytes=settings.file_max_upload_bytes,
        )
    allowed = settings.allowed_mimes()
    if allowed is not None and mime not in allowed:
        raise FileError(
            415,
            "mime_not_allowed",
            f"类型 {mime} 不在允许列表",
            mime=mime,
            allowed=allowed,
        )
    if len(sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in sha256):
        raise FileError(400, "invalid_sha256", "sha256 必须是 64 位十六进制串")

    sha256 = sha256.lower()

    # ---- 去重（3.2）：同 owner 同 sha256 且字节仍存活 → 秒传 ----
    existing = session.scalar(
        select(FileObject).where(
            FileObject.owner == owner,
            FileObject.sha256 == sha256,
            FileObject.purged_at.is_(None),
        )
    )
    if existing is not None:
        return _attach_dedup_reference(
            session, owner, existing, sha256, size, mime, filename, dedup=True
        )

    _check_quota(session, owner, size)

    file_id = new_file_id()
    storage_key = f"{owner_hash(owner)}/{file_id}"
    ttl = settings.file_presign_upload_ttl_seconds

    if use_post_policy:
        post = storage.presign_put(storage_key, mime, size, ttl)
        url, fields = post["url"], post["fields"]
        method = "POST"
    else:
        url, fields = storage.presign_put_url(storage_key, mime, ttl), None
        method = "PUT"

    asset = FileAsset(
        file_id=file_id,
        owner=owner,
        object_id=None,
        sha256=sha256,
        size=size,
        mime=mime,
        storage_key=storage_key,
        filename=filename,
        confirmed_at=None,
    )
    session.add(asset)
    session.commit()

    return UploadGrant(
        file_id=file_id,
        upload_url=url,
        fields=fields,
        method=method,
        storage_key=storage_key,
        expires_in=ttl,
        dedup=False,
    )


def _attach_dedup_reference(
    session: Session,
    owner: str,
    obj: FileObject,
    sha256: str,
    size: int,
    mime: str,
    filename: str | None,
    dedup: bool,
) -> UploadGrant:
    """命中已有字节 → 新建**已确认**的文件记录，ref_count +1。

    秒传无需再传字节，故直接进 live 状态（没有 pending 窗口）。
    """
    obj.ref_count += 1
    file_id = new_file_id()
    asset = FileAsset(
        file_id=file_id,
        owner=owner,
        object_id=obj.id,
        sha256=sha256,
        size=size,
        mime=mime,
        storage_key=obj.storage_key,
        filename=filename,
        confirmed_at=utcnow(),
    )
    session.add(asset)
    session.commit()
    _publish_confirmed(asset)
    return UploadGrant(
        file_id=file_id,
        upload_url="",
        fields=None,
        method="DEDUP",
        storage_key=obj.storage_key,
        expires_in=0,
        dedup=dedup,
    )


def confirm_upload(
    session: Session,
    storage: Storage,
    owner: str,
    file_id: str,
    size: int | None = None,
    mime: str | None = None,
) -> FileAsset:
    """确认上传（2.2）：pending → live，此时才计入配额与列表。

    确认前 head 桶内对象：不存在则拒绝（避免给没字节的记录记账）。
    """
    asset = _get_owned(session, owner, file_id)
    if asset.confirmed_at is not None:
        # 幂等早退：但若上次「入库成功、事件发布失败」，队列里没有这条——
        # 幂等发布恰好补上（见 events.publish_file_confirmed_once 的注释）。
        _publish_confirmed(asset)
        return asset

    asset_size = size or asset.size
    # 桶里没字节就不能记账（未确认的垃圾不进配额与列表）
    if storage.head(asset.storage_key) is None:
        raise FileError(
            409, "not_uploaded", "存储中找不到该文件的字节，请重新上传", file_id=file_id
        )

    # 可能并发重复确认（同内容同时确认）→ 用 (owner, sha256) 唯一约束兜底
    new_key = f"{owner_hash(owner)}/{file_id}"
    obj = FileObject(
        owner=owner,
        sha256=asset.sha256,
        size=asset_size,
        storage_key=new_key,
        ref_count=0,
        created_at=utcnow(),
    )
    session.add(obj)
    try:
        session.flush()
    except IntegrityError:
        # 已存在同 (owner, sha256)：可能是并发确认撞车，也可能是这一份内容
        # 曾被 purge（字节已删）后又重新上传——后者必须**复活**该行并指向新键，
        # 否则会引用一个字节已不存在的对象。
        session.rollback()
        obj = session.scalar(
            select(FileObject).where(FileObject.owner == owner, FileObject.sha256 == asset.sha256)
        )
        assert obj is not None
        if obj.purged_at is not None:
            obj.purged_at = None
            obj.storage_key = new_key
            obj.size = asset_size
            obj.ref_count = 0
        elif obj.ref_count > 0:
            # 并发确认撞车：本次不该再占一份字节，退回既有对象即可。
            pass

    obj.ref_count += 1
    asset.object_id = obj.id
    # 撞车时本次上传的字节成了孤儿（键不同、无人引用）；资产一律指向实际持有的
    # 对象键，孤儿键由运维侧的桶内前缀清理收尾（v1 不自动 GC，避免误删）。
    asset.storage_key = obj.storage_key
    asset.confirmed_at = utcnow()
    if size is not None:
        asset.size = size
    if mime is not None:
        asset.mime = mime
    session.commit()
    _publish_confirmed(asset)
    return asset


def _publish_confirmed(asset: FileAsset) -> None:
    """live 转换后发布 ``file.confirmed``（任务 5.1 的唯一发布点）。

    放在 service 层而不是路由层：秒传路径（``_attach_dedup_reference``）与
    正常确认路径都经这里，漏接一处就是「有的文件永不解析」的静默缺口。
    发布失败向上抛——确认接口报 5xx，调用方重试走幂等补发，不产生
    「文件已确认但永不解析」的状态。
    """
    from .events import publish_file_confirmed_once

    publish_file_confirmed_once(
        file_id=asset.file_id,
        owner=asset.owner,
        sha256=asset.sha256,
        mime=asset.mime,
        storage_key=asset.storage_key,
        size=asset.size,
        filename=asset.filename,
    )


# ------------------------------------------------------------ 软删除 / 恢复（3.4）


def soft_delete(session: Session, owner: str, file_id: str) -> FileAsset:
    """打 deleted_at → 从列表隐藏；保留期内可恢复。"""
    asset = _get_owned(session, owner, file_id)
    if asset.deleted_at is not None:
        return asset  # 幂等
    asset.deleted_at = utcnow()
    asset.purge_after = asset.deleted_at + timedelta(days=settings.file_soft_delete_days)
    session.commit()
    return asset


def restore(session: Session, owner: str, file_id: str) -> FileAsset:
    """保留期内恢复；已真正清除（purged）则报 410。"""
    asset = _get_owned_any(session, owner, file_id)
    if asset.purged_at is not None:
        raise FileError(410, "already_purged", "文件已超过保留期并释放，不可恢复", file_id=file_id)
    if asset.deleted_at is None:
        return asset  # 幂等
    asset.deleted_at = None
    asset.purge_after = None
    session.commit()
    return asset


def purge_expired(session: Session, storage: Storage, owner: str | None = None) -> dict:
    """保留期满释放（3.4）：引用计数归零才删字节。

    返回 ``{"scanned":n,"assets_purged":n,"objects_purged":n,"bytes_freed":n}``。
    """
    now = utcnow()
    q = select(FileAsset).where(
        FileAsset.deleted_at.is_not(None),
        FileAsset.purged_at.is_(None),
        FileAsset.purge_after.is_not(None),
        FileAsset.purge_after <= now,
    )
    if owner is not None:
        q = q.where(FileAsset.owner == owner)

    assets = list(session.scalars(q))
    if not assets:
        return {"scanned": 0, "assets_purged": 0, "objects_purged": 0, "bytes_freed": 0}

    obj_ids = sorted({a.object_id for a in assets if a.object_id is not None})
    for a in assets:
        a.purged_at = now
        if a.object_id is not None:
            obj = session.get(FileObject, a.object_id)
            if obj is not None:
                obj.ref_count = max(0, obj.ref_count - 1)

    session.flush()

    bytes_freed = 0
    objects_purged = 0
    for oid in obj_ids:
        obj = session.get(FileObject, oid)
        if obj is None or obj.purged_at is not None:
            continue
        if obj.ref_count <= 0:  # 最后一个引用清除 → 真删字节
            storage.delete(obj.storage_key)
            obj.purged_at = now
            bytes_freed += obj.size
            objects_purged += 1

    session.commit()
    return {
        "scanned": len(assets),
        "assets_purged": len(assets),
        "objects_purged": objects_purged,
        "bytes_freed": bytes_freed,
    }


# -------------------------------------------------------------------- 查询（1.3）


def list_assets(
    session: Session,
    owner: str,
    mime: str | None = None,
    created_after: object = None,
    created_before: object = None,
    limit: int = 100,
    offset: int = 0,
) -> list[FileAsset]:
    """按 owner 列表（严格限域）。只返回 live（已确认且未删除）。"""
    q = select(FileAsset).where(
        FileAsset.owner == owner,
        FileAsset.confirmed_at.is_not(None),
        FileAsset.deleted_at.is_(None),
        FileAsset.purged_at.is_(None),
    )
    if mime is not None:
        q = q.where(FileAsset.mime == mime)
    if created_after is not None:
        q = q.where(FileAsset.created_at >= created_after)
    if created_before is not None:
        q = q.where(FileAsset.created_at <= created_before)
    q = q.order_by(FileAsset.created_at.desc()).limit(limit).offset(offset)
    return list(session.scalars(q))


def list_deleted(session: Session, owner: str) -> list[FileAsset]:
    """回收站：已软删、未清除。"""
    return list(
        session.scalars(
            select(FileAsset)
            .where(
                FileAsset.owner == owner,
                FileAsset.deleted_at.is_not(None),
                FileAsset.purged_at.is_(None),
            )
            .order_by(FileAsset.deleted_at.desc())
        )
    )


def get_asset(session: Session, owner: str, file_id: str) -> FileAsset:
    """取一条 live 记录；跨域或不存在 → 404（不泄漏存在性）。"""
    return _get_owned(session, owner, file_id)


def presign_download(
    session: Session, storage: Storage, owner: str, file_id: str, ttl: int = 300
) -> str:
    """带有效期的取用地址（不接受永久公开地址）。"""
    asset = _get_owned(session, owner, file_id)
    return storage.presign_get(asset.storage_key, ttl)


# ------------------------------------------------------------------ 内部工具


def _get_owned(session: Session, owner: str, file_id: str) -> FileAsset:
    asset = session.get(FileAsset, file_id)
    if (
        asset is None
        or asset.owner != owner
        or asset.deleted_at is not None
        or asset.purged_at is not None
    ):
        raise FileError(404, "not_found", "文件不存在", file_id=file_id)
    return asset


def _get_owned_any(session: Session, owner: str, file_id: str) -> FileAsset:
    """含已软删记录（恢复/强制删除走这条）。"""
    asset = session.get(FileAsset, file_id)
    if asset is None or asset.owner != owner:
        raise FileError(404, "not_found", "文件不存在", file_id=file_id)
    return asset
