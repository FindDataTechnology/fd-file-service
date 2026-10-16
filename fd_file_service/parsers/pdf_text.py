"""PDF → 纯文本。

为什么本地抽取而不叫 Gotenberg 代劳
----------------------------------
见 ``parsers/__init__.py``：Gotenberg 的所有路由都以 PDF 为输出，
**没有文本抽取端点**（v1 核对结论）。所以这一步必须在本地做。

备选后端的取舍
--------------
按「文本质量」→「可用性」排序，逐个尝试，取第一个装得上的：

1. **PyMuPDF (``fitz``)**：带布局信息的 CJK 提取质量最好，复杂排版最稳；
2. **pdfminer.six**：纯 Python，无二进制依赖，对非标准 PDF 兼容性高；
3. **pypdf**：纯 Python，pip 永远装得上，但对某些 PDF（尤其 CJK 与损坏 xref）
   会抽出空串或乱码。

三者都是**软依赖**：一个都没装时不是直接 ``ImportError`` 死掉，而是由调用方
得到 ``NoExtractorAvailable``——这样部署清单里可以明确写「至少装一个」，
而不是把版本约束写进核心依赖把整个服务拖死。

注意：三者都**不做 OCR**。影印型 PDF 会抽出空文本，这在 v1 是预期行为
（OCR 属 v2）；调用方不得把「空文本」视为失败——详见 ``parse.py`` 的注释。
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable

Extractor = Callable[[bytes], str]


class NoExtractorAvailable(RuntimeError):
    """三个后端一个都没装。"""


class PdfTextError(RuntimeError):
    """PDF 解析失败（损坏、加密、非 PDF）。"""


def available_backends() -> list[str]:
    """已可用的后端名，按推荐顺序。"""
    out: list[str] = []
    for name, probe in (
        ("pymupdf", lambda: __import__("fitz")),
        ("pdfminer", lambda: __import__("pdfminer.high_level")),
        ("pypdf", lambda: __import__("pypdf")),
    ):
        try:
            probe()
        except Exception:
            continue
        out.append(name)
    return out


def extract_text(pdf: bytes, *, backend: str | None = None) -> tuple[str, str]:
    """抽取文本 → ``(文本, 后端名)``。

    :param backend: 指定后端；``None`` = 按推荐顺序自动挑。
    :raises NoExtractorAvailable: 无可用后端
    :raises PdfTextError: PDF 本身读不了
    """
    if backend:
        impl = _get_impl(backend)
        return impl(pdf), backend

    available = available_backends()
    if not available:
        raise NoExtractorAvailable(
            "无可用 PDF 文本抽取后端：请安装 pymupdf / pdfminer.six / pypdf 之一"
        )
    last_exc: Exception | None = None
    for name in available:
        try:
            return _get_impl(name)(pdf), name
        except Exception as exc:  # 换下一个后端继续试
            last_exc = exc
            continue
    raise PdfTextError(f"所有可用后端均失败: {last_exc!r}")


def normalize(text: str) -> str:
    """轻量清洗：把换行/大量空白压成检索友好的形态，不做语言相关处理。

    为什么要归一化：抽取器吐出的文本夹杂大量软回车与页眉页码；不清洗会让
    后续全文检索命中率下降、chunk 边界碎片化。这里只做**无损语义**的部分——
    不去重、不排序、不改词，保证原文可逐字核对。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


# --------------------------------------------------------------------------- #
# 后端实现
# --------------------------------------------------------------------------- #


def _get_impl(name: str) -> Extractor:
    if name == "pymupdf":
        return _via_pymupdf
    if name == "pdfminer":
        return _via_pdfminer
    if name == "pypdf":
        return _via_pypdf
    raise NoExtractorAvailable(f"未知后端: {name}")


def _via_pymupdf(pdf: bytes) -> str:
    import fitz  # type: ignore[import-not-found]

    with fitz.open(stream=pdf, filetype="pdf") as doc:
        if doc.needs_pass:
            raise PdfTextError("PDF 已加密")
        return "\n".join(page.get_text("text") for page in doc)


def _via_pdfminer(pdf: bytes) -> str:
    from pdfminer.high_level import extract_text  # type: ignore[import-not-found]

    return extract_text(io.BytesIO(pdf))  # type: ignore[return-value]


def _via_pypdf(pdf: bytes) -> str:
    from pypdf import PdfReader  # type: ignore[import-not-found]

    reader = PdfReader(io.BytesIO(pdf))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise PdfTextError(f"PDF 已加密且无法以空密码打开: {exc}") from exc
    return "\n".join((page.extract_text() or "") for page in reader.pages)


__all__ = [
    "NoExtractorAvailable",
    "PdfTextError",
    "available_backends",
    "extract_text",
    "normalize",
]
