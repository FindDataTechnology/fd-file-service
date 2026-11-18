#!/usr/bin/env python3
"""独立运行的解析消费端（任务 5.2 / 5.3）。

用法
----
消费一批（处理完当前队列就退出，适合 cron / systemd timer）::

    python scripts/parse_worker.py --once

常驻轮询（默认；空闲时用退避间隔轮询）::

    python scripts/parse_worker.py --interval 2.0 --max-idle-poll 300

先看看队列里有什么（不改动任何状态）::

    python scripts/parse_worker.py --peek
    python scripts/parse_worker.py --health     # 顺带探一下 Gotenberg

为什么必须是**独立进程**而不是服务内的后台任务
---------------------------------------------
这决定了事件总线必须能持久化（见 ``events.py`` 的选型理由）：

1. **负载隔离**：解析是 CPU/IO 密集（一个 50MB PDF 可能转几十秒），放在同一个
   uvicorn worker 里会拖慢所有 HTTP 请求——上传/列表全部变慢。「上传不被解析
   阻塞」（spec）要求两者**在时间上解耦**，而不只是 asyncio 意义上的
   ``create_task``（那仍争抢同一个事件循环）。
2. **可独立重启**：Gotenberg 出问题时只想重启消费端，不想重启文件服务
   （重启服务会让正在进行的上传全部失败）。
3. **可单独扩容/限流**：解析慢就多加几个 worker —— claim 的原子性已保证
   多个 worker 不会取到同一条事件。

退出码：0 正常；1 运行期异常；2 参数或环境不满足（如连不上存储）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# 让 `python scripts/parse_worker.py` 能直接跑（无需先 pip install -e .）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fd_file_service import events as ev
from fd_file_service import parse as ps
from fd_file_service.db import SessionLocal
from fd_file_service.parsers import gotenberg as gb
from fd_file_service.storage import from_settings as new_storage


def log(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="fd-file-service 解析消费端")
    p.add_argument("--once", action="store_true", help="处理完当前 backlog 即退出")
    p.add_argument("--interval", type=float, default=2.0, help="轮询间隔（秒），默认 2.0")
    p.add_argument(
        "--max-idle-poll",
        type=int,
        default=0,
        help="连续空闲轮询达到 N 次后退出；0 = 不限（常驻）",
    )
    p.add_argument("--limit", type=int, default=0, help="本次最多处理 N 条；0 = 不限")
    p.add_argument("--peek", action="store_true", help="只打印队列概览，不改动状态")
    p.add_argument("--health", action="store_true", help="探一下 Gotenberg 健康端点")
    p.add_argument(
        "--gotenberg-url",
        default=os.environ.get(gb.ENV_URL, ""),
        help="覆盖 FILE_GOTENBERG_URL",
    )
    return p.parse_args(argv)


def build_gotenberg(args: argparse.Namespace) -> gb.Client | None:
    """构造 Gotenberg 客户端。

    未启用的话返回 ``None``（原生 PDF 仍可抽取，退化由 ``parse.parse_file`` 处理）。
    """
    url = (args.gotenberg_url or "").strip() or os.environ.get(gb.ENV_URL, "").strip()
    if not url:
        url = gb.DEFAULT_URL
    return gb.Client.from_env() if not url else gb.Client(base_url=url.rstrip("/"))


def check_health(client: gb.Client) -> bool:
    ok, detail = client.health()
    log(f"Gotenberg {client.base_url} -> {'健康' if ok else '不可达'}: {detail}")
    if not ok:
        log("提示：Gotenberg 需由用户在 存储主机 部署（变更任务 6.1）；")
        log("      未部署时 Office 文件无法转换，但**原生 PDF 仍可完成文本抽取**。")
    return ok


def peek(backend: ev.Backend) -> None:
    if not isinstance(backend, ev.SQLiteBackend):
        log("队列概览: （非 SQLite 后端，无持久队列可查）")
        return
    counts = backend.count_by_status()
    # 空队列表非空 dict —— 直接打印 `{}` 容易被当成故障；说清"空"而不是
    # 让运维对着空花括号猜。（count_by_status 返回 {} 时就是"一条事件都没有"。）
    log(f"队列概览: {counts if counts else '（空，无任何事件行）'}")
    for rec in backend.recent(limit=10):
        payload = rec.to_event()
        missing = payload.missing_fields()
        log(
            f"  #{rec.id} {rec.event_type} file={rec.file_id} owner={rec.owner} "
            f"status={rec.status} attempts={rec.attempts}"
            + (f" 缺字段={missing}" if missing else "")
        )


def process_one(backend: ev.Backend, storage, gotenberg: gb.Client | None) -> bool:
    """认领并处理一条事件。返回是否真的处理了（False = 队列空）。"""
    claimed = backend.claim()
    if claimed is None:
        return False

    row_id, event = claimed
    session = SessionLocal()
    try:
        result = ps.handle_event(session, storage, event, gotenberg=gotenberg)
    except Exception as exc:  # 兜底：任何漏出的异常也要 nack，绝不让事件悬在 claimed
        backend.nack(row_id, f"worker 未捕获异常: {exc!r}")
        log(f"ERROR row={row_id} file={event.file_id}: {exc!r}")
        return True
    finally:
        session.close()

    # 处置结果：失败按重试预算 nack（可能在租约到期后回到队列）；
    # 成功/空文本/跳过都 ack —— 这三种再重试也不会变好，属于终态。
    if result.status == ps.STATUS_FAILED:
        verdict = backend.nack(row_id, result.error or "解析失败")
        log(
            f"row={row_id} file={event.file_id} FAILED stage={result.stage} "
            f"-> {verdict}: {(result.error or '')[:160]}"
        )
    else:
        backend.ack(row_id)
        log(
            f"row={row_id} file={event.file_id} {result.status} "
            f"chars={result.char_count} engine={result.engine} key={result.text_key}"
        )
    return True


def run(args: argparse.Namespace) -> int:
    if not ps.enabled():
        log("FILE_PARSE_ENABLED=0 —— 解析未启用，退出")
        return 0

    ps.init_parse_db()
    backend = ev.get_bus().backend
    storage = new_storage()
    gotenberg = build_gotenberg(args)

    log(
        f"启动: backend={type(backend).__name__} storage={type(storage).__name__} "
        f"gotenberg={gotenberg.base_url if gotenberg else '未配置'}"
    )

    if args.health:
        check_health(gotenberg)
    if args.peek:
        peek(backend)
        return 0

    # 存储连通性自检：连不上就别开工，否则每条事件都会因同一个原因失败
    # 且白白耗尽重试预算。早失败比留下一堆无意义的失败留痕有用。
    try:
        storage.list_keys("")
    except Exception as exc:
        log(f"FATAL 存储不可达: {exc!r}")
        return 2

    processed = 0
    idle = 0
    while True:
        did = process_one(backend, storage, gotenberg)
        if did:
            processed += 1
            idle = 0
            if args.limit and processed >= args.limit:
                log(f"达到本次上限 {args.limit} 条，退出")
                break
            continue

        if args.once:
            log(f"backlog 已空，本次共处理 {processed} 条")
            break

        idle += 1
        if args.max_idle_poll and idle >= args.max_idle_poll:
            log(f"连续空闲 {idle} 轮，退出（本次共处理 {processed} 条）")
            break
        time.sleep(max(0.1, args.interval))

    log(f"结束：处理 {processed} 条")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        log("收到中断，退出")
        return 0
    except Exception as exc:
        log(f"FATAL: {exc!r}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
