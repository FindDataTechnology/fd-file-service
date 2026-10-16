#!/usr/bin/env python3
"""真 Gotenberg 活体验收：`python3 scripts/live_gotenberg_test.py`

与 `stub_gotenberg_test.py` 的分工：
- 桩测试（离线可跑）：验**服务侧契约**——请求路径/字段名/凶铃拦截/留痕。
- 本脚本（需真部署）：验**真 LibreOffice 转换**——真 .docx 走真转换成 PDF，
  再抽文本，验证整条 Office 链路在真实部署上端到端可用。

跑的前提：Gotenberg 真容器已在 `FILE_GOTENBERG_URL`（缺省 .env 的 3010）
可达、RustFS 可达。

覆盖两条：纯 ASCII 与中文混排（后者同时验证 LibreOffice 转换时的字体嵌入
与 pdfminer 对转换产物的 CJK 抽取——这是桩测试不可能覆盖的真实风险面）。
"""

from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import subprocess
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BUCKET = f"fd-file-test-live-{os.getpid()}"
DB_PATH = ROOT / "live_test.db"
OWNER = "wire:live_cust"

ASCII_MARKER = "GOTENBERG LIVE MARKER 20261017"
CJK_MARKER = "寻数科技真容器验收"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, evidence: str = "") -> None:
    results.append((ok, name, evidence))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if evidence:
        for line in evidence.strip().splitlines():
            print(f"         {line}")


