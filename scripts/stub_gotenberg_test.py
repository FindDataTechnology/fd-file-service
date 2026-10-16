#!/usr/bin/env python3
"""Office 分支验收（Gotenberg 桩）：`python3 scripts/stub_gotenberg_test.py`

为什么用桩：Gotenberg 真容器部署在 windows-laptop（任务 6.1，用户执行），
部署前 Office 分支（.docx → Gotenberg → PDF → 文本）从未被任何验收跑过。
本脚本用**本地桩**验证本服务侧的那一半契约——请求形态是否正确：

1. POST 到 ``/forms/libreoffice/convert``（路径与 upstream 一致）
2. 文件字段名 ``files``、filename 带真实扩展名（LibreOffice 按扩展名校验）
3. 响应非 PDF 凶铃（缺 %PDF 魔数）时**必须报转换失败**而非放过坏 PDF
4. 转换成功时产物落桶、留痕 status=ok、engine 标 gotenberg@<url>

桩不接受 Office 语义（不真转换），只回一份含标记文本的 PDF——它验的是
**"我们发对了请求、处理对了响应"**，不是 LibreOffice 的转换质量（那归部署后
的真容器验收）。
"""

from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BUCKET = f"fd-file-test-stub-{os.getpid()}"
DB_PATH = ROOT / "stub_test.db"
OWNER = "wire:stub_cust"
MARKER = "Office converted stub text"

#: 桩收到的请求留痕（供断言请求形态）。
captured: list[dict] = []
#: 桩本轮返回的 PDF；None → 返回非 PDF 凶铃（验证转换失败路径）。
respond_with_pdf: bytes | None = None

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, evidence: str = "") -> None:
    results.append((ok, name, evidence))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if evidence:
        for line in evidence.strip().splitlines():
            print(f"         {line}")


def make_pdf(text: str) -> bytes:
    stream = f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    n = len(objs) + 1
    out += f"xref\n0 {n}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    return out


class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # /health
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        cap = {"path": self.path, "content_type": content_type, "body_len": length}
        # 抓 multipart 里的 filename（不解析完整 multipart，找 filename="..." 即可）
        import re

        m = re.search(rb'filename="([^"]+)"', body)
        cap["filename"] = m.group(1).decode("utf-8", "replace") if m else None
        cap["has_files_field"] = b'name="files"' in body
        captured.append(cap)

        if respond_with_pdf is None:
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.end_headers()
            self.wfile.write(b"<html>not a pdf</html>")  # 200 + 非 PDF
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.end_headers()
        self.wfile.write(respond_with_pdf)

    def log_message(self, *args):  # 静音
        pass


