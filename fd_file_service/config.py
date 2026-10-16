"""运行时配置：全部经环境变量注入（变更任务 7.1）。

凭据与端点不入仓——``.env.example`` 只给键名与占位值，真实值走本地
``.env``（已 gitignore）。存储面为 S3 兼容接口（RustFS / MinIO / 专业对象
存储同接口，design D3 / D11），换存储只改 ``FILE_S3_*`` 四个值。
"""

from __future__ import annotations

import json

from pydantic_settings import BaseSettings, SettingsConfigDict

#: 配额档位名 → 配置字段名（design D6：免费 1G / 基础 5G / 标准 10G）。
#: ``unlimited`` 为平台内部资产档（走监控、不限额），不占三档定价。
QUOTA_TIERS: tuple[str, ...] = ("free", "basic", "standard", "unlimited")

GIB = 1024**3


class Settings(BaseSettings):
    """环境变量同名覆盖（``FILE_S3_ENDPOINT`` → ``file_s3_endpoint``）。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- 存储（S3 兼容）----
    file_s3_endpoint: str = "http://127.0.0.1:9000"
    file_s3_access_key: str = ""
    file_s3_secret_key: str = ""
    file_s3_bucket: str = "fd-file"
    file_s3_region: str = "us-east-1"
    #: RustFS 实测必须 path-style（虚拟托管式签名在本端点不通）。
    file_s3_addressing_style: str = "path"

    # ---- 配额（按 owner，去重后字节）----
    file_quota_free_bytes: int = 1 * GIB
    file_quota_basic_bytes: int = 5 * GIB
    file_quota_standard_bytes: int = 10 * GIB
    #: 未显式指派档位的 owner 走此档。
    file_quota_default_tier: str = "free"
    #: 按 owner 覆盖档位，JSON 形如 ``{"wire:internal":"unlimited"}``。
    file_quota_tier_overrides: str = ""

    # ---- 生命周期 ----
    #: 软删除保留天数（D8：防误删经双向同步网盘扩散）。
    file_soft_delete_days: int = 30
    file_presign_upload_ttl_seconds: int = 900

    # ---- 上传校验 ----
    file_max_upload_bytes: int = 2 * GIB
    #: 白名单，逗号分隔；空 = 不限制类型（只限大小）。
    file_allowed_mimes: str = ""
    #: 确认时对字节做 sha256 复算——会把字节过一遍服务进程，默认关。
    file_verify_sha256_on_confirm: bool = False

    # ---- 元数据（SQLite，design D5）----
    file_database_url: str = "sqlite:///./fd_file_service.db"

    def quota_limit_bytes(self, owner: str) -> int:
        """某 owner 的配额上限；``unlimited`` 档返回 -1（不限）。"""
        tier = self.tier_of(owner)
        if tier == "unlimited":
            return -1
        return {
            "free": self.file_quota_free_bytes,
            "basic": self.file_quota_basic_bytes,
            "standard": self.file_quota_standard_bytes,
        }[tier]

    def tier_of(self, owner: str) -> str:
        """owner → 档位名。覆盖表优先，否则默认档。"""
        for key, tier in self._overrides().items():
            if key == owner:
                return tier
        return self.file_quota_default_tier

    def _overrides(self) -> dict[str, str]:
        raw = self.file_quota_tier_overrides.strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v) for k, v in data.items() if str(v) in QUOTA_TIERS}

    def allowed_mimes(self) -> list[str] | None:
        """None = 不限制类型。"""
        raw = self.file_allowed_mimes.strip()
        if not raw:
            return None
        return [m.strip() for m in raw.split(",") if m.strip()]


#: 模块级单例。验收脚本可就地改字段（如把保留期改成 0 天以验证释放），
#: 运行时改端点/凭据只需重启进程——不做热重载（v1 单实例）。
settings = Settings()
