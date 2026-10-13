# S1 全局 bug / 中断记录

> 用途：复制到具体 Stage 后，记录游离于单个编号需求之外，或暂时不适合直接归档到单需求文档中的 bug 与中断事件。

## 文档内容范围、结构与格式约定（必读）

### 1. 本文档负责什么

本文档用于记录游离于单个编号需求之外，或暂时不适合直接归档到单需求文档中的 bug 与中断事件。

本文档只负责：

1. 记录跨需求 bug。
2. 记录开发过程中临时爆发、会打断当前需求推进的问题。
3. 记录暂时无法明确归属的历史遗留 bug。
4. 为 bug 与需求之间建立双向链接。

本文档不负责：

1. 代替单需求文档保存该需求的完整业务上下文。
2. 记录所有测试细节。
3. 记录逐字聊天过程。

### 2. 本文档固定结构

本文档必须长期保持以下结构：

1. 文档内容范围、结构与格式约定
2. 使用规则
3. bug / 中断总表
4. 条目明细
5. 更新日志

### 3. 本文档记录格式

每个条目必须使用以下字段：

- `BUG-ID`：唯一编号，格式 `S1-BUG-XXX`
- `标题`
- `发现时间`
- `发现来源`
- `当前状态`：`open`、`in-progress`、`blocked`、`fixed`、`closed`
- `归属类型`：`independent`、`related-to-requirement`、`cross-requirement`、`unknown`
- `关联需求`
- `现象 / 影响`
- `处理结果`
- `回归情况`

## 使用规则

1. 开发 A 需求时若被 bug 打断，先在本文档登记，再决定是否同步写入 A 或 B 的需求文档。
2. 若 bug 明确只属于某个需求，仍可主要记录在对应需求文档；但若它打断了多个需求或后续需要追踪来源，建议同时登记在本文档。
3. bug 修复完成后，若影响到某个需求状态，必须同步更新对应需求文档与 [requirements-index.md](requirements-index.md)。
4. 本文档优先解决“先落地记录，避免上下文丢失”的问题；归属可以后补，不要求首次登记时完全准确。

## bug / 中断总表

| BUG-ID | 标题 | 发现时间 | 当前状态 | 归属类型 | 关联需求 |
| --- | --- | --- | --- | --- | --- |
| S1-BUG-001 | Samba 自定义模块缺失加载入口 | 2026-10-08 | fixed | cross-requirement | S1-RQ-001 / S1-RQ-002 |
| S1-BUG-002 | Samba 路径引用重开与句柄身份不一致 | 2026-10-08 | fixed | cross-requirement | S1-RQ-001 / S1-RQ-002 |
| S1-BUG-003 | 旧版 libnfs 的 NFSv4 上传重试 EEXIST | 2026-10-08 | fixed | cross-requirement | S1-RQ-001 / S1-RQ-002 |
| S1-BUG-004 | 远程删除与停机占用摄像机全局锁 | 2026-10-08 | fixed | cross-requirement | S1-RQ-001 / S1-RQ-002 |
| S1-BUG-005 | 旧版 libnfs 的 NFSv4 客户端身份冲突 | 2026-10-08 | fixed | cross-requirement | S1-RQ-001 / S1-RQ-003 |
| S1-BUG-006 | slim 镜像缺少网络名称映射导致 NFS 测试服务启动失败 | 2026-10-09 | fixed | cross-requirement | S1-RQ-001 / S1-RQ-003 |
| S1-BUG-007 | 旧版 libnfs 无法编码较大 NFSv4 写入 | 2026-10-09 | fixed | cross-requirement | S1-RQ-001 / S1-RQ-003 |
| S1-BUG-008 | GHCR Public 状态未满足原私有交付要求 | 2026-10-09 | closed | cross-requirement | S1-RQ-003 |
| S1-BUG-009 | NAS 已有 NetBIOS UDP 端口阻止 SMB 文件服务启动 | 2026-10-12 | fixed | cross-requirement | S1-RQ-001 / S1-RQ-003 |
| S1-BUG-010 | NAS 缓存测试文件无法通过 SMB 删除 | 2026-10-13 | open | related-to-requirement | S1-RQ-001 |

| S1-BUG-011 | 浏览器测试共享存储导致播放首帧断言失败 | 2026-10-13 | fixed | related-to-requirement | S1-RQ-003 |

## 条目明细

### S1-BUG-001