def main() -> int:
    global respond_with_pdf
    for p in (DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
        if p.exists():
            p.unlink()

    # ---- 桩起在随机端口 ----
    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    stub_port = stub.server_address[1]
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    respond_with_pdf = make_pdf(MARKER)
    print(f"\n桩 Gotenberg: http://127.0.0.1:{stub_port}")

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

    port = 0
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    env = {
        **os.environ,
        "FILE_S3_BUCKET": BUCKET,
        "FILE_DATABASE_URL": f"sqlite:///{DB_PATH}",
        "FILE_PARSE_ENABLED": "1",
        "FILE_GOTENBERG_URL": f"http://127.0.0.1:{stub_port}",
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
    base = f"http://127.0.0.1:{port}"
    client = httpx.Client(trust_env=False, timeout=60, base_url=base)
    raw = httpx.Client(trust_env=False, timeout=60)

    def sql(q, args=()):
        con = sqlite3.connect(str(DB_PATH), timeout=5)
        try:
            return con.execute(q, args).fetchall()
        finally:
            con.close()

    def upload(data: bytes, mime: str, filename: str) -> str:
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
        return g["file_id"]

    def run_worker():
        return subprocess.run(
            [sys.executable, "scripts/parse_worker.py", "--once"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    try:
        for _ in range(60):
            try:
                if client.get("/health").status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)

        # ------------------------------------------------ 1 docx → 桩转换
        print("\n1. .docx 走 Gotenberg 分支（桩返回 PDF）")
        docx_bytes = b"PK\x03\x04 fake docx payload " + os.urandom(256)
        fid = upload(
            docx_bytes,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "report.docx",
        )
        run_worker()
        row = sql(
            "SELECT status, char_count, engine, text_key FROM file_parses "
            "WHERE file_id=? ORDER BY id DESC LIMIT 1",
            (fid,),
        )
        check(
            bool(row) and row[0][0] == "ok",
            "docx 经桩转换 + 抽取成功（status=ok）",
            f"status={row[0][0] if row else '-'} chars={row[0][1] if row else '-'}",
        )
        check(
            bool(row) and row[0][2].startswith("gotenberg@"),
            "engine 标注为 gotenberg@<url>（走的是转换分支，不是裸 PDF 分支）",
            f"engine={row[0][2] if row else '-'}",
        )
        if row and row[0][3]:
            text = (storage.get_object(row[0][3]) or b"").decode("utf-8", "replace")
            check(MARKER in text, "产物含桩 PDF 的文本标记", f"chars={len(text)}")

        # ------------------------------------- 2 请求形态（服务侧契约）
        print("\n2. 请求形态核对（与 upstream 契约一致）")
        cap = captured[-1] if captured else {}
        check(
            cap.get("path") == "/forms/libreoffice/convert",
            "POST 路径 = /forms/libreoffice/convert",
            f"path={cap.get('path')}",
        )
        check(
            cap.get("has_files_field") and cap.get("filename") == "report.docx",
            "multipart 字段名 files + filename 带真实扩展名",
            f"files 字段={cap.get('has_files_field')} filename={cap.get('filename')}",
        )

        # ---------------------------------- 3 桩返回非 PDF → 必须记为失败
        print("\n3. 桩返回 200+非PDF → 转换失败（凶铃校验生效）")
        respond_with_pdf = None
        fid2 = upload(docx_bytes + b"x", "application/msword", "evil.doc")
        run_worker()
        row2 = sql(
            "SELECT status, stage, error FROM file_parses WHERE file_id=? ORDER BY id DESC LIMIT 1",
            (fid2,),
        )
        check(
            bool(row2) and row2[0][0] == "failed" and row2[0][1] == "convert",
            "非 PDF 响应被拦在转换阶段（不流入抽取器）",
            f"status={row2[0][0] if row2 else '-'} stage={row2[0][1] if row2 else '-'} "
            f"err={(row2[0][2] or '')[:60] if row2 else '-'}",
        )
        d = client.get(f"/files/{fid2}/download-url", params={"owner": OWNER})
        check(d.status_code == 200, "该文件仍可取用（失败不毁文件）", f"{d.status_code}")

        # ------------------------------- 4 Gotenberg 不可达 → 快速失败
        print("\n4. Gotenberg 不可达 → convert 阶段失败并留痕")
        env2 = {**env, "FILE_GOTENBERG_URL": "http://127.0.0.1:9"}
        fid3 = upload(docx_bytes + b"y", "application/rtf", "unreachable.rtf")
        subprocess.run(
            [sys.executable, "scripts/parse_worker.py", "--once"],
            cwd=str(ROOT),
            env=env2,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        row3 = sql(
            "SELECT status, stage FROM file_parses WHERE file_id=? ORDER BY id DESC LIMIT 1",
            (fid3,),
        )
        check(
            bool(row3) and row3[0][0] == "failed" and row3[0][1] == "convert",
            "不可达记为 convert 失败",
            f"status={row3[0][0] if row3 else '-'} stage={row3[0][1] if row3 else '-'}",
        )
        # --once 语义：nack 立即重排队，同一轮运行内会把重试预算跑完进死信。
        # （生产常驻模式下会等待租约到期再重试；此处一轮耗尽属预期。）
        ev = sql("SELECT status, attempts, last_error FROM file_events WHERE file_id=?", (fid3,))
        check(
            bool(ev) and ev[0][0] == "dead" and ev[0][1] >= 2 and "Gotenberg" in (ev[0][2] or ""),
            "失败留痕可查（死信含 last_error）",
            f"status={ev[0][0] if ev else '-'} attempts={ev[0][1] if ev else '-'} "
            f"err={(ev[0][2] or '')[:50] if ev else '-'}",
        )

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        stub.shutdown()
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
    print(f"\n{'=' * 62}\nOffice 分支验收（桩）：{passed}/{len(results)} 通过\n{'=' * 62}")
    for ok, name, _ in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
