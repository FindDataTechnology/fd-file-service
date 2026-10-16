"""Gotenberg HTTP 客户端（v1：只做 Office → PDF 这一段）。

端点 ``FILE_GOTENBERG_URL`` 指向跑着 ``gotenberg`` 容器的机器；
**部署动作由用户在 ``windows-laptop``（100.64.0.10）执行**（任务 6.1），
本模块只负责调用与健康检查。

multipart 表单字段名（核对 upstream ``pkg/modules/libreoffice/routes.go``）：

- 文件字段：``files``（可多份）
- 可选字段：``password`` / ``landscape`` / ``merge`` / ``flatten`` / ``pdfa`` 等

本客户端**只按需发字段**，不预置默认值——避免与将来版本的默认行为漂移撞车。

返回值是 **PDF 字节**（``Content-Type: application/pdf``）。单份输入且未开
``merge`` 时，响应体就是那个 PDF 本体（不是 zip）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

#: 兜底默认端点（Gotenberg 自有默认端口 3000）。**生产由 ``FILE_GOTENBERG_URL``
#: 指向实际部署地址**——部署机的 3000 被其它在用应用占用，Gotenberg 部署在
#: 3010（见 DEPLOY-WINDOWS.md / deploy/env.example）。
DEFAULT_URL = "http://100.64.0.10:3010"

ENV_URL = "FILE_GOTENBERG_URL"
ENV_TIMEOUT = "FILE_GOTENBERG_TIMEOUT_SECONDS"

#: LibreOffice 能读、Gotenberg 可转的常见办公/文本类型（不全量罗列）。
OFFICE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".doc",
        ".docx",
        ".odt",
        ".rtf",
        ".xls",
        ".xlsx",
        ".ods",
        ".ppt",
        ".pptx",
        ".odp",
        ".txt",
        ".md",
        ".html",
        ".htm",
        ".epub",
    }
)


class GotenbergError(RuntimeError):
    """调用失败。``status`` 为 HTTP 状态码（0 = 非 HTTP 错误，如连接失败）。"""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


@dataclass
class Client:
    """Gotenberg 薄客户端。

    :param base_url: 根 URL（如 ``http://100.64.0.10:3010``），尾随斜杠无害。
    :param timeout: 单次请求超时（秒）。LibreOffice 冷启动偏慢，默认 120s。

    为什么显式建 ``trust_env=False`` 的客户端、而不是模块级 ``httpx.get/post``：
    本机（及部署侧）系统代理指向一个内网地址（tailnet/局域网），tailnet 到内网
    的调用会被代理吞掉并挂到超时——直连与系统代理语义相反，必须显式绕开。
    """

    base_url: str = DEFAULT_URL
    timeout: float = 120.0

    @classmethod
    def from_env(cls) -> Client:
        raw = os.environ.get(ENV_URL, "").strip()
        base = raw if raw else DEFAULT_URL
        return cls(base_url=base.rstrip("/"), timeout=_env_float(ENV_TIMEOUT, 120.0))

    def _client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=timeout if timeout is not None else self.timeout,
            trust_env=False,
        )

    # ---- 健康检查 ----
    def health(self) -> tuple[bool, str]:
        """``GET /health`` → ``(是否健康, 说明)``。

        不抛异常：健康检查结果要能被 worker / 运维脚本直接判定。连接失败、
        404、非 200 一律视为不健康（不是 Gotenberg，或没起来）。
        """
        try:
            with self._client(timeout=min(self.timeout, 15.0)) as c:
                r = c.get("/health")
        except httpx.HTTPError as exc:
            return False, f"unreachable: {type(exc).__name__}: {exc}"
        if r.status_code == 200:
            return True, (r.text or "").strip() or "OK"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"

    # ---- 转换 ----
    def to_pdf(self, data: bytes, filename: str, *, merge: bool = False) -> bytes:
        """把 Office / 文本类文件转成 PDF，返回 PDF 字节。

        ``filename`` 必须带真实扩展名：LibreOffice 按扩展名选择输入 filter，
        扩展名与实际格式不符会被上游拒绝为 400。
        """
        files = {"files": (filename, data)}
        form = {"merge": "true"} if merge else {}
        try:
            with self._client() as c:
                r = c.post("/forms/libreoffice/convert", files=files, data=form)
        except httpx.HTTPError as exc:
            raise GotenbergError(f"Gotenberg 不可达: {type(exc).__name__}: {exc}") from exc

        if r.status_code != 200:
            raise GotenbergError(
                f"Gotenberg 转换失败 (HTTP {r.status_code}) file={filename}: {_decode_body(r)}",
                status=r.status_code,
            )
        if not r.content:
            raise GotenbergError(f"Gotenberg 返回空 body file={filename}")
        _assert_pdf_magic(r.content, filename)
        return r.content


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _decode_body(r: httpx.Response) -> str:
    """上游常把 LibreOffice 的诊断文案放进错误响应体，值得原样带上。"""
    if not r.content:
        return r.reason_phrase or ""
    try:
        text = r.content.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover
        return repr(r.content[:200])
    return " ".join(text.split())[:500]


def _assert_pdf_magic(content: bytes, filename: str) -> None:
    """校验魔数 ``%PDF``。

    Gotenberg 出错时可能返回 **200 + 非 PDF body**。不校验的话，坏 PDF 会流进
    下游抽取器，报错变成「PDF 损坏」而非根因「转换失败」，排障会南辕北辙。
    早失败、并带上文件名，才是最有用的信号。
    """
    if not content.startswith(b"%PDF"):
        raise GotenbergError(
            f"Gotenberg 返回的不是 PDF（缺 %PDF 魔数） file={filename}: {content[:80]!r}"
        )


__all__ = [
    "DEFAULT_URL",
    "OFFICE_EXTENSIONS",
    "Client",
    "GotenbergError",
]
