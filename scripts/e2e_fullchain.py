#!/usr/bin/env python3
"""全链端到端验收（变更任务 7.2）：`python3 scripts/e2e_fullchain.py`。

走完 spec 验收句的每一步并逐步骤留证据：

    上传 → 配额扣减 → 重复上传不增占用 → 文本抽取可检索 → 取用地址实效 → 软删后恢复

与 acceptance.py 的分工：acceptance 覆盖服务面（32 项，含越权/配额/引用计数
边界），本脚本覆盖**跨模块接缝**——事件发布、worker 消费、产物落桶、秒传文件
的解析取键。两处接缝缺陷（确认路由不发布事件 / 秒传按 file_id 现拼桶键）
都是 acceptance 的盲区，本脚本各含一条显式回归检查。

前提：真实 RustFS 可达（.env 凭据）；Gotenberg **不要求**——本脚本只用原生
PDF（pdfminer 抽取），Office 转换属于部署侧验收。本机代理坑见 acceptance.py。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BUCKET = f"fd-file-test-e2e-{os.getpid()}"
DB_PATH = ROOT / "e2e.db"
OWNER = "wire:e2e_cust"
MARKER = "Hello finddata file service E2E"


#: 最小可用 PDF（含可抽取文本）。手写而非引入 reportlab——验收脚本不添依赖。
def make_pdf(text: str) -> bytes:
    stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
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


results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, evidence: str = "") -> None:
    results.append((ok, name, evidence))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if evidence:
        for line in evidence.strip().splitlines():
            print(f"         {line}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sql(query: str, args: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(str(DB_PATH), timeout=5)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def main() -> int:
    for p in (DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
        if p.exists():
            p.unlink()

    port = free_port()
    env = {
        **os.environ,
        "FILE_S3_BUCKET": BUCKET,
        "FILE_QUOTA_FREE_BYTES": str(64 * 1024 * 1024),
        "FILE_DATABASE_URL": f"sqlite:///{DB_PATH}",
        "FILE_PARSE_ENABLED": "1",
        "PYTHONPATH": str(ROOT),
    }

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
    print(f"\n测试桶 {BUCKET}（harbor 等既有桶 untouched）")
    print(f"服务端口 127.0.0.1:{port}  库 {DB_PATH.name}\n")

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

    def put_and_confirm(data: bytes, mime: str, filename: str):
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
        if not g.get("dedup"):
            put = raw.put(g["upload_url"], content=data, headers={"Content-Type": mime})
            put.raise_for_status()
            cr = client.post(f"/files/{g['file_id']}/confirm", params={"owner": OWNER})
            cr.raise_for_status()
        return g

    try:
        # ---------------------------------------------------------- 0 就绪
        print("0. 服务就绪")
        up = False
        for _ in range(60):
            try:
                if client.get("/health").status_code == 200:
                    up = True
                    break
            except Exception:
                time.sleep(0.5)
        check(up, "服务启动且 /health 200")

        # -------------------------------------------------- 1 上传 + 配额
        print("\n1. 上传 → 直传 → 确认 → 配额扣减")
        pdf = make_pdf(MARKER)
        q0 = client.get("/quota", params={"owner": OWNER}).json()["used_bytes"]
        g = put_and_confirm(pdf, "application/pdf", "report.pdf")
        check(
            g["method"] == "PUT" and g["file_id"],
            "拿到上传凭据并直传成功",
            f"file_id={g['file_id']} method={g['method']}",
        )
        q1 = client.get("/quota", params={"owner": OWNER}).json()["used_bytes"]
        check(
            q1 - q0 == len(pdf),
            "确认后配额扣减 = 文件大小",
            f"used {q0} → {q1}（Δ={q1 - q0} = {len(pdf)} 字节）",
        )

        # ---- 1.5 事件发布（任务 5.1：确认后发出且字段完整）----
        rows = sql(
            "SELECT id, payload_json, status FROM file_events WHERE file_id=?", (g["file_id"],)
        )
        check(
            len(rows) == 1,
            "确认后发布 file.confirmed 事件（恰好一条）",
            f"rows={len(rows)} status={rows[0][2] if rows else '-'}",
        )
        if rows:
            payload = json.loads(rows[0][1])
            need = ("file_id", "owner", "sha256", "mime", "storage_key", "event_type", "ts")
            missing = [k for k in need if not payload.get(k)]
            check(
                not missing,
                "事件字段完整（file_id/owner/sha256/mime/storage_key/ts）",
                f"missing={missing or '无'} event_type={payload.get('event_type')}",
            )

        # ------------------------------------------------- 2 去重不增占用
        print("\n2. 重复上传不增占用（秒传）")
        g2 = put_and_confirm(pdf, "application/pdf", "report-copy.pdf")
        q2 = client.get("/quota", params={"owner": OWNER}).json()["used_bytes"]
        check(
            g2.get("dedup") is True and g2["file_id"] != g["file_id"],
            "二次上传返回 dedup=true 且为新 file_id",
            f"file_id={g2['file_id']} dedup={g2.get('dedup')}",
        )
        check(q2 == q1, "配额不增（字节只一份）", f"used {q1} → {q2}")
        check(
            g2["storage_key"] == g["storage_key"],
            "秒传记录指向原文件的桶键（解析取键的回归锚点）",
            f"key={g2['storage_key']}",
        )
        rows2 = sql("SELECT id FROM file_events WHERE file_id=?", (g2["file_id"],))
        check(
            len(rows2) == 1,
            "秒传文件同样发布事件（缺陷回归：确认路径全覆盖）",
            f"rows={len(rows2)}",
        )

        # ------------------------------------------- 3 畸形文件（5.3 材料）
        print("\n3. 畸形 PDF 上传（为 5.3 失败留痕备料）")
        bad = b"%PDF-1.4 %%EOF truncated garbage" + os.urandom(2048)
        gbad = put_and_confirm(bad, "application/pdf", "broken.pdf")
        check(
            gbad["file_id"] != "",
            "畸形文件正常入库（解析失败是它的事）",
            f"file_id={gbad['file_id']}",
        )

        # ------------------------------------------------- 4 worker 消费
        print("\n4. 解析 worker 消费队列（--once）")
        w = subprocess.run(
            [sys.executable, "scripts/parse_worker.py", "--once"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        tail = "\n".join((w.stdout or "").strip().splitlines()[-8:])
        check(w.returncode == 0, "worker 跑完且退出码 0", tail)

        # ------------------------------------------------- 5 文本抽取
        print("\n5. 文本抽取产物可检索")
        prows = sql(
            "SELECT status, char_count, text_key, engine FROM file_parses "
            "WHERE file_id=? ORDER BY id DESC LIMIT 1",
            (g["file_id"],),
        )
        ok_row = prows and prows[0][0] == "ok"
        check(
            bool(ok_row) and prows[0][1] > 0,
            "原件解析 ok 且有字数",
            f"status={prows[0][0] if prows else '-'} chars={prows[0][1] if prows else '-'} "
            f"engine={prows[0][3] if prows else '-'}",
        )
        if ok_row:
            text = storage.get_object(prows[0][2])
            got = (text or b"").decode("utf-8", "replace")
            check(
                MARKER in got,
                "产物可从桶内取回且含原文标记",
                f"key={prows[0][2]} chars={len(got)} 含标记={MARKER in got}",
            )

        prows2 = sql(
            "SELECT status, char_count FROM file_parses WHERE file_id=? ORDER BY id DESC LIMIT 1",
            (g2["file_id"],),
        )
        check(
            bool(prows2) and prows2[0][0] == "ok",
            "秒传文件也抽出文本（缺陷回归：按事件 storage_key 取字节）",
            f"status={prows2[0][0] if prows2 else '-'} chars={prows2[0][1] if prows2 else '-'}",
        )

        # --------------------------------------------- 6 失败留痕不毁文件
        print("\n6. 解析失败留痕且文件可用（任务 5.3）")
        frows = sql(
            "SELECT status, stage, error FROM file_parses WHERE file_id=? ORDER BY id DESC LIMIT 1",
            (gbad["file_id"],),
        )
        check(
            bool(frows) and frows[0][0] == "failed",
            "畸形文件解析失败且留痕",
            f"status={frows[0][0] if frows else '-'} stage={frows[0][1] if frows else '-'} "
            f"err={(frows[0][2] or '')[:60] if frows else '-'}",
        )
        ev_bad = sql(
            "SELECT status, attempts, last_error FROM file_events WHERE file_id=?",
            (gbad["file_id"],),
        )
        check(
            bool(ev_bad) and ev_bad[0][0] == "dead" and ev_bad[0][1] >= 2,
            "失败事件重试预算耗尽后进死信（dead，可查）",
            f"status={ev_bad[0][0] if ev_bad else '-'} attempts={ev_bad[0][1] if ev_bad else '-'} "
            f"last_error={(ev_bad[0][2] or '')[:50] if ev_bad else '-'}",
        )
        d_bad = client.get(f"/files/{gbad['file_id']}/download-url", params={"owner": OWNER})
        check(d_bad.status_code == 200, "失败文件仍可取用（不破坏可用性）", f"{d_bad.status_code}")

        # --------------------------------------------- 7 取用地址实效
        print("\n7. 取用地址实效")
        d = client.get(
            f"/files/{g['file_id']}/download-url", params={"owner": OWNER, "ttl": 2}
        ).json()
        got = raw.get(d["url"])
        check(
            got.status_code == 200 and got.content == pdf,
            "ttl=2 的地址立即可取且字节一致",
            f"GET {got.status_code} len={len(got.content)} 一致={got.content == pdf}",
        )
        time.sleep(3.5)
        expired = raw.get(d["url"])
        check(
            expired.status_code in (400, 403),
            "3.5s 后同一地址被拒（实效生效）",
            f"GET {expired.status_code} body={expired.text[:80]}",
        )

        # --------------------------------------------- 8 软删 → 恢复
        print("\n8. 软删后恢复")
        r = client.delete(f"/files/{g['file_id']}", params={"owner": OWNER})
        lst = client.get("/files", params={"owner": OWNER}).json()
        hidden = all(i["file_id"] != g["file_id"] for i in lst["items"])
        check(
            r.status_code == 200 and hidden,
            "删除后从列表隐藏",
            f"{r.status_code} status={r.json()['status']} 列表隐藏={hidden}",
        )
        rr = client.post(f"/files/{g['file_id']}/restore", params={"owner": OWNER})
        lst2 = client.get("/files", params={"owner": OWNER}).json()
        back = any(i["file_id"] == g["file_id"] for i in lst2["items"])
        check(
            rr.status_code == 200 and back,
            "保留期内恢复后重新可见",
            f"{rr.status_code} status={rr.json()['status']} 可见={back}",
        )
        d2 = client.get(
            f"/files/{g['file_id']}/download-url", params={"owner": OWNER, "ttl": 60}
        ).json()
        got2 = raw.get(d2["url"])
        check(
            got2.status_code == 200 and got2.content == pdf,
            "恢复后取回字节完好",
            f"GET {got2.status_code} len={len(got2.content)}",
        )

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        client.close()
        raw.close()
        print("\n清理…")
        try:
            n = storage.delete_all()
            storage.delete_bucket()
            print(f"  删除 {n} 个对象与桶 {BUCKET}；RustFS 剩余桶：{storage.list_buckets()}")
        except Exception as e:
            print(f"  清理失败（需手工处理）：{e}")
        if DB_PATH.exists():
            DB_PATH.unlink()

    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n{'=' * 62}\n全链验收：{passed}/{len(results)} 通过\n{'=' * 62}")
    for ok, name, _ in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
