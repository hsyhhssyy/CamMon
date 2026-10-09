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
| S1-BUG-006 | slim 镜像缺少网络名称映射导致 NFS 测试服务启动失败 | 2026-10-09 | in-progress | cross-requirement | S1-RQ-001 / S1-RQ-003 |

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
- `当前状态`：`in-progress`，修复已应用，等待云端容器回归。
- `归属类型`：`cross-requirement`。
- `关联需求`：[S1-RQ-001](requirements/REQ-001-gateway.md)、[S1-RQ-003](requirements/REQ-003-management.md)。
- `现象 / 影响`：Ganesha 不能向 rpcbind 注册 NFS V3 UDP；原 fixture 丢弃 rpcbind 日志，无法直接显示网络服务名解析错误。
- `处理结果`：运行镜像显式安装 netbase，提供 /etc/services、/etc/protocols 和 /etc/rpc；fixture 保存 rpcbind 日志，并等待 111 端口可用再启动 Ganesha。
- `回归情况`：隔离 chroot 中移除映射时 rpcbind 不监听 IP 端口，补回映射后正常绑定；本机 4 项 NFSv3/v4.0 测试通过。准备 0.1.1 修复版本，云端回归待完成。

## 更新日志

- 2026-10-08：记录并修复 5 类原生协议、恢复和并发链路问题。
- 2026-10-09：登记首个云端容器回归发现的网络基础包缺失，保持协议测试启用并修复部署依赖。
