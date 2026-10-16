#!/usr/bin/env python3
"""线上全链自检（在 file-api 容器内跑）：生产桶 fd-file 真实链路。

上传真 docx → 确认 → worker 解析 → 产物可取回 → 取用地址取字节 → 清理。
"""

import hashlib
import sqlite3
import sys
import time
import zipfile
from io import BytesIO

import httpx

BASE = "http://127.0.0.1:8731"
OWNER = "ops:online_selftest"
MARKER = "ONLINE SELF TEST 20261017"
DB = "/data/fd_file_service.db"


def make_docx(text: str) -> bytes:
    ct = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def sql(q, args=()):
    con = sqlite3.connect(DB, timeout=10)
    try:
        return con.execute(q, args).fetchall()
    finally:
        con.close()


results = []


def check(ok, name, ev=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {ev}" if ev else ""))


c = httpx.Client(trust_env=False, timeout=120, base_url=BASE)
raw = httpx.Client(trust_env=False, timeout=120)

data = make_docx(MARKER)
mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
sha = hashlib.sha256(data).hexdigest()

r = c.post(
    "/files/upload-url",
    json={
        "owner": OWNER,
        "size": len(data),
        "mime": mime,
        "sha256": sha,
        "filename": "online.docx",
        "use_post_policy": False,
    },
)
r.raise_for_status()
g = r.json()
check(True, "upload-url 签发", f"file_id={g['file_id']}")

put = raw.put(g["upload_url"], content=data, headers={"Content-Type": mime})
check(put.status_code == 200, "字节直传 RustFS（生产桶 fd-file）", f"PUT {put.status_code}")

cr = c.post(f"/files/{g['file_id']}/confirm", params={"owner": OWNER})
check(cr.status_code == 200, "确认入账", f"{cr.status_code} status={cr.json()['status']}")

row = sql("SELECT id,status FROM file_events WHERE file_id=?", (g["file_id"],))
check(bool(row) and row[0][1] == "pending", "事件已发布（等 worker 消费）", f"events={row}")

# 等 worker 消费（常驻轮询 5s 间隔）
ok_parse = False
for _ in range(40):
    p = sql(
        "SELECT status, char_count, text_key FROM file_parses WHERE file_id=? ORDER BY id DESC LIMIT 1",
        (g["file_id"],),
    )
    if p and p[0][0] in ("ok", "empty", "failed"):
        ok_parse = p[0][0] == "ok" and p[0][1] > 0
        check(
            ok_parse,
            "worker 消费 + 真 LibreOffice 转换 + 文本抽取",
            f"status={p[0][0]} chars={p[0][1]} key={(p[0][2] or '')[-30:]}",
        )
        break
    time.sleep(3)
if not ok_parse:
    check(False, "worker 消费 + 抽取", "40 轮未见终态")

# 产物可取回（经取用地址）
if ok_parse:
    d = c.get(f"/files/{g['file_id']}/download-url", params={"owner": OWNER, "ttl": 120}).json()
    got = raw.get(d["url"])
    check(
        got.status_code == 200 and got.content == data,
        "取用地址取回原文一致",
        f"GET {got.status_code} len={len(got.content)}",
    )

# 清理：软删 → 强制过期 → purge（生产桶不留测试数据）
c.delete(f"/files/{g['file_id']}", params={"owner": OWNER})
con = sqlite3.connect(DB)
con.execute("UPDATE file_assets SET purge_after='2000-01-01' WHERE file_id=?", (g["file_id"],))
con.commit()
con.close()
pr = c.post("/maintenance/purge", params={"owner": OWNER}).json()
check(
    pr["assets_purged"] >= 1,
    "测试文件已清理（purge 回收）",
    f"purged={pr['assets_purged']} bytes={pr['bytes_freed']}",
)

passed = sum(results)
print(f"\n线上全链自检：{passed}/{len(results)} 通过")
sys.exit(0 if passed == len(results) else 1)
