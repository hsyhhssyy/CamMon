# CamMon 摄像机录像存储网关

CamMon 在 NAS 的 IP 上提供 SMB1 共享和 NetBIOS 发现服务。每台设备拥有独立账号、共享和保留天数；设备内按字符串相机编号 X 管理录像。摄像机写入本地缓存，完成的录像整文件转存至 SMB2/3 或 NFS。过期录像按北京时间自然日生成 60 倍速 H.264 MP4，归档永久保留。

管理页面提供设备配置、后端连接测试、组合统计、分页文件列表、字节范围下载、容量告警和归档任务重试。前端资源随镜像提供，运行时无需外部字体或 CDN。

PostgreSQL 保存元数据。运行时的配置、索引和待同步变更全部缓存在内存中，PostgreSQL 断线后当前进程继续服务。新增录像使用临时磁盘缓存；重启允许丢失临时录像及未同步变更。

## NAS 部署

需要 Linux NAS、Docker Compose、可用的 PostgreSQL、足够的临时录像空间和运行内存，以及独占的 UDP 137/138、TCP 139/445。NAS 原有 SMB 服务使用同样端口时，应先由管理员调整 NAS 服务。CamMon 会报告冲突端口，并保留管理页面用于查看错误。

### 使用 GHCR 私有镜像

将 [compose.ghcr.yaml](compose.ghcr.yaml) 和 [.env.ghcr.example](.env.ghcr.example) 放到 NAS 的同一目录即可，不需要源码或本地构建。镜像通过发布工作流生成并保持 Private。NAS 先用有此包读取权限的 GitHub 账号登录 GHCR，登录密码使用具有 `read:packages` 权限的 classic PAT。默认使用 `ghcr.io/hsyhhssyy/cammon-private:latest`，手动 CI 发布的镜像使用 `edge`，也可在 `.env` 中指定已发布的版本或 digest。

```bash
cp .env.ghcr.example .env
docker login ghcr.io -u hsyhhssyy
# 在交互提示中输入 classic PAT；这与下面生成的应用加密密钥不同。
# 生成密钥：这个临时容器只运行生成命令，不启动摄像机服务，也不需要数据库。
docker run --rm --entrypoint python ghcr.io/hsyhhssyy/cammon-private:latest \
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
| UDP 137、138 | NetBIOS 名称解析与发现 |
| TCP 18080 | 管理页面，可用 `CAMMON_PORT` 修改 |

启动后访问 `http://NAS-IP:18080`。配置不创建 `/data` 或任何持久化数据卷；元数据使用 PostgreSQL 和内存，Samba 运行文件在 tmpfs，新增录像使用容器临时 `/cache`。PostgreSQL 需要在首次启动和重启时在线，运行期间断线仍按内存配置服务。

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

镜像默认使用普通容器内的 root 管理 Samba 独立 Unix 账号。Compose 使用 host 网络，无 `privileged`、额外 capability、FUSE 设备或宿主机 SMB/NFS 挂载，也不挂载本地持久数据目录。`/run/cammon` 使用 tmpfs；`/cache` 使用容器临时可写层。不要设置容器为非 root，也不要运行多个 CamMon 实例共用同一 PostgreSQL schema。

构建会从固定的 Samba **4.25.0** 源码同时生成 `smbd`、`nmbd` 和 VFS 模块，并编译固定版本的 **libnfs 5.0.2**，两份源码均校验 SHA256。libnfs 单独构建并与 CFFI 桥接配套，避免 Debian 旧版客户端无法编码较大 NFSv4 写入。首次构建需要网络和一定编译时间。`CAMMON_BUILD_JOBS` 默认 2，可按 NAS 内存调整。

## CI 构建与 GHCR 镜像

[Publish to GHCR](.github/workflows/publish.yml) 在推送 `v` 开头的语义化版本标签或从 GitHub Actions 手动运行时发布镜像。先将项目代码和工作流提交并推送到 GitHub，再发布版本：

```bash
git tag v0.1.4
git push origin v0.1.4
```

镜像地址采用小写仓库名加 `-private` 后缀，当前为 `ghcr.io/hsyhhssyy/cammon-private`。`v0.1.4` 发布 `0.1.4`、`0.1`、`latest` 和 `sha-完整提交哈希` 标签；`v0.1.4-rc.1` 只发布预发布版本和 SHA 标签，不更新稳定版本标签。手动运行发布 `edge` 和 SHA 标签。`latest` 指最近一次成功发布的稳定版本。

发布前复用 [CamMon checks](.github/workflows/ci.yml)，执行 Python、PostgreSQL、前端构建、浏览器和真实协议测试。amd64 与 arm64 分别使用原生 Linux runner 测试、构建，并复用 BuildKit 缓存；全部检查通过后才上传 `production` 镜像，最终合并为支持 `linux/amd64` 和 `linux/arm64` 的镜像。PR 和普通分支推送执行检查；发布流程不重复触发另一套标签检查。

