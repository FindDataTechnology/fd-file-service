# fd-file-service 节点侧部署（任务 7.1）

栈构成：`file-api`（HTTP 面，256m）+ `parse-worker`（事件消费与文本抽取，768m），
**同一镜像、不同入口**，共享 `/data` 卷上的同一个 SQLite（WAL 已开，双进程共写安全）。

镜像不在节点上构建（本机/节点禁 docker build）：经私有构建池出片：

```sh
fd-build-snapshot ~/finddata/fd-file-service -R <owner>/build-pool \
  --namespace yizuo --image fd-file-service
```

出片后拿 registry 引用填进 `.env` 的 `FD_FILE_IMAGE`。

## 0. 目录布局与前置

节点上选一个目录（下称 `$DEPLOY`，如 `/opt/fd-file-service`）：

```
/opt/fd-file-service/
  compose.yml     # 本目录的 compose.yml（拷过去）
  .env            # cp env.example .env 后逐项填写
```

`.env` 要点：
- `FD_FILE_IMAGE` = 构建池出的镜像引用；
- `FILE_S3_*` 四个值 + 桶名（生产桶 `fd-file`；`harbor` 桶永不触碰）；
- 真实凭据只放节点这份 `.env`，不进仓。

离线校验（不 build 不 pull）：`docker compose config`

## 1. 起栈

```sh
cd $DEPLOY
docker compose up -d
docker compose ps                  # 等 file-api healthy
curl -fsS http://127.0.0.1:8731/health
curl -fsS http://127.0.0.1:8731/config    # 生效配置自检（不含凭据）
```

`/config` 会回显配额档位与存储端点等**非凭据**配置，用来确认环境注入生效。

## 2. 首次连通性自检（三条，缺一不可）

```sh
# a. RustFS 连通（经 tailnet）：应看到桶列表，含 fd-file 与 harbor
docker compose exec file-api python -c "
from fd_file_service.storage import from_settings
print(from_settings().list_buckets())"

# b. 服务签发预签名 URL（不起浏览器）：应返回 upload_url 与 file_id
curl -s -X POST http://127.0.0.1:8731/files/upload-url -H 'content-type: application/json' \
  -d '{"owner":"ops:selftest","size":11,"mime":"text/plain","sha256":"'"$(printf hello-world | sha256sum | cut -d' ' -f1)"'"}'

# c. worker 能看到队列与 Gotenberg 状态
docker compose exec parse-worker python scripts/parse_worker.py --peek --health
```

## 3. 反代（对外形态）

默认端口只绑 `127.0.0.1:8731`，由节点反代终结 TLS 后转发。对外路径建议：

- 对外域名/入口由入口侧决定（与四条线的网关集成时再定）；
- **上传接口必须能直达**：客户端拿到预签名 URL 后直传 RustFS（不经本服务），
  所以 `FILE_S3_ENDPOINT` 的地址必须对**调用方网络**可达（tailnet 内机器用
  `100.64.0.10:9000`）；跨公网调用方需在 RustFS 前有公网入口。

## 4. 运维例行

```sh
# 保留期满释放（软删 30 天后真正删字节并回收配额）
# 建议每日 cron：curl -s -X POST http://127.0.0.1:8731/maintenance/purge

# 看队列积压与死信
docker compose exec parse-worker python scripts/parse_worker.py --peek

# 备份：/data 卷内是全部元数据（SQLite），桶内是全部字节与解析产物
docker run --rm -v fd-file-service_filedata:/d -v $(pwd):/b alpine \
  tar czf /b/fd-file-service-$(date +%F).tar.gz -C /d .
```

备份通道纪律（design D9）：客户业务文件**不得**经第三方消费级网盘；
网盘只可用于平台自有素材并显式标注。

## 5. 回滚（任务 7.4）

本服务不触碰任何存量：既有 `fd-text` 桶、`harbor` 桶、四条线的文件处理
零改动。回滚 = `docker compose down` + 需要时删 `fd-file` 桶，四条线行为不变。