- `BUG-ID`：`S1-BUG-001`
- `标题`：Samba 自定义模块缺失加载入口
- `发现时间`：2026-10-08。
- `发现来源`：原生联调或并发故障测试。
- `当前状态`：`fixed`。
- `归属类型`：`cross-requirement`。
- `关联需求`：[S1-RQ-001](requirements/REQ-001-gateway.md)、[S1-RQ-002](requirements/REQ-002-archive.md)。
- `现象 / 影响`：原生客户端 tree connect 失败。
- `处理结果`：构建声明指定 init_function='vfs_cammon_init'，导出 samba_init_module，并与 smbd 同版本编译。
- `回归情况`：SMB1 完整协议测试通过。

### S1-BUG-002

- `BUG-ID`：`S1-BUG-002`
- `标题`：Samba 路径引用重开与句柄身份不一致
- `发现时间`：2026-10-08。
- `发现来源`：原生联调或并发故障测试。
- `当前状态`：`fixed`。
- `归属类型`：`cross-requirement`。
- `关联需求`：[S1-RQ-001](requirements/REQ-001-gateway.md)、[S1-RQ-002](requirements/REQ-002-archive.md)。
- `现象 / 影响`：目录枚举拒绝 /proc/self/fd；O_PATH 重开时关闭旧 fd 会错误释放新 RPC 句柄。
- `处理结果`：解析并限定内部描述符路径到共享根；每个物理 fd 独立保存 RPC 句柄；枚举改用索引分页。
- `回归情况`：目录、合法改名、缓存释放后远程读取及客户端断开均通过。

### S1-BUG-003

- `BUG-ID`：`S1-BUG-003`
- `标题`：旧版 libnfs 的 NFSv4 上传重试 EEXIST
- `发现时间`：2026-10-08。
- `发现来源`：原生联调或并发故障测试。
- `当前状态`：`fixed`。
- `归属类型`：`cross-requirement`。
- `关联需求`：[S1-RQ-001](requirements/REQ-001-gateway.md)、[S1-RQ-002](requirements/REQ-002-archive.md)。
- `现象 / 影响`：上传中断留下临时文件，重启重试无法重新打开。
- `处理结果`：CREATE 返回 EEXIST 后以非 CREATE 模式打开并截断，保留全部源录像直至验证发布。
- `回归情况`：NFSv3/v4.0 实际远程半文件上传、重试及重启恢复通过。

### S1-BUG-004

- `BUG-ID`：`S1-BUG-004`
- `标题`：远程删除与停机占用摄像机全局锁
- `发现时间`：2026-10-08。
- `发现来源`：原生联调或并发故障测试。
- `当前状态`：`fixed`。
- `归属类型`：`cross-requirement`。
- `关联需求`：[S1-RQ-001](requirements/REQ-001-gateway.md)、[S1-RQ-002](requirements/REQ-002-archive.md)。
- `现象 / 影响`：后端删除、读取句柄关闭或长转码可能阻塞写入，或在 SQLite 关闭后仍运行。
- `处理结果`：网络删除先声明状态再释放全局锁；关闭句柄在句柄锁内完成；转码可取消，后台退出后才关闭数据库。
- `回归情况`：网络等待不阻塞新录像、读取租约延后删除、转码取消及协议会话清理测试通过。

### S1-BUG-005

- `BUG-ID`：`S1-BUG-005`
- `标题`：NFSv4 多连接的默认客户端身份冲突。
- `发现时间`：2026-10-08。
- `发现来源`：保持读取句柄时，新建 stat/SHA256 校验连接的真实协议测试。
- `当前状态`：`fixed`。
- `归属类型`：`cross-requirement`。
- `关联需求`：[S1-RQ-001](requirements/REQ-001-gateway.md)、[S1-RQ-003](requirements/REQ-003-management.md)。
- `现象 / 影响`：旧版 libnfs 的多个上下文共享默认客户端身份，重新 SETCLIENTID 使已有读取状态失效，返回 NFS4ERR_EXPIRED。
- `处理结果`：每个 NFSv4 上下文设置独立 UUID 客户端身份，保留读取和校验连接各自的状态。
- `回归情况`：NFSv3/v4.0 均通过保持读取句柄、新建 stat/完整 SHA256 连接、继续范围读取的测试。

### S1-BUG-006