def make_docx(*paragraphs: str) -> bytes:
    """手搓最小合法 .docx（OOXML zip）——不引 python-docx 依赖。

    LibreOffice 对最小 OOXML 容错良好；三个部件即可：Content_Types、
    根 _rels、word/document.xml。
    """
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def main() -> int:
    for p in (DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
        if p.exists():
            p.unlink()

    # ---- 0 前置：真 Gotenberg 健康（先验，失败就不白跑全链）----
    from fd_file_service.parsers.gotenberg import Client as GBClient

    gb = GBClient.from_env()
    print(f"\n真 Gotenberg: {gb.base_url}")
    ok, detail = gb.health()
    check(ok, f"真容器健康（{gb.base_url}/health）", detail[:150])
    if not ok:
        print("\nGotenberg 不可达——先确认部署；本脚本退出。")
        return 1

    from fd_file_service.config import settings
    from fd_file_service.storage import Storage

    storage = Storage(
        endpoint=settings.file_s3_endpoint,
        access_key=settings.file_s3_access_key,
        secret_key=settings.file_s3_secret_key,
        bucket=BUCKET,
        region=settings.file_s3_region,
        addressing_style=settings.file_s3_addressing_style,
    )
    storage.ensure_bucket()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {
        **os.environ,
        "FILE_S3_BUCKET": BUCKET,
        "FILE_DATABASE_URL": f"sqlite:///{DB_PATH}",
        "FILE_PARSE_ENABLED": "1",
        "FILE_GOTENBERG_URL": gb.base_url,
        "PYTHONPATH": str(ROOT),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "fd_file_service.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    client = httpx.Client(trust_env=False, timeout=180, base_url=f"http://127.0.0.1:{port}")
    raw = httpx.Client(trust_env=False, timeout=180)

    def sql(q, args=()):
        con = sqlite3.connect(str(DB_PATH), timeout=10)
        try:
            return con.execute(q, args).fetchall()
        finally:
            con.close()

    def upload_and_parse(data: bytes, filename: str) -> tuple[str, subprocess.CompletedProcess]:
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        sha = hashlib.sha256(data).hexdigest()
        r = client.post(
            "/files/upload-url",
            json={
                "owner": OWNER,
                "size": len(data),
                "mime": mime,
                "sha256": sha,
                "filename": filename,
                "use_post_policy": False,
            },
        )
        r.raise_for_status()
        g = r.json()
        raw.put(g["upload_url"], content=data, headers={"Content-Type": mime}).raise_for_status()
        client.post(f"/files/{g['file_id']}/confirm", params={"owner": OWNER}).raise_for_status()
        w = subprocess.run(
            [sys.executable, "scripts/parse_worker.py", "--once"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        return g["file_id"], w

    try:
        for _ in range(60):
            try:
                if client.get("/health").status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)

        # -------------------------------------------- 1 纯 ASCII docx
        print("\n1. 真 .docx（ASCII）→ 真 LibreOffice → PDF → 文本")
        fid, w = upload_and_parse(make_docx(ASCII_MARKER, "Second line here."), "live-ascii.docx")
        row = sql(
            "SELECT status, char_count, engine, text_key FROM file_parses "
            "WHERE file_id=? ORDER BY id DESC LIMIT 1",
            (fid,),
        )
        if not row or row[0][0] != "ok":
            wlog = "\n".join((w.stdout or "").strip().splitlines()[-6:])
            check(False, "ASCII docx 真转换+抽取成功", wlog[-400:])
        else:
            check(True, "ASCII docx 真转换+抽取成功", f"chars={row[0][1]} engine={row[0][2]}")
            text = (storage.get_object(row[0][3]) or b"").decode("utf-8", "replace")
            check(ASCII_MARKER in text, "抽取文本含 ASCII 标记", f"chars={len(text)}")
            check(
                row[0][2].startswith("gotenberg@"),
                "engine 标注 gotenberg@<真容器地址>",
                f"engine={row[0][2]}",
            )

        # -------------------------------------------- 2 中文混排 docx
        print("\n2. 真 .docx（中文混排）→ CJK 抽取（字体嵌入与 ToUnicode 风险面）")
        fid2, w2 = upload_and_parse(
            make_docx(CJK_MARKER, f"ASCII part: {ASCII_MARKER}", "第三段：混合 2026 数字。"),
            "live-cjk.docx",
        )
        row2 = sql(
            "SELECT status, char_count, text_key, engine FROM file_parses "
            "WHERE file_id=? ORDER BY id DESC LIMIT 1",
            (fid2,),
        )
        if not row2 or row2[0][0] != "ok":
            wlog = "\n".join((w2.stdout or "").strip().splitlines()[-6:])
            check(False, "中文 docx 真转换+抽取成功", wlog[-400:])
        else:
            check(True, "中文 docx 真转换+抽取成功", f"chars={row2[0][1]} engine={row2[0][3]}")
            text2 = (storage.get_object(row2[0][2]) or b"").decode("utf-8", "replace")
            check(CJK_MARKER in text2, "抽取文本含中文标记（CJK 未丢）", f"chars={len(text2)}")
            check(ASCII_MARKER in text2, "同文含 ASCII 标记（混排完整）")

        # ---------------------------------- 3 垃圾字节的如实记录（真容器宽容行为）
        print("\n3. 垃圾字节 .docx → 如实记录（真 LibreOffice 宽容，当文本转 PDF）")
        bad = b"not an office file at all " + os.urandom(128)
        fid3, _ = upload_and_parse(bad, "fake.docx")
        row3 = sql(
            "SELECT status, stage, char_count, text_key FROM file_parses "
            "WHERE file_id=? ORDER BY id DESC LIMIT 1",
            (fid3,),
        )
        # 真容器对任意字节宽容（实测：垃圾也转得出 PDF）→ 记录 ok 是**如实**的。
        # 断言改为诚实契约：终态合法、ok 时产物可取回、文件始终可用。
        st = row3[0][0] if row3 else "-"
        check(
            bool(row3) and st in ("ok", "empty", "failed"),
            "垃圾输入得到合法终态（如实记录真实结果）",
            f"status={st} chars={row3[0][2] if row3 else '-'} stage={row3[0][1] if row3 else '-'}",
        )
        if row3 and st == "ok" and row3[0][3]:
            got = (storage.get_object(row3[0][3]) or b"").decode("utf-8", "replace")
            check(
                len(got) == row3[0][2],
                "char_count 与产物实际长度一致（记录诚实）",
                f"recorded={row3[0][2]} actual={len(got)}",
            )
        d = client.get(f"/files/{fid3}/download-url", params={"owner": OWNER})
        check(d.status_code == 200, "无论解析结果，文件始终可取用", f"{d.status_code}")

        # ----------------------------- 4 无文本 PDF → empty（影印件设计语义）
        print("\n4. 无文本 PDF → status=empty（影印件语义：不是 failed、不重试）")
        # 合法 PDF、页面无文本对象——模拟扫描件。
        empty_pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj\n"
            b"trailer << /Size 4 /Root 1 0 R >>\n%%EOF\n"
        )
        fid4, _ = upload_and_parse(empty_pdf, "scan.pdf")
        row4 = sql(
            "SELECT status, stage, error FROM file_parses WHERE file_id=? ORDER BY id DESC LIMIT 1",
            (fid4,),
        )
        check(
            bool(row4) and row4[0][0] == "empty",
            "空文本记为 empty 而非 failed（扫描件不误进死信）",
            f"status={row4[0][0] if row4 else '-'} err={(row4[0][2] or '')[:60] if row4 else '-'}",
        )
        ev4 = sql("SELECT status FROM file_events WHERE file_id=?", (fid4,))
        check(
            bool(ev4) and ev4[0][0] == "done",
            "empty 事件直接 ack（不耗重试预算）",
            f"event status={ev4[0][0] if ev4 else '-'}",
        )

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        client.close()
        raw.close()
        try:
            n = storage.delete_all()
            storage.delete_bucket()
            print(f"\n清理：删除 {n} 个对象与桶 {BUCKET}；剩余桶 {storage.list_buckets()}")
        except Exception as e:
            print(f"清理失败：{e}")
        if DB_PATH.exists():
            DB_PATH.unlink()

    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n{'=' * 62}\n真 Gotenberg 活体验收：{passed}/{len(results)} 通过\n{'=' * 62}")
    for ok, name, _ in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
