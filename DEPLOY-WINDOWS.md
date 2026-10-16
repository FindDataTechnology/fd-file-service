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

**验收对象 = windows-laptop（100.64.0.10）**。本服务的存储（RustFS 9000）与
解析（Gotenberg 3010）都在这台；windows-admin（100.64.0.5）是另一套独立
RustFS（凭据不互认、无 Docker），不在本服务链路内，无需为它做任何事。

**背景（实况）**：RustFS 跑在 Docker Desktop 的容器里（restart 策略）。
所以关键不是给 RustFS 换启动方式，而是：

1. **Docker Desktop 设为开机自启**（RustFS 与 Gotenberg 共同的前提）：
   Docker Desktop → Settings → General → 勾选
   **"Start Docker Desktop when you sign in to your computer"**。
2. **确认两个容器的 restart 策略**（在 PowerShell 里跑）：
   ```powershell
   docker inspect rustfs --format "{{.HostConfig.RestartPolicy.Name}}"
   docker inspect gotenberg --format "{{.HostConfig.RestartPolicy.Name}}"
   ```
   期望双双输出 `unless-stopped`。若为空或 `no`，补上：
   ```powershell
   docker update --restart unless-stopped rustfs
   docker update --restart unless-stopped gotenberg
   ```
3. **Windows 服务（NSSM）方式**：若要求"未登录也存活"（Docker Desktop
   默认需用户登录会话），两条路：
   - a) 该机常开且自动登录 → 无需 NSSM；
   - b) 需真正服务化 → 用 NSSM 把 Docker Desktop 的
     `com.docker.service` 注册成服务。

   推荐先做 a（成本为零），把 b 作为后续优化。

### 验证（把输出贴回）

```powershell
docker inspect rustfs gotenberg --format "{{.Name}} {{.HostConfig.RestartPolicy.Name}}"
```

期望：两行均为 `unless-stopped`。

重启机器后（可选，更有说服力）：
```powershell
curl.exe -s -o NUL -w "rustfs=%{http_code}`n" http://localhost:9000
curl.exe -s -o NUL -w "gotenberg=%{http_code}`n" http://localhost:3010/health
```
期望：`rustfs=403`（活着，未签名请求被拒）、`gotenberg=200`——**都不是**连接失败。

---

## 6.3 记录磁盘容量与数据目录

> ✅ **已完成（2026-10-17 回执）**，数字见下（已落 INFRA-NOTES.md 与
> deploy/README.md §4.1）：
> - windows-laptop：C 111.7G 用 / **剩 8.3G（C 盘紧张）**、D 168.5G 用 /
>   剩 174.8G；数据 `D:\rustfs\data`（bind），日志 `D:\rustfs\logs`；
>   Docker 数据盘 `D:\DockerDesktopWSL`（占 D 盘）。
> - windows-admin：C 79.9G 用 / 剩 12.5G、D 500G 用 / 剩 51.7G；
>   数据 `D:\rustfs\data`（11 桶约 30G）。该机无 Docker（LTSC 2021），
>   RustFS 为原生进程 + 计划任务登录自启。

原始步骤保留备查：

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

## 状态（2026-10-17）

- **6.1 ✅ 完成**：Gotenberg 部署于 3010；云侧复核健康 + 真 .docx 活体 12/12。
- **6.3 ✅ 完成**：两机容量与数据目录已回执并入档（见上）。
- **6.2 差最后一步**：Docker Desktop 开机自启勾选 + 两容器 restart 策略回执
  （只关 windows-laptop；windows-admin 不在本服务链路内）。

`FILE_GOTENBERG_URL` 已按 3010 注入生产（cheap-2 栈，2026-10-17 上线，
线上全链自检 7/7）。