GHCR 登录使用 Actions 自带的 `GITHUB_TOKEN`，仅发布任务授予 `packages: write`，可见性预检只授予 `packages: read`，无需另存发布用的 PAT；CI 使用临时测试数据库，不需要部署环境的 PostgreSQL 连接和凭据。若同名包已存在，须在包的 “Manage Actions access” 中允许此仓库写入。按当前要求保持包为 Private；工作流在发布前检查已有包的可见性，已有公开包会阻止发布，首次创建采用 GHCR 默认私有设置，上传 digest 后先验证 Private 再创建下载标签，发布后再次验证 Private。私有包拉取按 [GitHub Container registry 文档](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry) 登录。

NAS 上直接使用私有镜像见前文“使用 GHCR 私有镜像”。已有源码构建部署也可以登录 GHCR 并将 `.env` 中的 `CAMMON_IMAGE` 改为 `ghcr.io/hsyhhssyy/cammon-private:0.1.4`，使用原 Compose 拉取启动：

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

PostgreSQL 保存 `settings`、`devices`、`directories`、`recordings`、`archive_jobs`、`coverage`、`sessions` 表，分别包含配置、设备、目录、录像索引、归档任务、跨日覆盖关系与会话哈希。录像字节仍在临时缓存及 SMB/NFS 后端；远程存储密码和摄像机密码使用 `CAMMON_SECRET_KEY` 加密后保存到 PostgreSQL，管理员密码只保存哈希。Samba 的临时凭据库在 tmpfs 中，启动时从 PostgreSQL 中的凭据重建。

进程启动时，在一致性事务中从 PostgreSQL 加载配置、元数据与索引，重建虚拟目录和 Samba 凭据。随后使用 SQLite `:memory:` 管理运行索引及事务队列，临时 SQL 数据同样只用内存，不生成 SQLite、WAL 或本地密钥文件。独立线程默认每 5 秒分批同步；关联记录和凭据一起保存，使已提交批次可重新加载。远端事务提交后才确认内存队列，重复确认可幂等重放，连续修改按记录合并。

启动后 PostgreSQL 断线，当前进程继续按内存配置服务：允许修改设备和存储配置，录像写入、封存、读取及转存继续运行。连接恢复后自动补同步。“真实存储”页面显示连接状态、最近同步时间和待同步数；单独元数据库故障不将摄像机服务标记为不健康。SMB/NFS 故障仍按缓存容量规则处理。

重启时内存配置和待同步变更消失，临时录像及转码文件也会丢弃；只从 PostgreSQL 加载已保存的数据。PostgreSQL 尚不可用时启动会失败并报告原因，不提供断线重启恢复。`CAMMON_SECRET_KEY` 应跨重启保持相同，否则无法解密数据库中已保存的凭据。

每个 schema 供一个运行中的网关使用，通过数据库锁阻止并发写入。管理页面/API 是运行期间的配置写入口；直接修改 PostgreSQL 不会自动刷新内存。当前进程仍存活时，较旧的 PostgreSQL 快照可从内存重新同步；重启后完全采用数据库快照，不覆盖为一份空索引。未知同名表或运行中的远端修订冲突会阻止同步。

## 配置与接入

1. 在“真实存储”页面填写一个全局后端，先点击“测试读写连接”，再保存。
   - SMB：`smb://192.168.1.10/recordings`，可带端口和共享内子目录，账号、密码、域分别填写。使用 SMB2/3；可要求 SMB3 加密。
   - NFS：`nfs://192.168.1.10/volume1/recordings`，必须填写完整导出目录，选择 NFSv3 或 NFSv4.0。默认 UID/GID 65534；在 NAS 上允许配置的 UID/GID 读写并允许网关 IP 访问导出。
2. 在“摄像机管理”添加设备并设置保留天数。保存一次性显示的共享路径、用户名和密码，填入摄像机的 NAS 配置。
3. 为摄像机创建第一层或第二层子文件夹，录像名称使用 `00_20251008080239_20251008082813.mp4`。

共享根目录只允许创建目录，最多两层目录。MP4 位于第一层或第二层目录；X 为数字字符串，`00` 与 `0` 独立。两个时间戳按北京时间解释，时间必须有效，结束不早于开始。随机写入、追加、回改文件头和合法改名均支持；改名后的 X 和时间重新进入索引，目录改名也重新检查深度。缓存录像关闭所有句柄后可以删除，已封存录像不能由摄像机修改、改名或删除。

设备可以停用和重置接入密码；停用停止摄像机访问，管理页面仍可统计与下载该设备的数据。归档视频只在管理页面可见。

