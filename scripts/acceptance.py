#!/usr/bin/env python3
"""端到端验收（真实 RustFS）：`python3 scripts/acceptance.py`。

做什么
------
1. 在真实 RustFS 建一个 ``fd-file-test-`` 前缀的测试桶（**不碰 harbor**）
2. 起一个子进程 HTTP 服务（随机高端口，不对外暴露）
3. 逐条跑验收项，打勾打叉并打印证据
4. 收尾删空并删除测试桶

注意：本机 httpx 会读系统代理（``127.0.0.1:7892`` 已死）导致请求挂死，
故所有客户端一律 ``trust_env=False``——这是本机环境事实，不是服务行为。
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:  # 只供注解；真实导入在 main() 里（需先就位 sys.path）
    from fd_file_service.storage import Storage

# ---- 测试参数（覆盖 .env 的桶与配额，其余沿用）----
BUCKET = f"fd-file-test-acceptance-{os.getpid()}"
QUOTA = 2 * 1024 * 1024  # 2MB：让"配额超限"可真实触发
SOFT_DELETE_DAYS = 30
DB_PATH = ROOT / "acceptance.db"

OWNER_A = "wire:cust_A"
OWNER_B = "wire:cust_B"

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


def main() -> int:
    if DB_PATH.exists():
        DB_PATH.unlink()

    port = free_port()
    env = {
        **os.environ,
        "FILE_S3_BUCKET": BUCKET,
        "FILE_QUOTA_FREE_BYTES": str(QUOTA),
        "FILE_SOFT_DELETE_DAYS": str(SOFT_DELETE_DAYS),
        "FILE_DATABASE_URL": f"sqlite:///{DB_PATH}",
        "PYTHONPATH": str(ROOT),
    }

    # ---- 建桶（服务启动即会 ensure_bucket，这里先显式建以便收尾一定删得掉）----
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
    print(
        f"\n测试桶 {BUCKET}（既有桶 untouched: "
        f"{[b for b in storage.list_buckets() if b != BUCKET]}）"
    )
    print(f"服务端口 127.0.0.1:{port}\n")

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
    raw = httpx.Client(trust_env=False, timeout=60)  # 直连存储用（不过服务）

    try:
        _wait_up(client)
        _run_checks(client, raw, storage)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        client.close()
        raw.close()
        _cleanup(storage)

    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n{'=' * 62}\n验收结果：{passed}/{len(results)} 通过\n{'=' * 62}")
    for ok, name, _ in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if DB_PATH.exists():
        DB_PATH.unlink()
    return 0 if passed == len(results) else 1


def _wait_up(client: httpx.Client) -> None:
    for _ in range(60):
        try:
            if client.get("/health").status_code == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("服务未起来")


def _upload_flow(
    client: httpx.Client,
    raw: httpx.Client,
    owner: str,
    data: bytes,
    mime: str = "application/pdf",
    filename: str = "a.pdf",
    confirm: bool = True,
):
    """申请凭据 → 直传真实字节到 RustFS →（可选）确认。"""
    sha = hashlib.sha256(data).hexdigest()
    r = client.post(
        "/files/upload-url",
        json={
            "owner": owner,
            "size": len(data),
            "mime": mime,
            "sha256": sha,
            "filename": filename,
            "use_post_policy": False,
        },
    )
    r.raise_for_status()
    g = r.json()
    if g.get("dedup"):
        return g, None
    put = raw.put(g["upload_url"], content=data, headers={"Content-Type": mime})
    put.raise_for_status()
    if confirm:
        cr = client.post(f"/files/{g['file_id']}/confirm", params={"owner": owner})
        cr.raise_for_status()
    return g, put


def _run_checks(client: httpx.Client, raw: httpx.Client, storage: Storage) -> None:
    print("1. 健康检查")
    r = client.get("/health")
    check(r.status_code == 200, "GET /health 返回 200", f"{r.status_code} {r.text}")

    # ---------------------------------------------------------------- 2 上传
    print("\n2. 上传 → 直传 → 确认 → 出现在列表")
    data_a = b"%PDF-1.4 acceptance-A " + os.urandom(2048)
    g, put = _upload_flow(client, raw, OWNER_A, data_a, filename="a.pdf")
    check(
        put is not None and put.status_code == 200,
        "字节直传真实 RustFS 成功（不经服务进程）",
        f"PUT {put.status_code}",
    )

    in_bucket = storage.head(g["storage_key"])
    check(
        in_bucket is not None and in_bucket["ContentLength"] == len(data_a),
        "桶内对象存在且大小一致",
        f"key={g['storage_key']} len={in_bucket and in_bucket['ContentLength']}",
    )

    lst = client.get("/files", params={"owner": OWNER_A}).json()
    hit = [i for i in lst["items"] if i["file_id"] == g["file_id"]]
    check(
        bool(hit) and hit[0]["status"] == "live",
        "确认后出现在列表且状态 live",
        f"count={lst['count']} status={hit and hit[0]['status']}",
    )

    q = client.get("/quota", params={"owner": OWNER_A}).json()
    check(
        q["used_bytes"] == len(data_a),
        "确认后计入配额",
        f"used={q['used_bytes']} limit={q['limit_bytes']}",
    )

    # ------------------------------------------------------------ 3 不确认
    print("\n3. 传而不确认 → 列表无、配额不变")
    before = client.get("/quota", params={"owner": OWNER_A}).json()["used_bytes"]
    g2, put2 = _upload_flow(
        client,
        raw,
        OWNER_A,
        b"%PDF-1.4 ghost" + os.urandom(1024),
        filename="ghost.pdf",
        confirm=False,
    )
    after = client.get("/quota", params={"owner": OWNER_A}).json()["used_bytes"]
    lst2 = client.get("/files", params={"owner": OWNER_A}).json()
    check(put2.status_code == 200, "字节已传到存储（只是未确认）", f"PUT {put2.status_code}")
    check(after == before, "配额未变", f"before={before} after={after}")
    check(
        all(i["file_id"] != g2["file_id"] for i in lst2["items"]),
        "列表中无此文件",
        f"count={lst2['count']}（不含 ghost）",
    )

    # ------------------------------------------------------------ 4 域隔离
    print("\n4. owner 域隔离")
    data_b = b"%PDF-1.4 acceptance-B " + os.urandom(1024)
    gb, _ = _upload_flow(client, raw, OWNER_B, data_b, filename="b.pdf")
    la = client.get("/files", params={"owner": OWNER_A}).json()
    lb = client.get("/files", params={"owner": OWNER_B}).json()
    check(
        all(i["file_id"] != g["file_id"] for i in lb["items"])
        and all(i["file_id"] != gb["file_id"] for i in la["items"]),
        "A 列表无 B 的文件，B 列表无 A 的文件",
        f"A={len(la['items'])} 项, B={len(lb['items'])} 项，互不交叉",
    )
    ra = client.get(f"/files/{gb['file_id']}/download-url", params={"owner": OWNER_A})
    check(
        ra.status_code == 404,
        "A 取用 B 的文件 → 404（不泄漏存在性）",
        f"{ra.status_code} {ra.text[:80]}",
    )
    qa = client.get("/quota", params={"owner": OWNER_A}).json()
    qb = client.get("/quota", params={"owner": OWNER_B}).json()
    check(
        qa["used_bytes"] == len(data_a) and qb["used_bytes"] == len(data_b),
        "配额按 owner 各自计算",
        f"A={qa['used_bytes']}（仅 A 的文件）, B={qb['used_bytes']}（仅 B 的文件）",
    )

    # -------------------------------------------------- 4b 列表过滤（任务 4.1）
    print("\n4b. 列表过滤：类型 / 前缀 / 时间 / 分页计数")
    gp, _ = _upload_flow(
        client,
        raw,
        OWNER_A,
        b"%PDF-1.4 with-image " + os.urandom(512),
        mime="image/png",
        filename="pixel.png",
    )
    only_pdf = client.get("/files", params={"owner": OWNER_A, "mime": "application/pdf"}).json()
    check(
        all(i["mime"] == "application/pdf" for i in only_pdf["items"])
        and all(i["file_id"] != gp["file_id"] for i in only_pdf["items"]),
        "mime 过滤：只返回 PDF，png 不在其中",
        f"count={only_pdf['count']} mimes={sorted({i['mime'] for i in only_pdf['items']})}",
    )
    only_img = client.get("/files", params={"owner": OWNER_A, "mime_prefix": "image/"}).json()
    check(
        [i["file_id"] for i in only_img["items"]] == [gp["file_id"]],
        "mime_prefix 过滤：image/ 只命中 png",
        f"count={only_img['count']} file_id={[i['file_id'][:12] for i in only_img['items']]}",
    )
    fut = client.get("/files", params={"owner": OWNER_A, "since": "2999-01-01T00:00:00Z"}).json()
    check(
        fut["count"] == 0 and fut["total"] == 0,
        "since 过滤：未来时刻 → 空集（分页计数同步为 0）",
        f"count={fut['count']} total={fut['total']}",
    )
    page1 = client.get("/files", params={"owner": OWNER_A, "limit": 1}).json()
    check(
        page1["count"] == 1 and page1["total"] >= 2 and page1["has_more"] is True,
        "limit=1 分页：count=1 但 total 为全量、has_more 为真",
        f"count={page1['count']} total={page1['total']} has_more={page1['has_more']}",
    )
    bad = client.get("/files", params={"owner": OWNER_A, "since": "not-a-date"})
    check(
        bad.status_code == 400,
        "非法时间格式报 400（不静默忽略）",
        f"{bad.status_code} {bad.text[:70]}",
    )
    # 收尾：png 移除，避免影响后续配额断言
    client.delete(f"/files/{gp['file_id']}", params={"owner": OWNER_A})
    _force_expire(DB_PATH, [gp["file_id"]])
    client.post("/maintenance/purge", params={"owner": OWNER_A})

    # -------------------------------------------------------------- 5 去重
    print("\n5. 内容去重（同 owner 同 sha256 二次上传）")
    before_q = client.get("/quota", params={"owner": OWNER_A}).json()["used_bytes"]
    before_keys = len(storage.list_keys())
    g3, put3 = _upload_flow(client, raw, OWNER_A, data_a, filename="a-copy.pdf")
    after_q = client.get("/quota", params={"owner": OWNER_A}).json()["used_bytes"]
    after_keys = len(storage.list_keys())
    check(g3["dedup"] is True, "二次上传返回 dedup=true", f"method={g3['method']}")
    check(
        g3["file_id"] != g["file_id"],
        "返回新 file_id",
        f"old={g['file_id'][:12]}… new={g3['file_id'][:12]}…",
    )
    check(put3 is None, "未再产生第二次字节传输", "无 PUT 发生")
    check(after_q == before_q, "配额未增加", f"before={before_q} after={after_q}")
    check(
        after_keys == before_keys,
        "桶内对象数未增（字节只一份）",
        f"before={before_keys} after={after_keys}",
    )

    # -------------------------------------------------------------- 6 配额
    print("\n6. 配额超限被拒")
    q_now = client.get("/quota", params={"owner": OWNER_A}).json()
    too_big = q_now["limit_bytes"] - q_now["used_bytes"] + 1
    big = os.urandom(too_big)
    r = client.post(
        "/files/upload-url",
        json={
            "owner": OWNER_A,
            "size": len(big),
            "mime": "application/pdf",
            "sha256": hashlib.sha256(big).hexdigest(),
            "filename": "big.pdf",
        },
    )
    body = r.json()["error"]
    check(
        r.status_code == 413 and body["code"] == "quota_exceeded",
        "超限申请被拒（4xx）",
        f"{r.status_code} {body['code']}",
    )
    check(
        {"used_bytes", "limit_bytes", "requested_bytes"} <= set(body),
        "报文含当前用量 / 上限 / 所需大小",
        f"used={body['used_bytes']} limit={body['limit_bytes']} "
        f"requested={body['requested_bytes']}",
    )
    # 不超限的大小应放行
    ok_size = q_now["limit_bytes"] - q_now["used_bytes"]
    r2 = client.post(
        "/files/upload-url",
        json={
            "owner": OWNER_A,
            "size": max(1, ok_size),
            "mime": "application/pdf",
            "sha256": hashlib.sha256(b"fit").hexdigest(),
            "filename": "fit.pdf",
        },
    )
    check(r2.status_code == 200, "恰好用满（不超限）的申请放行", f"{r2.status_code}")

    # ---------------------------------------------------------- 7 软删/恢复
    print("\n7. 软删除 → 列表隐藏 → 保留期内恢复")
    r = client.delete(f"/files/{g['file_id']}", params={"owner": OWNER_A})
    check(
        r.status_code == 200 and r.json()["status"] == "deleted",
        "删除返回 deleted 状态",
        f"{r.status_code} status={r.json()['status']}",
    )
    lst3 = client.get("/files", params={"owner": OWNER_A}).json()
    check(
        all(i["file_id"] != g["file_id"] for i in lst3["items"]),
        "已从列表隐藏",
        f"count={lst3['count']}（不含已删项）",
    )
    trash = client.get("/files/trash", params={"owner": OWNER_A}).json()
    check(
        any(i["file_id"] == g["file_id"] for i in trash["items"]),
        "出现在回收站（保留期内）",
        f"trash count={trash['count']}",
    )
    dl = client.get(f"/files/{g['file_id']}/download-url", params={"owner": OWNER_A})
    check(dl.status_code == 404, "已删文件不可取用", f"{dl.status_code}")

    rr = client.post(f"/files/{g['file_id']}/restore", params={"owner": OWNER_A})
    check(
        rr.status_code == 200 and rr.json()["status"] == "live",
        "保留期内恢复成功",
        f"{rr.status_code} status={rr.json()['status']}",
    )
    lst4 = client.get("/files", params={"owner": OWNER_A}).json()
    check(
        any(i["file_id"] == g["file_id"] for i in lst4["items"]),
        "恢复后重新出现在列表",
        f"count={lst4['count']}",
    )

    # 取用地址有实效 + 内容一致
    d = client.get(
        f"/files/{g['file_id']}/download-url", params={"owner": OWNER_A, "ttl": 120}
    ).json()
    got = raw.get(d["url"])
    check(
        got.status_code == 200 and got.content == data_a,
        "取用地址可取回且字节与原文一致",
        f"GET {got.status_code} len={len(got.content)} sha_ok={got.content == data_a}",
    )
    check(
        "X-Amz-Expires" in d["url"] or "Expires" in d["url"],
        "取用地址带有效期参数（非永久公开地址）",
        f"ttl={d['expires_in']}s",
    )

    # ------------------------------------------------- 8 保留期满释放（引用计数）
    print("\n8. 保留期满释放 + 引用计数")
    # 8a 保留期未到 → 不释放（软删的记录还在桶里）
    client.delete(f"/files/{g3['file_id']}", params={"owner": OWNER_A})  # 去重副本
    pr = client.post("/maintenance/purge", params={"owner": OWNER_A}).json()
    check(
        pr["assets_purged"] == 0,
        "保留期未到 → 不释放（字节仍在，可恢复）",
        f"scanned={pr['scanned']} purged={pr['assets_purged']}",
    )
    # 8b 引用计数：副本到期被清除，但原件（g，live）仍引用同一字节 → 字节必须留着
    _force_expire(DB_PATH, [g3["file_id"]])
    pr2 = client.post("/maintenance/purge", params={"owner": OWNER_A}).json()
    check(
        pr2["assets_purged"] == 1 and pr2["objects_purged"] == 0,
        "还有一条存活引用 → 字节不删（引用计数防误删）",
        f"assets_purged={pr2['assets_purged']} objects_purged={pr2['objects_purged']} "
        f"bytes_freed={pr2['bytes_freed']}",
    )
    after = client.get(
        f"/files/{g['file_id']}/download-url", params={"owner": OWNER_A, "ttl": 120}
    ).json()
    got2 = raw.get(after["url"])
    check(
        got2.status_code == 200 and got2.content == data_a,
        "另一条引用仍可取用（字节完好）",
        f"GET {got2.status_code} len={len(got2.content)}",
    )

    # 8c 最后一条引用也到期 → 真删字节、配额回升
    before_free = client.get("/quota", params={"owner": OWNER_A}).json()["used_bytes"]
    client.delete(f"/files/{g['file_id']}", params={"owner": OWNER_A})
    _force_expire(DB_PATH, [g["file_id"]])
    pr3 = client.post("/maintenance/purge", params={"owner": OWNER_A}).json()
    after_free = client.get("/quota", params={"owner": OWNER_A}).json()["used_bytes"]
    check(
        pr3["objects_purged"] >= 1 and after_free < before_free,
        "最后引用清除 → 字节真删 + 配额回升",
        f"objects_purged={pr3['objects_purged']} bytes_freed={pr3['bytes_freed']} "
        f"quota {before_free} → {after_free}",
    )
    check(
        g["storage_key"] not in storage.list_keys(),
        "桶内字节确实已删除",
        f"剩余对象：{len(storage.list_keys())} 个",
    )


def _force_expire(db_path: Path, file_ids: list[str]) -> None:
    """把指定记录的 purge_after 改到过去，模拟"保留期已过"（不改业务代码）。"""
    from datetime import datetime

    from sqlalchemy import create_engine, text

    eng = create_engine(f"sqlite:///{db_path}")
    with eng.begin() as cx:
        for fid in file_ids:
            cx.execute(
                text("UPDATE file_assets SET purge_after = :t WHERE file_id = :f"),
                {"t": datetime(2000, 1, 1), "f": fid},
            )
    eng.dispose()


def _cleanup(storage: Storage) -> None:
    print("\n清理测试桶…")
    try:
        n = storage.delete_all()
        storage.delete_bucket()
        print(f"  已删除 {n} 个对象并删除桶 {storage.bucket}")
        print(f"  RustFS 剩余桶：{storage.list_buckets()}（harbor 未受影响）")
    except Exception as e:  # pragma: no cover
        print(f"  清理失败（需手工处理）：{e}")


if __name__ == "__main__":
    raise SystemExit(main())
