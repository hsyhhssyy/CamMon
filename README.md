# CamMon 摄像机录像存储网关

CamMon 在 NAS 的 IP 上提供 SMB1 共享和 NetBIOS 发现服务。每台设备拥有独立账号、共享和保留天数；设备内按字符串相机编号 X 管理录像。摄像机写入本地缓存，完成的录像整文件转存至本地磁盘、SMB2/3 或 NFS；默认选择本地磁盘，直接保存到 NAS 挂载目录。过期录像按北京时间自然日生成 60 倍速 H.264 MP4，归档永久保留。

管理页面提供设备配置、后端连接测试、组合统计、带首帧预览的分页录像列表、在线播放、字节范围下载、容量告警和归档任务重试。前端资源随镜像提供，运行时无需外部字体或 CDN。

新录像落盘和归档视频生成时会提取首帧，JPEG 图片保存在同一存储后端的 `thumbnails/` 目录，关联信息保存在 PostgreSQL。列表用预览图和信息卡片展示完整路径、摄像机、编号、日期、起止时间、时长、大小、类型及状态，支持播放与下载，手机端纵向排布。旧录像导入确认页每页 25 个文件，只对当前页逐张生成首帧，换页或离开后停止继续请求上一页；返回已浏览页会复用预览。导入时复用这些图片，并为尚未预览的录像补齐首帧。首帧无法解码时显示占位，录像仍按原流程保存或导入。已有数据库在启动时自动补充缩略图字段；以前已入库的本地磁盘录像按需生成首帧。

PostgreSQL 保存元数据。运行时的配置、索引和待同步变更全部缓存在内存中，PostgreSQL 断线后当前进程继续服务。新增录像使用临时磁盘缓存；重启允许丢失临时录像及未同步变更。

## NAS 部署

需要 Linux NAS、Docker Compose、可用的 PostgreSQL、足够的临时录像空间和运行内存，以及独占的 TCP 139/445。NAS 原有 SMB 文件服务使用同样 TCP 端口时，应先由管理员调整 NAS 服务。UDP 137/138 可以由 NAS 已有 NetBIOS 服务提供，默认自动选择共存模式；CamMon 会报告必需端口冲突，并保留管理页面用于查看错误。

### 使用 GHCR 公开镜像