- `BUG-ID`：`S1-BUG-006`
- `标题`：slim 镜像缺少网络名称映射导致 NFS 测试服务启动失败。
- `发现时间`：2026-10-09。
- `发现来源`：[首次 Docker CI](https://github.com/hsyhhssyy/CamMon/actions/runs/37926470601)，amd64 / arm64 均出现 78 项通过、4 项 NFS fixture 错误。
- `当前状态`：`fixed`。
- `归属类型`：`cross-requirement`。
- `关联需求`：[S1-RQ-001](requirements/REQ-001-gateway.md)、[S1-RQ-003](requirements/REQ-003-management.md)。
- `现象 / 影响`：Ganesha 不能向 rpcbind 注册 NFS V3 UDP；原 fixture 丢弃 rpcbind 日志，无法直接显示网络服务名解析错误。
- `处理结果`：运行镜像显式安装 netbase，提供 /etc/services、/etc/protocols 和 /etc/rpc；fixture 保存 rpcbind 日志，并等待 111 端口可用再启动 Ganesha。移除显式 -h 127.0.0.1，避免 Debian rpcbind 1.2.6 重复加入默认 loopback 地址导致 double free。
- `回归情况`：隔离 chroot 复现网络映射及重复 loopback 绑定问题；v0.1.3 云端 amd64 / arm64 均通过全部 82 项容器测试。

### S1-BUG-007

- `BUG-ID`：`S1-BUG-007`
- `标题`：Debian libnfs 4.0.0 无法编码较大 NFSv4 写入请求。
- `发现时间`：2026-10-09。
- `发现来源`：[v0.1.2 云端容器回归](https://github.com/hsyhhssyy/CamMon/actions/runs/37929913732)，两种架构均为 80 项通过、2 项 NFSv4 写入失败。
- `当前状态`：`fixed`。
- `归属类型`：`cross-requirement`。
- `关联需求`：[S1-RQ-001](requirements/REQ-001-gateway.md)、[S1-RQ-003](requirements/REQ-003-management.md)。
- `现象 / 影响`：NFSv3 正常；NFSv4 上传较大缓冲区返回 Failed to encode COMPOUND4args，源录像仍保留缓存，发布被检查阻止。
- `处理结果`：独立构建固定 libnfs 5.0.2 并校验源码 SHA256，桥接链接该版本；镜像携带对应动态库及许可证。测试文件增至超过 1 MiB，覆盖完整写入块与尾块。
- `回归情况`：用 Debian 4.0.0 库在本机复现相同 2 项失败；源码构建 5.0.2 后全部 4 项 NFS 测试通过。v0.1.3 云端 amd64 / arm64 均通过 82 项容器测试，包含超过 1 MiB 的 NFSv3/v4.0 上传、校验与重试。

### S1-BUG-008

- `BUG-ID`：`S1-BUG-008`
- `标题`：已上传的 cammon 包为 Public，未满足私有镜像交付。
- `发现时间`：2026-10-09。
- `发现来源`：[v0.1.3 发布后校验](https://github.com/hsyhhssyy/CamMon/actions/runs/37931012436)，应用、协议、镜像上传及多架构检查成功，最终 Private 校验失败。
- `当前状态`：`closed`，2026-10-10 用户撤销私有镜像要求，接受公开镜像。
- `归属类型`：`cross-requirement`。
- `关联需求`：[S1-RQ-003](requirements/REQ-003-management.md)。
- `现象 / 影响`：API 预检 404 未能证明包名未占用；cammon:0.1.3 可匿名拉取，包页面显示 Public。GitHub 不允许 Public 包改回 Private。
- `处理结果`：选择 cammon-private 新包名并同步 Compose、模板及说明；匿名拉取预检补充 API 检查，上传 digest 后再次确认 Private，再生成下载标签。
- `回归情况`：v0.1.4 应用、amd64 / arm64 的全部 82 项容器测试和生产上传通过；新包上传后 API 实际返回 Public，版本标签发布被阻止。两个新包均出现该行为，需确认独立私有发布仓库或现有仓库改私有的方式，未将发布判为成功。
- `关闭结果`：2026-10-10 用户明确接受公开镜像；cammon:0.1.3 和 latest 已验证匿名 HTTP 200、相同 digest 和双架构支持，部署地址与 CI 检查改回 Public。

### S1-BUG-009

- `BUG-ID`：`S1-BUG-009`
- `标题`：NAS 已有 NetBIOS UDP 端口阻止 SMB 文件服务启动。
- `发现时间`：2026-10-12。
- `发现来源`：目标 NAS 192.168.31.26:18080 管理页 HTTP 200，/healthz HTTP 503，明确提示 UDP 137/138 被占用；用户确认端口无法释放。
- `当前状态`：`fixed`，本机、双架构云端及目标 NAS 更新后的启动 / 协议现场检查通过。
- `归属类型`：`cross-requirement`。
- `关联需求`：[S1-RQ-001](requirements/REQ-001-gateway.md)、[S1-RQ-003](requirements/REQ-003-management.md)。
- `现象 / 影响`：启动检查把 UDP 发现和 TCP 文件服务一起判为必需，NAS 原有发现服务会阻止 CamMon smbd 启动。
- `处理结果`：新增 auto / internal / external 模式；auto 在任一 UDP 端口不可用时跳过 nmbd，external 明确沿用宿主机发现，internal 保留独占要求。TCP 冲突始终阻止启动；健康接口及管理页显示实际发现模式；NAS 无需卸载已有服务。
- `回归情况`：15 项本机真实 SMB 测试通过，包括已有 nmbd 名称响应、UDP 单独 / 同时占用、TCP 139/445 的 SMB1 登录和写读、健康检查及预检退出码；[0.1.5 云端发布](https://github.com/hsyhhssyy/CamMon/actions/runs/38253526834)全部成功，各架构通过 92 项容器测试。目标 NAS 更新后健康 HTTP 200、external 模式正常、TCP 139/445 均可协商 SMB1、原有 NetBIOS 仍响应。NAS 实际设备认证、录像写读与转存尚未测试。

### S1-BUG-010

- `BUG-ID`：`S1-BUG-010`
- `标题`：NAS 缓存测试文件无法通过 SMB 删除。
- `发现时间`：2026-10-13。
- `发现来源`：管理员授权现场检查的临时联调；用户随后明确只需要验证 PostgreSQL 初始化。
- `当前状态`：`open`，按用户范围停止进一步录像联调，未开始实现修复。
- `归属类型`：`related-to-requirement`。
- `关联需求`：[S1-RQ-001](requirements/REQ-001-gateway.md)。
- `现象 / 影响`：独立测试设备的 cached 文件、写句柄数为 0 时，SMB1 与 SMB3 删除均返回 STATUS_ACCESS_DENIED；目录也无法清理。具体原因尚未确认，不将缓存删除现场验收判为通过。
- `处理结果`：仅对独立测试设备重新启用并清理，使用覆盖方式把 2242 字节测试视频清空为 0 字节，然后再次停用设备。未配置真实存储，未改动真实设备；元数据同步已确认提交。
- `回归情况`：尚未修复；剩余停用测试设备和零字节文件记录，后续需要补充缓存删除的正向协议回归及清理入口。此前视频读回断言未完成，不能将本次录像读写联调标记通过。

### S1-BUG-011

- `BUG-ID`：`S1-BUG-011`
- `标题`：浏览器测试共享存储导致播放首帧断言失败。
- `发现时间`：2026-10-13。
- `发现来源`：[v0.1.9 云端发布检查](https://github.com/hsyhhssyy/CamMon/actions/runs/38423142319)。
- `当前状态`：`fixed`，本地两个 worker 和单 worker 回归通过，云端发布回归待核验。
- `归属类型`：`related-to-requirement`。
- `关联需求`：[S1-RQ-003](requirements/REQ-003-management.md)。
- `现象 / 影响`：四项测试共用一个网关；CI 两个 worker 先完成管理测试，该测试更换并删除本地存储，随后播放测试无法读取已生成的首帧，naturalWidth 断言超时。v0.1.9 镜像上传被阻止。
- `处理结果`：每项浏览器测试启动独立 uvicorn 进程，分配空闲端口和临时录像 / 存储目录，结束时停止进程并清理目录，消除顺序及并发状态串扰。
- `回归情况`：4 项浏览器测试在两个 worker 并发及单 worker 顺序配置均通过；v0.1.10 云端回归待核验。

## 更新日志

- 2026-10-08：记录并修复 5 类原生协议、恢复和并发链路问题。
- 2026-10-09：登记首个云端容器回归发现的网络基础包缺失，保持协议测试启用并修复部署依赖。
- 2026-10-09：NFS 服务启动修正后发现旧版 libnfs 写入编码限制，固定升级至已验证的源码版本并增加大块上传回归。
- 2026-10-10：用户接受公开镜像并请求 NAS Compose，验证既有镜像可匿名访问，关闭私有交付中断。
- 2026-10-12：修复宿主机 NetBIOS UDP 占用阻止摄像机文件服务启动，增加发现共存配置和真实协议验收。
- 2026-10-12：目标 NAS 更新后确认 UDP 共存修复生效，健康与 SMB1 / NetBIOS 协议现场检查通过。
- 2026-10-13：确认 NAS PostgreSQL 初始化及事务同步；记录缓存测试文件删除受拒，按用户范围停止录像联调并清空测试视频。

- 2026-10-13：v0.1.9 发布被浏览器共享存储干扰阻止，新增独立服务器测试 fixture，以 v0.1.10 重新发布。
