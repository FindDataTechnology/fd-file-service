# fd-file-service

统一文件服务：四条产品线（柏讯/壹座/识律/公文）共用的文件底座。

**核心原则：服务不理解业务。** 文件归属由调用方以不透明 `owner` 字符串声明
（如 `wire:cust_x`、`yizuo:sub_y`），服务只负责"该域下的字节"——不解析、不校验
其结构，也不查任何业务库。

对应 OpenSpec 变更 `fd-file-service`（任务 1.1-1.4、2.1-2.2、3.1-3.4、7.1）。
列表/取用的完整过滤面与事件解析管线归并行 change，本仓不重复实现。

## 跑起来

```bash
cp .env.example .env     # 填真实端点与凭据（.env 已 gitignore）
uv venv .venv && uv pip install -e ".[dev]"
uvicorn fd_file_service.app:app --host 127.0.0.1 --port 8731
curl http://127.0.0.1:8731/health
```

## 验收（三套，真实 RustFS，一键复跑）

```bash
python3 scripts/acceptance.py          # 37 项：服务面（上传/隔离/配额/去重/恢复/过滤）
python3 scripts/e2e_fullchain.py       # 22 项：全链（事件→worker→文本抽取可检索）
python3 scripts/stub_gotenberg_test.py # 9 项：Office 分支（本地桩替代真 Gotenberg）
```

均建 `fd-file-test-*` 临时桶 → 起服务于随机高端口 → 跑断言 → 删空并删桶。
**不动 `harbor` 桶。** 真 Gotenberg 容器部署后（见 `DEPLOY-WINDOWS.md`），
第三套的桩验部分可换成对真容器复跑。

解析 worker（常驻或一次）：

```bash
python3 scripts/parse_worker.py --once        # 处理完当前 backlog 即退出
python3 scripts/parse_worker.py               # 常驻轮询
python3 scripts/parse_worker.py --peek        # 只看队列概览，不改状态
python3 scripts/parse_worker.py --health      # 顺带探 Gotenberg 健康
```

## 上传流程（字节不经服务）

```
POST /files/upload-url {owner,size,mime,sha256}  → 预签名 URL + file_id（pending）
PUT  <预签名 URL>                                 → 字节直传对象存储
POST /files/{file_id}/confirm?owner=...           → 进列表、计入配额
```

未确认的上传不计配额、不出现在列表（不堆积垃圾）。

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/files/upload-url` | 申请上传凭据（命中去重则秒传，无需再传） |
| POST | `/files/{file_id}/confirm` | 确认上传 |
| GET | `/files?owner=` | 按 owner 列表（严格限域） |
| GET | `/files/trash?owner=` | 回收站（保留期内） |
| GET | `/files/{file_id}/download-url` | 带有效期的取用地址 |
| DELETE | `/files/{file_id}?owner=` | 软删除 |
| POST | `/files/{file_id}/restore?owner=` | 保留期内恢复 |
| GET | `/quota?owner=` | 用量 / 上限 / 档位 |
| POST | `/maintenance/purge` | 跑一轮保留期满释放 |
| GET | `/config` | 生效配置自检（不含凭据） |

`owner` 一律显式传参。鉴权归网关，本服务只做域隔离——跨域调用不报错，只查不到。

## 数据模型：两张表

任务书给的单表 `file_assets` 同时要求两件互斥的事：`(owner, sha256)` 唯一，
以及去重后"一个字节对象 + 多条文件记录"。唯一约束会拒掉第二个 `file_id`；
去掉约束则"只一份字节"没有强制力。故按职责拆：

- **`file_objects`** — 字节对象（去重单位）。`(owner, sha256)` 唯一约束落在这里，
  由数据库保证"同域同内容只有一份字节"。`ref_count` = 存活引用数。
- **`file_assets`** — 文件记录（交付单位）。每次上传一条，`file_id` 唯一，
  经 `object_id` 指向字节对象，可独立删除。

| file_assets 状态 | 条件 | 计入配额 | 在列表 |
|---|---|---|---|
| `pending` | `confirmed_at` 空 | 否 | 否 |
| `live` | 已确认、未删 | 是 | 是 |
| `deleted` | `deleted_at` 非空 | 是（字节还在） | 否 |
| `purged` | `purged_at` 非空 | 否 | 否 |

配额 = 该 owner 存活字节对象的 `size` 之和，天然是"去重后字节"。

## 环境变量

见 `.env.example`。存储四个值（`FILE_S3_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET`）
换掉即可换存储，业务行为不变；配额三档（`FILE_QUOTA_{FREE,BASIC,STANDARD}_BYTES`，
默认 1G/5G/10G）、保留期 `FILE_SOFT_DELETE_DAYS`（默认 30）、
`FILE_QUOTA_TIER_OVERRIDES`（按 owner 覆盖档位，平台内部资产走 `unlimited`）。

真实凭据只放本地 `.env`，已 gitignore。

## 已知环境坑

本机 httpx 默认 `trust_env=True` 会读到一个已死的系统代理（`127.0.0.1:7892`），
导致所有请求挂死。**验收脚本与任何调用本服务的客户端都必须用
`httpx.Client(trust_env=False)`。** 这是本机环境事实，与服务无关。