将 [compose.ghcr.yaml](compose.ghcr.yaml) 和 [.env.ghcr.example](.env.ghcr.example) 放到 NAS 的同一目录即可，不需要源码或本地构建，也无需登录 GHCR。默认固定使用 `ghcr.io/hsyhhssyy/cammon:0.1.12`，支持 `linux/amd64` 和 `linux/arm64`。0.1.12 修复旧录像导入的文件夹浏览卡顿，将目录归属展示和路径查询合并进浏览对话框，并增加加载、超时及失败重试；0.1.11 新增 H.265/HEVC 浏览器端兼容播放、文件归属查询和导入目录独占绑定；0.1.10 新增录像及归档首帧缩略图、完整信息卡片和每页 25 个文件的按需导入预览；0.1.8 新增小米录像命名及目录支持、实际时长识别和可恢复查看的后台导入进度；0.1.7 新增在线播放、旧录像导入及归档后的虚拟文件记录；0.1.6 新增本地磁盘真实存储，默认将 `/volume1/WebCam` 挂载到 `/data`；从 0.1.5 起支持与 NAS 已有 NetBIOS 服务共存。镜像页面见 [GitHub Packages](https://github.com/hsyhhssyy/CamMon/pkgs/container/cammon)。

```bash
test -f .env || cp .env.ghcr.example .env
chmod 600 .env
# 首次部署且没有现有密钥时生成；已有 .env 中的密钥可直接沿用。
# 这个临时容器只运行生成命令，不启动摄像机服务，也不需要数据库。
docker run --rm --entrypoint python ghcr.io/hsyhhssyy/cammon:0.1.12 \
  -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
# 编辑 .env，填写 CAMMON_ADMIN_PASSWORD、CAMMON_POSTGRES_DSN、CAMMON_SECRET_KEY
# 将生成的密钥保存为 CAMMON_SECRET_KEY，更新或重启时保持相同。
docker compose -f compose.ghcr.yaml config --quiet
docker compose -f compose.ghcr.yaml pull
docker compose -f compose.ghcr.yaml up -d
docker compose -f compose.ghcr.yaml logs -f cammon
```

容器使用 [Docker host 网络](https://docs.docker.com/engine/network/drivers/host/)，直接监听 NAS 的 IP；此模式不使用 `ports` 映射。NAS 防火墙须允许摄像机局域网访问以下端口：

| 协议 / 端口 | 用途 |
| --- | --- |
| TCP 139、445 | 摄像机 Samba 访问 |
| UDP 137、138 | NetBIOS 名称解析与发现，由 CamMon 或 NAS 已有服务提供 |
| TCP 18080 | 管理页面，可用 `CAMMON_PORT` 修改 |

启动后访问 `http://NAS-IP:18080`。Compose 默认将 NAS 的 `/volume1/WebCam` 挂载到容器 `/data`，用于持久保存完成的录像和归档；可在 `.env` 中通过 `CAMMON_STORAGE_PATH` 修改 NAS 目录。管理页面首次默认选择“本地磁盘”，路径为 `/data`，测试读写后保存即可启用；已有 SMB/NFS 配置继续沿用。元数据使用 PostgreSQL 和内存，Samba 运行文件在 tmpfs，新增录像使用容器临时 `/cache`。PostgreSQL 需要在首次启动和重启时在线，运行期间断线仍按内存配置服务。

本地磁盘功能从 `0.1.6` 开始提供。已有录像时，切换存储方式或 NAS 目录前须迁移录像和索引；更新镜像不会自动将已有 SMB/NFS 录像搬到本地磁盘。

NAS 的 NetBIOS UDP 端口无法释放时，使用默认 `CAMMON_NETBIOS_MODE=auto`：只要 UDP 137 或 138 不可用，CamMon 不启动自己的 nmbd，SMB1 文件服务继续监听 TCP 139/445，健康检查不会因这些 UDP 端口报错。管理页及 `/healthz` 的 `discovery` 字段显示实际模式。也可在 `.env` 中显式设置 `CAMMON_NETBIOS_MODE=external`，始终沿用 NAS 发现服务；`internal` 则强制使用自带发现，UDP 冲突仍阻止启动。

共存时使用 NAS 现有的发现名称或直接填写 NAS IP，不会新增独立的 CAMMON 广播名称，也不修改 NAS 的发现服务。NAS 是否广播 SMB 服务器名称、摄像机能否枚举该名称，需要实机确认；直接通过 IP 和生成的共享名接入不依赖 UDP 发现。TCP 139/445 仍须独占。

从旧版本升级时保留 `.env` 中的密码、数据库连接和原密钥，更新 Compose 文件，将 `CAMMON_IMAGE` 改为 `ghcr.io/hsyhhssyy/cammon:0.1.12` 后执行 `pull` 和 `up -d`。仅 `restart` 不会更换镜像。源码部署预检可指定 `python3 scripts/check_deployment.py --netbios-mode external`。

更新镜像时执行同一组 `pull` 和 `up -d` 命令；查看状态用 `docker compose -f compose.ghcr.yaml ps`，停止服务用 `docker compose -f compose.ghcr.yaml down`。更新重建容器会丢弃临时录像及未同步的内存变更，符合临时缓存约定。

应用密钥保存在部署目录的 `.env` 中，文件权限应为 `0600`。`.env`、其他 `.env.*` 私有配置及 `secrets/` 同时被 Git 和 Docker 构建上下文忽略；仅两个无凭据的环境模板进入 Git。部署时携带并保管现有 `CAMMON_SECRET_KEY`，不要因重新部署而换一把密钥。

### 从源码构建

```bash
cp .env.example .env
# 修改 .env 中的管理员密码、PostgreSQL 连接和稳定的凭据加密密钥
# 生成 CAMMON_SECRET_KEY（开发环境先执行 make setup）
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
python3 scripts/check_deployment.py
docker compose up -d --build
docker compose logs -f cammon
```

浏览器访问 `http://NAS-IP:18080`，用 `.env` 中的账号登录。首次启动将管理员密码哈希写入数据库，此后修改环境变量不会重置已有密码。管理端口可用 `CAMMON_PORT` 改为其他端口，预检时同样传入 `--port`。

镜像默认使用普通容器内的 root 管理 Samba 独立 Unix 账号。Compose 使用 host 网络，无 `privileged`、额外 capability、FUSE 设备或宿主机 SMB/NFS 挂载。本地磁盘通过普通目录绑定挂载：`${CAMMON_STORAGE_PATH:-/volume1/WebCam}:/data`，录像转存不经过 SMB/NFS 网络。`/run/cammon` 使用 tmpfs；`/cache` 使用容器临时可写层。不要设置容器为非 root，也不要运行多个 CamMon 实例共用同一 PostgreSQL schema。

构建会从固定的 Samba **4.25.0** 源码同时生成 `smbd`、`nmbd` 和 VFS 模块，并编译固定版本的 **libnfs 5.0.2**，两份源码均校验 SHA256。libnfs 单独构建并与 CFFI 桥接配套，避免 Debian 旧版客户端无法编码较大 NFSv4 写入。首次构建需要网络和一定编译时间。`CAMMON_BUILD_JOBS` 默认 2，可按 NAS 内存调整。

## CI 构建与 GHCR 镜像

[Publish to GHCR](.github/workflows/publish.yml) 在推送 `v` 开头的语义化版本标签或从 GitHub Actions 手动运行时发布镜像。先将对应版本的代码和工作流提交并推送到 GitHub，再创建一个新版本标签，例如：

```bash
git tag v0.1.13
git push origin v0.1.13
```

镜像地址采用小写仓库名，当前为 `ghcr.io/hsyhhssyy/cammon`。`v0.1.13` 发布 `0.1.13`、`0.1`、`latest` 和 `sha-完整提交哈希` 标签；`v0.1.13-rc.1` 只发布预发布版本和 SHA 标签，不更新稳定版本标签。手动运行发布 `edge` 和 SHA 标签。`latest` 指最近一次发布的稳定版本；NAS 默认固定版本，更新时修改 `CAMMON_IMAGE` 后执行 `pull` 和 `up -d`。

发布前复用 [CamMon checks](.github/workflows/ci.yml)，执行 Python、PostgreSQL、前端构建、浏览器和真实协议测试。amd64 与 arm64 分别使用原生 Linux runner 测试、构建，并复用 BuildKit 缓存；全部检查通过后才上传 `production` 镜像，最终合并为支持 `linux/amd64` 和 `linux/arm64` 的镜像。PR 和普通分支推送执行检查；发布流程不重复触发另一套标签检查。

2026-10-10 的[公开发布运行](https://github.com/hsyhhssyy/CamMon/actions/runs/38017078122)已全部通过，发布了 `edge` 和对应 SHA 标签，并验证包为 Public、匿名清单包含两种架构。NAS 默认使用的 `0.1.3` 固定标签保持不变。

2026-10-12 的[0.1.5 发布运行](https://github.com/hsyhhssyy/CamMon/actions/runs/38253526834)全部成功，两种架构各通过 92 项容器测试，公开发布并匿名验证 0.1.5 / latest；NAS 默认版本更新为 0.1.5，支持宿主机 NetBIOS 共存。

2026-10-13 的[0.1.6 发布运行](https://github.com/hsyhhssyy/CamMon/actions/runs/38282822017)全部七个任务成功，两种架构各通过 108 项完整容器测试，应用、PostgreSQL、前端构建和浏览器流程通过。0.1.6 / latest 的匿名双架构清单及镜像配置已验证，指向提交 `6a37f69`；NAS Compose 默认版本更新为 0.1.6，新增默认本地磁盘存储。

2026-10-13 的[0.1.7 发布运行](https://github.com/hsyhhssyy/CamMon/actions/runs/38347769327)全部七个任务成功，amd64 / arm64 各通过 157 项完整容器测试，应用、PostgreSQL、前端构建和两项浏览器流程通过。0.1.7 / latest / 0.1 / SHA 标签的匿名双架构清单、版本及源码配置已独立验证，指向提交 `c9d4c0a`，镜像摘要为 `sha256:e04253019377b1c34c84792e89f73d38d35910d0b4afd2ff4041b67713d4624c`；[GitHub Release](https://github.com/hsyhhssyy/CamMon/releases/tag/v0.1.7)已正式发布。默认部署版本更新为 0.1.7，包含在线播放、旧录像导入、关联副本清理、归档后虚拟原片和摄像机共享枚举隔离。

2026-10-13 的[0.1.8 发布运行](https://github.com/hsyhhssyy/CamMon/actions/runs/38408359091)全部七个任务成功，amd64 / arm64 各通过 200 项完整容器测试；154 项非集成、21 项 PostgreSQL、3 项浏览器测试及前端生产构建通过。0.1.8 / latest / 0.1 / SHA 标签的匿名双架构清单、版本及源码配置已独立验证，指向提交 `7ba40c4`，镜像摘要为 `sha256:3e4d71f906cc41d88bbbf80c057d2fe489aae827d4cfea9d2b4af6f86ce0ee45`；[GitHub Release](https://github.com/hsyhhssyy/CamMon/releases/tag/v0.1.8)已正式发布。默认部署版本为 0.1.8，新增小米录像格式与三层目录支持、实际时长识别及后台导入进度。

2026-10-13 的[0.1.10 发布运行](https://github.com/hsyhhssyy/CamMon/actions/runs/38424283874)全部七个任务成功，amd64 / arm64 各通过 209 项完整容器测试；162 项非集成、22 项 PostgreSQL、4 项浏览器测试及前端生产构建通过。0.1.10 / latest / 0.1 / SHA 标签的匿名双架构清单、版本及源码配置已独立验证，指向提交 `211e130`，镜像摘要为 `sha256:5052fd3642ebf5cc439714b499cae0b972d5aa189d516f9f1469d51808d373f7`；[GitHub Release](https://github.com/hsyhhssyy/CamMon/releases/tag/v0.1.10)已正式发布。默认部署版本为 0.1.10，新增录像和归档持久首帧、完整信息卡片及分页按需导入预览；v0.1.9 因浏览器测试共享存储干扰未上传镜像，修复后回归通过。

2026-10-15 的[0.1.12 发布运行](https://github.com/hsyhhssyy/CamMon/actions/runs/38599070218)全部七个任务成功，amd64 / arm64 各通过 225 项完整容器测试；177 项非集成、23 项 PostgreSQL、12 项浏览器测试及前端生产构建通过。0.1.12 / latest / 0.1 / SHA 标签的匿名双架构清单、版本及源码配置已独立验证，指向提交 `ff7bfbd`，镜像摘要为 `sha256:56fa6be8935c20ce98cdd070eb606f0133986d577dee22214c72a974fa3a9263`；[GitHub Release](https://github.com/hsyhhssyy/CamMon/releases/tag/v0.1.12)已正式发布。默认部署版本为 0.1.12，修复文件夹浏览时反复扫描全部录像的卡顿，将目录归属和路径查询合并到浏览对话框，并增加加载、超时和失败重试。

CI 登录 GHCR 使用 Actions 自带的 `GITHUB_TOKEN`，仅发布任务授予 `packages: write`，可见性预检只授予 `packages: read`，无需另存发布用的 PAT；CI 使用临时测试数据库，不需要部署环境的 PostgreSQL 连接和凭据。若同名包已存在，须在包的 “Manage Actions access” 中允许此仓库写入。按用户最新要求使用公开包：上传 digest 后验证 Public，再创建下载标签，发布后验证实际可见性及匿名多架构镜像访问。NAS 拉取公开镜像无需登录；参见 [GitHub Container registry 文档](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)。

NAS 上直接使用公开镜像见前文“使用 GHCR 公开镜像”。已有源码构建部署也可以将 `.env` 中的 `CAMMON_IMAGE` 改为 `ghcr.io/hsyhhssyy/cammon:0.1.12`，使用原 Compose 拉取启动：

```bash
python3 scripts/check_deployment.py
docker compose pull cammon
docker compose up -d --no-build
```

## PostgreSQL 元数据与离线服务

在 `.env` 中填写连接后重启容器：

```dotenv
CAMMON_POSTGRES_DSN=postgresql://cammon:URL编码后的密码@192.168.1.20:5432/cammon?sslmode=require
CAMMON_POSTGRES_SCHEMA=cammon
CAMMON_METADATA_SYNC_SECONDS=5
CAMMON_SECRET_KEY=生成的Fernet密钥
```

连接指向已创建的 PostgreSQL 数据库，账号须能连接、创建自己的 schema/table，以及读写这些表。管理员也可提前创建专用 schema 并授予该账号所有权。保留连接中的 `sslmode` 等 TLS 参数；本机未启用 TLS 的测试服务可按环境调整。连接字符串与密钥只从部署环境读取，不通过 API 返回。Compose 要求配置连接及密钥；直接开发运行可省略 PostgreSQL，此时所有元数据仅存在于内存，重启从空配置开始。

PostgreSQL 保存 `settings`、`devices`、`directories`、`recordings`、`archive_jobs`、`coverage`、`sessions` 表，分别包含配置、设备、目录、录像索引、归档任务、跨日覆盖关系与会话哈希。录像字节仍在临时缓存及本地磁盘或 SMB/NFS 后端；远程存储密码和摄像机密码使用 `CAMMON_SECRET_KEY` 加密后保存到 PostgreSQL，管理员密码只保存哈希。Samba 的临时凭据库在 tmpfs 中，启动时从 PostgreSQL 中的凭据重建。

进程启动时，在一致性事务中从 PostgreSQL 加载配置、元数据与索引，重建虚拟目录和 Samba 凭据。随后使用 SQLite `:memory:` 管理运行索引及事务队列，临时 SQL 数据同样只用内存，不生成 SQLite、WAL 或本地密钥文件。独立线程默认每 5 秒分批同步；关联记录和凭据一起保存，使已提交批次可重新加载。远端事务提交后才确认内存队列，重复确认可幂等重放，连续修改按记录合并。

启动后 PostgreSQL 断线，当前进程继续按内存配置服务：允许修改设备和存储配置，录像写入、封存、读取及转存继续运行。连接恢复后自动补同步。“真实存储”页面显示连接状态、最近同步时间和待同步数；单独元数据库故障不将摄像机服务标记为不健康。SMB/NFS 故障仍按缓存容量规则处理。

重启时内存配置和待同步变更消失，临时录像及转码文件也会丢弃；只从 PostgreSQL 加载已保存的数据。PostgreSQL 尚不可用时启动会失败并报告原因，不提供断线重启恢复。`CAMMON_SECRET_KEY` 应跨重启保持相同，否则无法解密数据库中已保存的凭据。

每个 schema 供一个运行中的网关使用，通过数据库锁阻止并发写入。管理页面/API 是运行期间的配置写入口；直接修改 PostgreSQL 不会自动刷新内存。当前进程仍存活时，较旧的 PostgreSQL 快照可从内存重新同步；重启后完全采用数据库快照，不覆盖为一份空索引。未知同名表或运行中的远端修订冲突会阻止同步。

## 配置与接入

1. 在“真实存储”页面填写一个全局后端，先点击“测试读写连接”，再保存。
   - 本地磁盘（默认）：填写容器内的绝对路径，默认 `/data`；Compose 默认绑定 NAS 的 `/volume1/WebCam`，无需账号密码或网络传输。原录像保存在 `originals/设备ID/`，归档保存在 `archives/设备ID/`。更换 NAS 目录时修改 `.env` 中的 `CAMMON_STORAGE_PATH` 并重建容器；页面路径保持 `/data`。
   - SMB：`smb://192.168.1.10/recordings`，可带端口和共享内子目录，账号、密码、域分别填写。使用 SMB2/3；可要求 SMB3 加密。
   - NFS：`nfs://192.168.1.10/volume1/recordings`，必须填写完整导出目录，选择 NFSv3 或 NFSv4.0。默认 UID/GID 65534；在 NAS 上允许配置的 UID/GID 读写并允许网关 IP 访问导出。
2. 在“摄像机管理”添加设备并设置保留天数。保存一次性显示的共享路径、用户名和密码，填入摄像机的 NAS 配置。
3. 摄像机写入支持 `00_20251008080239_20251008082813.mp4`，也支持小米的 `30M33S_1753536633.mp4` 和自动创建的 `xiaomi_camera_videos/设备标识/年月日小时/` 目录。

摄像机使用各自的接入账号登录后，共享列表只显示该账号对应的摄像机共享，其他设备的共享不可见，也不能通过已知共享名访问。无法手动指定路径的摄像机，可在填写用户名和密码后列出并选择自己的共享文件夹。

共享根目录只允许创建目录。普通目录最多两层；小米目录额外支持三层 `xiaomi_camera_videos/设备标识/YYYYMMDDHH/`，设备标识为 12 位十六进制字符串（MAC 地址去掉冒号），小时目录须为有效的北京时间。录像支持以下两种名称：

- `X_YYYYMMDDHHmmss_YYYYMMDDHHmmss.mp4`：位于第一层或第二层目录，X 为数字字符串，`00` 与 `0` 独立。起止时间按北京时间解释，时间必须有效，结束不早于开始。
- `mmMssS_UNIX.mp4`：可位于普通目录或小米三层目录，例如 `xiaomi_camera_videos/788b2a487711/2025072621/30M33S_1753536633.mp4`。10 位 Unix 秒级时间戳对应北京时间 `2025-07-26 21:30:33`，分、秒须与前缀一致；父目录若为 10 位年月日小时，也须一致。此格式没有通道和结束时间，统一归入编号 `00`，写入期间暂按 60 秒建立索引，封存后转存前用 ffprobe 读取实际时长并更新结束时间。时长读取失败会保留缓存、显示原因并重试。

随机写入、追加、回改文件头和合法改名均支持；改名后的编号和时间重新进入索引，目录改名也重新检查目录结构和时间。缓存录像关闭所有句柄后可以删除，已封存录像不能由摄像机修改、改名或删除。

设备可以停用和重置接入密码；停用停止摄像机访问，管理页面仍可统计与下载该设备的数据。归档视频只在管理页面可见。

在“录像文件”列表点击播放按钮，可直接播放普通录像或 60× 归档，支持暂停、拖动进度、全屏和 0.5×–4× 播放速度。归档视频本身已压缩时间轴，1× 播放即为原录像的 60 倍速。播放通过登录后的 `/api/files/{id}/stream` 接口按字节范围读取，适用于本地缓存及本地磁盘、SMB/NFS 后端，不需要先下载整个文件。关闭播放器会停止播放和加载；下载按钮继续提供原文件。

在线播放优先使用浏览器原生播放器；原生解码失败或只播放出音频时，自动切换为浏览器端 WebAssembly 软件解码，支持 H.265/HEVC、H.264 及 AAC、Opus、MP3、PCM 音频。两种模式均通过带登录认证的 HTTP Range 接口读取原录像；兼容模式每次请求最多 1 MiB 并保留短播放缓冲，支持暂停、拖动、倍速、音量和全屏，关闭播放器停止读取。服务器不转码、不生成播放副本，下载仍为原文件。解码资源随镜像提供，运行时不依赖外部 CDN，也可在 NAS 的普通 HTTP 页面上使用；4K 软件解码流畅度取决于观看设备的性能。未写完、损坏或软件解码仍失败时提示重试或下载。已删除内容的虚拟原片不提供播放，须选择对应的归档。

### 导入旧录像

使用本地磁盘存储时，可将历史录像放到 NAS 挂载目录中的独立文件夹，例如 `/volume1/WebCam/旧录像`（容器内为 `/data/旧录像`），然后在管理页“旧录像导入”中浏览或填写容器内路径，选择归属摄像机，预览后点击“开始导入”。默认包含所有子文件夹，也可取消递归扫描。已停用的摄像机同样可以接收历史数据。

“旧录像导入”页面点击“浏览文件夹”后立即打开对话框，显示加载进度、当前目录和每个子目录的摄像机归属；加载失败或超时会在对话框内显示原因，可重试或返回存储根目录。归属信息按一次请求共享的路径索引查询，避免每个子目录都扫描全部录像。对话框内的“文件归属查询”默认显示当前目录，也可填写 `/data` 下任意视频、文件或文件夹的容器内绝对路径，查看摄像机名称、是否已登记录像或副本，以及路径是否仍在磁盘上。查询涵盖摄像机写入的 `originals/设备ID/`、`archives/设备ID/`、旧录像导入和关联副本。未入库的文件若位于已绑定目录内，会显示按目录归属，并注明尚未登记为录像。归档清理后仍可查询保存的归属。查询接口为需要登录的 `GET /api/storage/ownership?path=/data/旧录像/视频.mp4`，返回 `status`（`owned` 已归属、`unassigned` 未归属、`container` 公共父目录、`conflict` 历史归属冲突）、`owners`（摄像机 ID 和名称）、`kind`、`exists` 及关联录像信息。

一个导入文件夹及其全部子目录只能归属一台摄像机。预览只检查归属，点击“开始导入”时立即保存目录绑定，即使目录为空、文件处理失败或取消递归扫描也保留绑定；视频归档或副本清理后绑定继续保留，并随元数据同步到 PostgreSQL、在重启后恢复。另一个摄像机导入相同目录、它的子目录或包含它的父目录时，预览和提交均返回 HTTP 409，不会部分导入。已有版本保存的录像和副本关联也参与检查；若历史数据已经把同一目录关联给多个摄像机，查询显示冲突并阻止继续导入，保留现有数据。`/data` 根目录是公共容器，导入时必须选择独立子目录；不同摄像机可以分别使用互不包含的同级目录。

点击“开始导入”后，服务端创建后台任务，先统计文件数量，再逐个处理。页面显示已处理 / 总文件数、当前文件和导入、重复、跳过、失败统计；切换页面、刷新或关闭浏览器不会取消任务，重新打开“旧录像导入”会自动恢复最近任务的进度或最终结果。进度查询失败时页面自动重试。一次只执行一个导入任务，重复提交同一运行中的任务会返回原任务。任务状态和最近 10 次结果保存在当前服务进程内；容器或服务重启后不保留任务进度，也不自动续跑，重新导入同一目录会跳过已导入文件。

从 0.1.8 起，`POST /api/import` 返回 HTTP 202 和任务信息；通过 `GET /api/import/jobs/{job_id}` 查询进度与最终结果，`GET /api/import/jobs` 返回最近任务。自定义 API 客户端需改为轮询任务结果。

导入直接为原位置的文件建立索引，不复制视频、不占用录像缓存。源目录层数不限；支持上述两种录像名称。带 X 的名称按文件名识别编号和起止时间；小米 `mmMssS_UNIX.mp4` 统一归入编号 `00`，开始时间取 Unix 秒级时间戳，导入预览和导入时读取实际视频时长来计算结束时间，并校验原目录的小时（若为 10 位时间目录）。所有时间按北京时间显示。空文件、时间无效、无法识别的名称或无法读取时长的小米视频会跳过，结果显示导入、重复、可关联副本、跳过和失败数量以及前 100 个文件详情。同一源文件重复导入会跳过，已归入其他摄像机的源文件不会再次归属。CamMon 管理的 `originals/`、`archives/` 和符号链接目录不参与导入。

目标摄像机已有同名录像（相同 X、起止时间）时，系统进一步核对大小及 SHA256 内容。完全相同的旧目录副本会关联到已有录像，统计和加速视频只计算一次；点击“开始导入”后才保存副本关联，预览不会修改或删除文件。副本关联沿用现有 `settings` 元数据保存到 PostgreSQL。该录像全部日期归档成功、所需元数据持久保存后，摄像机目录的原视频和旧目录的副本都会清理。若录像已归档且留有校验值，副本可在后续定时清理中删除。删除前再次校验副本内容；同名但内容不同、摄像机仍在写入或无法核对内容的文件会保留并显示原因。

导入后录像进入该摄像机的统计、筛选和下载列表，保留天数按录像日期计算，不按导入时间或文件修改时间重置。超期片段在下一次每天北京时间 02:00 后的归档入队中生成 60 倍速视频；若当天已经入队，则等次日处理。跨日录像沿用按日期切分的规则。全部覆盖日期归档成功且所需元数据写入 PostgreSQL 后，系统会删除原位置的源文件；损坏或转码失败的原件继续保留。

导入完成后请勿在磁盘上移动、改名、修改或另行删除这些文件。导入仅支持当前配置的本地磁盘目录，SMB/NFS 后端需先迁移至本地磁盘。更新镜像后可使用此入口，更新本身不会自动扫描历史目录。

默认允许 NTLMv2。若实机只能使用 NTLMv1，在独立的摄像机局域网内配置 `CAMMON_ALLOW_NTLMV1=true`。自带 NetBIOS 模式中可发现 CAMMON；共存模式沿用 NAS 已有名称，也可直接使用 NAS IP。不同摄像机的发现、认证方式仍须实机确认。

## 录像流转与恢复

```mermaid
flowchart LR
    Camera[摄像机 SMB1] --> Samba[Samba / VFS]
    Samba --> RPC[本机 Unix socket]
    RPC --> Index[内存运行索引]
    Index --> Outbox[内存事务队列]
    Outbox --> PostgreSQL[PostgreSQL 元数据]
    PostgreSQL -->|启动加载| Index
    RPC --> Cache[本地录像缓存]
    Cache --> Upload[顺序转存临时文件]
    Upload --> Verify[刷新 / 大小与 SHA256 校验 / 改名]
    Verify --> Remote[本地磁盘 / SMB2/3 / NFS 原录像]
    Remote --> Archive[按 X 与北京时间日期生成 60× 归档]
    Archive --> Permanent[校验发布后永久保存]
    Index --> Web[管理页面 / 筛选 / 下载]
```

同一 X 出现时间更晚的录像后，上一段在所有写入句柄关闭且连续 10 秒未修改时封存。最后一段没有后续录像时，关闭句柄且连续 30 分钟未修改也会封存。不同 X 独立处理；迟到的旧片段不会误封存当前片段。

转存至后端临时文件，刷新后核对大小及 SHA256，再改名为最终文件。成功前保留本地源文件；成功后删除本地录像缓存，索引中的路径、大小、文件 ID 保持一致。已打开的本地读取句柄可继续读完，之后读取通过对应的后端适配器完成；本地磁盘直接读写挂载目录，SMB/NFS 使用用户态客户端。枚举、属性、统计均使用索引；共享目录中的零字节文件只是 Samba 的路径占位，不能拿它们当录像备份。

后端故障期间在当前进程中保留缓存并重试。重启丢弃临时录像，不恢复中断的缓存上传；数据库中已保存的归档任务和远程录像可重新加载。空间不足时向摄像机返回 ENOSPC 并在页面告警。空间限制计算缓存录像与归档临时视频的实际总大小；应给缓存预留网络离线和归档工作空间。

每天北京时间 02:00 后入队过期日期，重启后补入当日错过的队列。保留天数包含当天：保留 1 天时只保留今天的普通录像；保留 30 天时保留今天及此前 29 天。默认一次运行一个归档任务。

归档只拼接实际存在的录像，不补齐缺失时段：24 小时约生成 24 分钟，12 小时约生成 12 分钟。按 60 倍时间轴抽帧，输出 H.264、25 fps、无音轨，标准偶数分辨率保持源尺寸。同组分辨率发生变化时统一到首段尺寸。跨午夜片段按日期切分，全部覆盖日期成功归档后才删除原录像数据；下载句柄正在读取时延后删除。损坏、时长与文件名不符、空间不足或转码失败均保留原件并记录原因。迟到片段生成追加归档，不覆盖已有归档。60 倍代表播放速度，文件大小不保证为原来的 1/60。

删除原录像数据后永久保留 `virtual` 文件记录。摄像机通过 SMB 枚举、查询属性或打开句柄查询属性时，仍能看到原路径、大小、文件 ID 和修改时间，避免因为旧文件消失而重复上传。数据库保留起止时间及归档时探测的原视频时长，重启后恢复这些记录；共享中的零字节路径占位仅在 tmpfs，不保存视频数据。虚拟文件和其他已封存录像一样禁止摄像机覆盖、截断、改名或删除。

虚拟记录只提供文件信息，不提供可播放的原视频：SMB 读取内容返回无数据错误，管理端对原片下载（包括 HEAD）返回 HTTP 410，应改为下载归档视频。SMB 普通文件属性不包含视频时长；摄像机若读取 MP4 文件头来校验时长，仍需实机验证兼容性。管理页文件列表和容量统计不计入虚拟原片。升级时根据完整、成功的归档覆盖关系恢复旧版本已删除原片的虚拟记录；旧记录没有探测时长时使用文件名起止时间之差，同路径已有文件优先保留。

删除历史原片还须等归档文件、任务及覆盖关系的元数据成功写入 PostgreSQL。元数据库断线时可以生成和发布归档，但原片继续保留；这样重启丢失未同步的归档状态后，仍可由数据库中保存的源录像和任务重新处理。

统计按年、月、日期、设备、X 和视频类型同时筛选。普通录像归属起始日期，归档归属归档日期。每个逻辑文件计数一次，上传临时副本不计入。跨日原录像尚待其他日期归档时仍实际保留，因此会与已生成的归档分别计数。

## 存储位置与运行参数

| 路径 / 参数 | 用途 / 默认值 |
| --- | --- |
| 进程内存 | 完整元数据索引、配置、内存事务同步队列；重启丢失 |
| `/run/cammon/runtime/samba/` | tmpfs 中的 Samba 临时凭据与运行状态 |
| `/run/cammon/runtime/shares/` | tmpfs 中的虚拟目录路径占位 |
| `/run/cammon/runtime/archive-manifests/` | tmpfs 中的转码清单，完成后删除 |
| `/cache/blobs/` | 临时录像字节，无重启恢复保证 |
| `/cache/work/` | 归档下载、转码临时文件及发布候选文件 |
| `/data/` | 默认本地真实存储目录，绑定 NAS 的 `/volume1/WebCam`，持久保存原录像与归档 |
| `CAMMON_STORAGE_PATH` | Compose 的宿主机录像目录，默认 `/volume1/WebCam`；容器内挂载路径为 `/data` |
| `CAMMON_CACHE_LIMIT_BYTES` | 默认 10 GiB；0 表示使用磁盘剩余空间 |
| `CAMMON_CACHE_RESERVE_BYTES` | 默认给磁盘预留 64 MiB |
| `CAMMON_SUCCESSOR_QUIET_SECONDS` | 默认 10 秒 |
| `CAMMON_IDLE_SEAL_SECONDS` | 默认 1800 秒 |
| `CAMMON_FFMPEG_THREADS` | 默认 2 |
| `CAMMON_POSTGRES_DSN` | PostgreSQL 连接字符串；Compose 必填 |
| `CAMMON_SECRET_KEY` | 稳定的 Fernet 密钥，Compose 必填，保存在部署环境中 |
| `CAMMON_POSTGRES_SCHEMA` | 默认 `cammon`；每个网关单独使用 |
| `CAMMON_METADATA_SYNC_SECONDS` | 默认 5 秒；有积压时连续分批同步 |
| `CAMMON_METADATA_CONNECT_TIMEOUT_SECONDS` | 默认 3 秒，仅影响独立同步线程 |
| `CAMMON_METADATA_STATEMENT_TIMEOUT_SECONDS` | 默认 5 秒，限制数据库语句及锁等待 |
| `CAMMON_METADATA_BATCH_SIZE` | 默认选择 256 条变更，附带保存相关依赖 |

需要备份的是 PostgreSQL、真实存储中的录像和部署配置中的密钥。本地磁盘模式应备份 `/data` 对应的 NAS 目录；SMB/NFS 模式备份远程录像目录。无需保留临时录像缓存。需要选择 SSD 临时录像路径时，可在 Compose override 中仅为 `/cache` 绑定 scratch 目录；其中录像仍按重启时丢弃处理。元数据和 Samba 运行文件保持内存 / tmpfs 模式。

已有录像或导入目录归属时禁止直接切换存储协议、地址或 NFS 版本。第一版没有自动迁移工具；更换地址需要独立安排数据与索引迁移。单纯更新账号密码不会迁移或重复统计数据。

健康检查：`GET /healthz`。API 文档：`/docs`；管理接口位于 `/api`，需要登录 Cookie，写请求须带 `X-CamMon-Request: 1`。管理端默认用于局域网；经 HTTPS 反向代理部署可设置 `CAMMON_COOKIE_SECURE=true`（使用 Compose 时在 environment 中加入）。

## 开发与测试

```bash
make setup
make check
make test
# 实际 Samba / NFS 协议集成，在普通容器中运行
make native-test
# 浏览器操作测试，使用隔离的临时测试数据
cd frontend
npx playwright install chromium
npm run test:e2e
```

只开发管理端可关闭 Samba：

```bash
CAMMON_SAMBA_ENABLED=false CAMMON_DATA_DIR=/dev/shm/cammon-dev CAMMON_CACHE_DIR=./cache \
CAMMON_SOCKET_PATH=/tmp/cammon-dev/vfs.sock CAMMON_FRONTEND_DIR=./frontend/dist \
CAMMON_ADMIN_PASSWORD=your-development-password uv run cammon
```

原生构建支持在独立开发机上验证：按 Dockerfile 安装构建依赖，解压固定 Samba 源码，执行 `python3 native/register_module.py /path/to/samba-source`，然后用 Dockerfile 中的 configure 参数构建安装。使用项目虚拟环境执行 `python cammon/nfs_build.py` 编译 libnfs 桥接。

```bash
sudo env CAMMON_TEST_SAMBA_PREFIX=/your/samba/prefix CAMMON_TEST_NFS=1 \
  CAMMON_TEST_POSTGRES=1 .venv/bin/python -m pytest -q
```

PostgreSQL 集成测试需要安装服务端二进制，使用隔离的临时数据库和空闲高位端口，不修改现有数据库：`CAMMON_TEST_POSTGRES=1 uv run --extra test pytest -m postgres -q`。测试覆盖当前进程断线服务、数据库不可用时不能冷启动、启动加载、临时状态丢弃、提交确认丢失、并发同步、归档删除确认、快照恢复和 schema 冲突。原生环境另测清空全部 Samba 运行目录后，原账号与密码仍能通过真实 SMB1 登录、读取既有录像。

原生测试会创建并清理临时 Unix 摄像机账号、启动独立 Samba 和 Ganesha MEM 导出，需要测试机的 UDP 137/138、TCP 139/445、111、2049 空闲。共存测试临时占用 UDP 端口或启动已有 nmbd，验证发现不受影响、两种 TCP 端口上的 SMB1 认证及读写、健康检查和必需端口冲突。Ganesha 仅用于测试，运行产品不需要 NFS 服务端、rpcbind 或系统挂载。

0.1.5 的两种架构各通过全部 92 项 Python 容器测试，管理页浏览器流程和生产构建通过，已验证实际 SMB1 客户端写入和读取、SMB2/3 后端、NFSv3/v4.0、NetBIOS 自带发现与宿主机共存、FFmpeg 跨日归档、PostgreSQL 18 内存断线服务、冷启动加载和 Samba 原账号重建。上传中断与远程 ENOSPC 使用真实适配器操作周围的可控故障注入。`cammon:0.1.5` 及 `latest` 的匿名访问和双架构清单已验证；目标 NAS 更新后的页面、健康接口、SMB1 协商及 NetBIOS 共存已通过现场检查，实际设备认证、录像写读、后端转存及摄像机实机验收仍待完成。

需求和验收记录见 [Stage S1](.docs/stages/S1-CamMon/README.md)。Samba 与 VFS 模块采用 GPL-3.0-or-later，许可证见 [native/COPYING](native/COPYING)。

实现参考：[Samba VFS](https://github.com/samba-team/samba/blob/samba-4.25.0/examples/VFS/skel_transparent.c)、[Samba 配置](https://www.samba.org/samba/docs/current/man-html/smb.conf.5.html)、[nmbd](https://www.samba.org/samba/docs/current/man-html/nmbd.8.html)、[smbprotocol](https://github.com/jborean93/smbprotocol)、[libnfs](https://github.com/sahlberg/libnfs)、[Ganesha MEM 测试导出](https://github.com/nfs-ganesha/nfs-ganesha/blob/next/src/config_samples/mem.conf)、[Psycopg](https://www.psycopg.org/psycopg3/docs/basic/usage.html)、[PostgreSQL 锁](https://www.postgresql.org/docs/current/explicit-locking.html)。
