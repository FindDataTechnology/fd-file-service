"""S3 兼容存储层（变更任务 2.1 / 2.3 / D3 / D11）。

只依赖 S3 接口，不依赖任何厂商专有能力：换 RustFS → MinIO → 专业对象存储
只改 ``FILE_S3_ENDPOINT`` / 凭据 / 桶名，业务行为不变。

字节**从不经过服务进程**：上传给预签名 PUT URL，取用给预签名 GET URL
（有实效，spec「取用地址有实效」）。
"""

from __future__ import annotations

import hashlib

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def owner_hash(owner: str) -> str:
    """owner → 桶内前缀（D3：``<owner_hash>/<file_id>``）。

    哈希而非原样：owner 是任意不透明串，可能含 S3 键不友好的字符；哈希后
    前缀定长、可安全拼接，且前缀本身不泄漏 owner 语义（服务不理解业务）。
    """
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]


class Storage:
    """S3 兼容客户端薄封装。"""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
        addressing_style: str = "path",
    ):
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(s3={"addressing_style": addressing_style}, signature_version="s3v4"),
        )

    # ---- 桶 ----
    def ensure_bucket(self) -> bool:
        """桶不存在则创建；已存在返回 False。

        RustFS/MinIO 的 ``create_bucket`` 对已存在桶抛 BucketAlreadyOwnedByYou；
        先 head 判断更稳，避免对既有桶（如 ``harbor``）产生任何写动作。
        """
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return False
        except ClientError:
            self._client.create_bucket(Bucket=self.bucket)
            return True

    def list_buckets(self) -> list[str]:
        return [b["Name"] for b in self._client.list_buckets().get("Buckets", [])]

    def delete_bucket(self) -> None:
        """仅测试清理用——删桶前须已删空对象。"""
        self._client.delete_bucket(Bucket=self.bucket)

    # ---- 预签名（字节不经服务）----
    def presign_put(self, key: str, mime: str, size: int, ttl: int) -> str:
        """一次性受限 PUT URL。

        用 POST 策略把 ``content-length-range`` 与 ``Content-Type`` 钉进签名，
        使凭据**无法**被用于上传别的大小或别的类型（任务 2.1「受限」）。
        RustFS/MinIO 均支持此策略字段。
        """
        return self._client.generate_presigned_post(
            Bucket=self.bucket,
            Key=key,
            Fields={"Content-Type": mime},
            Conditions=[
                {"Content-Type": mime},
                ["content-length-range", size, size],
            ],
            ExpiresIn=ttl,
        )

    def presign_put_url(self, key: str, mime: str, ttl: int) -> str:
        """纯 PUT URL（无大小钉定）。策略式不可用时回落。"""
        return self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": mime},
            ExpiresIn=ttl,
        )

    def presign_get(self, key: str, ttl: int = 300) -> str:
        """带有效期的取用地址——绝不返回永久公开地址。"""
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl,
        )

    # ---- 对象 ----
    def head(self, key: str) -> dict | None:
        try:
            return self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError:
            return None

    def get_object(self, key: str) -> bytes | None:
        """取回字节；不存在返回 None（对齐 head() 口径——"不在桶里"是
        可留痕的业务结果，不是崩溃）。"""
        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError:
            return None

    def put_object(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        """覆盖写。解析产物（抽取文本）走这条。"""
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def list_keys(self, prefix: str = "") -> list[str]:
        out: list[str] = []
        token = None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kw)
            out.extend(o["Key"] for o in resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                return out
            token = resp.get("NextContinuationToken")

    def delete_all(self, prefix: str = "") -> int:
        """删空（测试清理）。返回删除对象数。"""
        keys = self.list_keys(prefix)
        for k in keys:
            self.delete(k)
        return len(keys)


def from_settings() -> Storage:
    """按配置构造 Storage。"""
    from .config import settings

    return Storage(
        endpoint=settings.file_s3_endpoint,
        access_key=settings.file_s3_access_key,
        secret_key=settings.file_s3_secret_key,
        bucket=settings.file_s3_bucket,
        region=settings.file_s3_region,
        addressing_style=settings.file_s3_addressing_style,
    )
