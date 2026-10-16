# Windows 侧部署清单（fd-file-service 任务 6.1-6.3）

在 `windows-laptop`（tailnet `100.64.0.10`）上执行。每一步都给验证方法——
执行后把输出贴回会话，我这边交叉核对。

> **端口规划（2026-10-17 复核）**：该机 `9000` = **RustFS**（文件的存储面，
> 一切照旧不动）；`3000` = 其它在用的应用（**不动它**，Gotenberg 不用默认
> 端口）；Gotenberg 定在 **`3010`**（已实测空闲）。若 3010 日后被占，换任何
> 空闲端口并把 `FILE_GOTENBERG_URL` 对应改掉告知我。

---

## 6.1 起 Gotenberg 容器

### 步骤

在 windows-laptop 上打开 PowerShell（Docker Desktop 需已启动）：

```powershell
docker run -d --name gotenberg --restart unless-stopped -p 3010:3000 gotenberg/gotenberg:8
```

说明：
- `3010:3000` = 宿主 3010 → 容器内 3000（**容器内固定 3000，不改镜像**；
  冒号左边 3010 是宿主端口，避开 3000 上那个在用的应用）。
- `--restart unless-stopped` = Docker Desktop 运行期间自动重启；机器重启后
  依赖 Docker Desktop 自启（见 6.2 的说明）。
- 首次运行会拉镜像（约 500MB），稍等。

### 验证（把输出贴回）

```powershell
docker ps --filter name=gotenberg --format "{{.Names}} {{.Status}} {{.Ports}}"
curl.exe -s http://localhost:3010/health
```

期望：
1. `docker ps` 显示容器 `Up`，端口 `0.0.0.0:3010->3000/tcp`；
2. `curl` 返回健康文本（Gotenberg 8 的 `/health` 返回 `{"status":"up"}` 或
   `OK` 类文案，总之非空、非错误）。

我这边会从云侧验证 tailnet 可达：

```bash
curl -s http://100.64.0.10:3010/health
```

---

## 6.2 让 RustFS 与 Docker 在无人登录时存活

**背景（实况修正）**：RustFS 跑在 Docker Desktop 的容器里（restart 策略），
不是"计划任务·登录自启动"。所以关键不是给 RustFS 换启动方式，而是：

1. **Docker Desktop 设为开机自启**（这是 RustFS 与 Gotenberg 共同的前提）：
   Docker Desktop → Settings → General → 勾选
   **"Start Docker Desktop when you sign in to your computer"**。
2. **确认 RustFS 容器 restart 策略**（在 PowerShell 里跑）：
   ```powershell
   docker inspect rustfs --format "{{.HostConfig.RestartPolicy.Name}}"
   ```
   期望输出 `unless-stopped`。若为空或 `no`，补上：
   ```powershell
   docker update --restart unless-stopped rustfs
   ```
3. **Windows 服务（NSSM）方式**：若要求"未登录也存活"（Docker Desktop
   默认需用户登录会话），两条路：
   - a) 该机常开且自动登录 → 无需 NSSM；
   - b) 需真正服务化 → 用 NSSM 把 Docker Desktop 的
     `com.docker.service` 或直接给 RustFS.exe 注册服务（若 RustFS 有原生
     Windows 二进制，NSSM 注册比走 Docker 更稳）。

   推荐先做 a（成本为零），把 b 作为后续优化。

### 验证（把输出贴回）

```powershell
docker inspect rustfs --format "{{.HostConfig.RestartPolicy.Name}}"
```

期望：`unless-stopped`。

重启机器后（可选，更有说服力）：
```powershell
curl.exe -s -o NUL -w "%{http_code}" http://localhost:9000
```
期望：返回 `403`（服务活着，只是未签名请求被拒）——**不是**连接失败。

---

## 6.3 记录磁盘容量与数据目录

### 步骤（把输出贴回）

```powershell
# 各盘容量
Get-PSDrive -PSProvider FileSystem | Select-Object Name, @{n='UsedGB';e={[math]::Round($_.Used/1GB,1)}}, @{n='FreeGB';e={[math]::Round($_.Free/1GB,1)}}

# RustFS 数据目录（从容器挂载点看）
docker inspect rustfs --format "{{json .Mounts}}"
```

第二条会输出容器的挂载映射，形如
`[{"Source":"D:\\rustfs\\data","Destination":"/data",...}]`——`Source`
就是数据目录（宿主路径）。

---

## 执行顺序与回报

| 步骤 | 命令要点 | 回报内容 |
|---|---|---|
| 1 | `docker run ... -p 3010:3000 gotenberg/gotenberg:8` | `docker ps` 行 + `curl localhost:3010/health` 输出 |
| 2 | Docker Desktop 自启 + `docker inspect rustfs` 策略 | 策略字符串 + 重启后 9000 端口探测码 |
| 3 | 磁盘与挂载 | 两个输出原样贴回 |

我这边并行做的（无需你操作）：
- 从云侧验证 `100.64.0.10:3010` 的 tailnet 可达性；
- 验证 office 文件（.docx）走完整链：上传 → 事件 → worker → Gotenberg 转换
  → 文本抽取，并把结果写进验收报告。

完成后任务 6.1-6.3 勾掉，`FILE_GOTENBERG_URL` 在云侧部署（任务 7.1）时按
3010 注入。