默认允许 NTLMv2。若实机只能使用 NTLMv1，在独立的摄像机局域网内配置 `CAMMON_ALLOW_NTLMV1=true`。发现采用 NetBIOS，同一局域网内可看到 CAMMON；不同摄像机的发现、认证方式仍须实机确认。

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
    Cache --> Upload[顺序上传临时文件]
    Upload --> Verify[刷新 / 大小与 SHA256 校验 / 改名]
    Verify --> Remote[SMB2/3 或 NFS 原录像]
    Remote --> Archive[按 X 与北京时间日期生成 60× 归档]
    Archive --> Permanent[校验发布后永久保存]
    Index --> Web[管理页面 / 筛选 / 下载]
```

同一 X 出现时间更晚的录像后，上一段在所有写入句柄关闭且连续 10 秒未修改时封存。最后一段没有后续录像时，关闭句柄且连续 30 分钟未修改也会封存。不同 X 独立处理；迟到的旧片段不会误封存当前片段。

上传至后端临时文件，刷新后核对大小及 SHA256，再改名为最终文件。成功前保留本地源文件；成功后删除本地录像缓存，索引中的路径、大小、文件 ID 保持一致。已打开的本地读取句柄可继续读完，之后读取通过用户态后端客户端完成。枚举、属性、统计均使用索引；共享目录中的零字节文件只是 Samba 的路径占位，不能拿它们当录像备份。

后端故障期间在当前进程中保留缓存并重试。重启丢弃临时录像，不恢复中断的缓存上传；数据库中已保存的归档任务和远程录像可重新加载。空间不足时向摄像机返回 ENOSPC 并在页面告警。空间限制计算缓存录像与归档临时视频的实际总大小；应给缓存预留网络离线和归档工作空间。

每天北京时间 02:00 后入队过期日期，重启后补入当日错过的队列。保留天数包含当天：保留 1 天时只保留今天的普通录像；保留 30 天时保留今天及此前 29 天。默认一次运行一个归档任务。

归档只拼接实际存在的录像，不补齐缺失时段：24 小时约生成 24 分钟，12 小时约生成 12 分钟。按 60 倍时间轴抽帧，输出 H.264、25 fps、无音轨，标准偶数分辨率保持源尺寸。同组分辨率发生变化时统一到首段尺寸。跨午夜片段按日期切分，全部覆盖日期成功归档后才删除原文件；下载句柄正在读取时延后删除。损坏、时长与文件名不符、空间不足或转码失败均保留原件并记录原因。迟到片段生成追加归档，不覆盖已有归档。60 倍代表播放速度，文件大小不保证为原来的 1/60。

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

需要备份的是 PostgreSQL、远程录像和部署配置中的密钥。无需持久化 `/data` 或本地元数据库，也无需保留临时录像缓存。需要选择 SSD 临时录像路径时，可在 Compose override 中仅为 `/cache` 绑定 scratch 目录；其中录像仍按重启时丢弃处理。元数据和 Samba 运行文件保持内存 / tmpfs 模式。

已有录像时禁止直接切换存储协议、地址或 NFS 版本。第一版没有自动迁移工具；更换地址需要独立安排数据与索引迁移。单纯更新账号密码不会迁移或重复统计数据。

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

原生测试会创建并清理临时 Unix 摄像机账号、启动独立 Samba 和 Ganesha MEM 导出，需要测试机的 UDP 137/138、TCP 445、111、2049 空闲。Ganesha 仅用于测试，运行产品不需要 NFS 服务端、rpcbind 或系统挂载。

目前 82 项 Python 测试和浏览器流程通过，已验证实际 SMB1 客户端写入和读取、SMB2/3 后端、NFSv3/v4.0、NetBIOS 发现、FFmpeg 跨日归档、PostgreSQL 18 内存断线服务、冷启动加载和 Samba 原账号重建。上传中断与远程 ENOSPC 使用真实适配器操作周围的可控故障注入。当前开发环境没有 Docker daemon，镜像构建及目标 NAS 的摄像机实机验收仍待部署环境执行；CI 已配置完整镜像构建与容器内协议测试。

需求和验收记录见 [Stage S1](.docs/stages/S1-CamMon/README.md)。Samba 与 VFS 模块采用 GPL-3.0-or-later，许可证见 [native/COPYING](native/COPYING)。

实现参考：[Samba VFS](https://github.com/samba-team/samba/blob/samba-4.25.0/examples/VFS/skel_transparent.c)、[Samba 配置](https://www.samba.org/samba/docs/current/man-html/smb.conf.5.html)、[nmbd](https://www.samba.org/samba/docs/current/man-html/nmbd.8.html)、[smbprotocol](https://github.com/jborean93/smbprotocol)、[libnfs](https://github.com/sahlberg/libnfs)、[Ganesha MEM 测试导出](https://github.com/nfs-ganesha/nfs-ganesha/blob/next/src/config_samples/mem.conf)、[Psycopg](https://www.psycopg.org/psycopg3/docs/basic/usage.html)、[PostgreSQL 锁](https://www.postgresql.org/docs/current/explicit-locking.html)。
