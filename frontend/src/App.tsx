import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Activity, AlertCircle, Archive, BarChart3, Camera, Check, ChevronLeft, ChevronRight, Copy, Database, Download, HardDrive, LogOut, Plus, RefreshCw, Server, Settings, ShieldCheck, Video, X } from 'lucide-react'
import { api, bytes, datetime, type Device, type FilePage, type Statistics, type Status, type Storage } from './api'

type View = 'overview' | 'files' | 'devices' | 'storage'
type Filters = { year: string; month: string; day: string; device_id: string; channel: string; kind: string }
const emptyFilters: Filters = { year: '', month: '', day: '', device_id: '', channel: '', kind: '' }
const blankStorage: Storage = { protocol: 'nfs', address: '', username: '', domain: '', password: null, encrypt: false, nfs_version: 3, uid: 65534, gid: 65534 }
const stateLabels: Record<string, string> = { cached: '本地缓存', pending: '等待落盘', uploading: '正在落盘', stored: '已存储', queued: '等待归档', running: '正在归档', failed: '失败', done: '完成' }

function Dialog({ title, children, close }: { title: string; children: ReactNode; close: () => void }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => { ref.current?.showModal() }, [])
  return <dialog ref={ref} onCancel={close} onClose={close}>
    <div className="dialog-title"><h2>{title}</h2><button className="icon-button" aria-label="关闭" onClick={close}><X size={20}/></button></div>{children}
  </dialog>
}

async function copy(value: string) {
  if (navigator.clipboard) return navigator.clipboard.writeText(value)
  const field = document.createElement('textarea'); field.value = value; document.body.appendChild(field)
  field.select(); document.execCommand('copy'); field.remove()
}

export default function App() {
  const [user, setUser] = useState<string | null>(null)
  const [checking, setChecking] = useState(true)
  const [view, setView] = useState<View>('overview')
  const [devices, setDevices] = useState<Device[]>([])
  const [status, setStatus] = useState<Status | null>(null)
  const [storage, setStorage] = useState<Storage>(blankStorage)
  const [filter, setFilter] = useState<Filters>(emptyFilters)
  const [files, setFiles] = useState<FilePage | null>(null)
  const [stats, setStats] = useState<Statistics | null>(null)
  const [offset, setOffset] = useState(0)
  const [revision, setRevision] = useState(0)
  const [toast, setToast] = useState<{ text: string; error?: boolean } | null>(null)
  const [busy, setBusy] = useState(false)
  const [deviceDialog, setDeviceDialog] = useState<Device | 'new' | null>(null)
  const [credentials, setCredentials] = useState<Device | null>(null)
  const [fileLoading, setFileLoading] = useState(false)

  function notify(text: string, error = false) { setToast({ text, error }) }
  function failure(error: unknown) {
    if ((error as Error & { status?: number }).status === 401) setUser(null)
    notify(error instanceof Error ? error.message : '请求失败', true)
  }
  useEffect(() => { api<{ username: string }>('/auth/me').then(data => setUser(data.username)).catch(() => {}).finally(() => setChecking(false)) }, [])
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(null), 6500); return () => clearTimeout(timer) }, [toast])
  async function reload() {
    try {
      const [newDevices, newStatus] = await Promise.all([api<Device[]>('/devices'), api<Status>('/status')])
      setDevices(newDevices); setStatus(newStatus); setRevision(r => r + 1)
    } catch (error) { failure(error) }
  }
  useEffect(() => {
    if (!user) return
    void reload()
    api<Storage | null>('/storage').then(data => setStorage(data ?? blankStorage)).catch(failure)
    const timer = setInterval(reload, 15000)
    return () => clearInterval(timer)
  }, [user])

  const query = new URLSearchParams(Object.entries(filter).filter(([, value]) => value)).toString()
  useEffect(() => {
    if (!user) return
    let active = true
    const timer = setTimeout(() => {
      setFileLoading(true)
      Promise.all([api<FilePage>(`/files?${query}&limit=50&offset=${offset}`), api<Statistics>(`/stats?${query}`)])
        .then(([page, totals]) => { if (active) { setFiles(page); setStats(totals) } })
        .catch(error => { if (active) failure(error) })
        .finally(() => { if (active) setFileLoading(false) })
    }, 150)
    return () => { active = false; clearTimeout(timer) }
  }, [user, query, offset, revision])

  function changeFilter(key: keyof Filters, value: string) { setFilter(f => ({ ...f, [key]: value })); setOffset(0) }
  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget); setBusy(true)
    try { const result = await api<{ username: string }>('/auth/login', 'POST', Object.fromEntries(data)); setUser(result.username) }
    catch (error) { failure(error) } finally { setBusy(false) }
  }

  if (checking) return <div className="loading-screen"><Camera size={34}/><p>正在连接 CamMon…</p></div>
  if (!user) return <div className="login-screen"><div className="login-art"><div className="brand"><Camera/><span>CamMon<span className="brand-dot">.</span></span></div><h1>让每一段录像，<br/>都有它的位置。</h1><p>本地缓存 · 可靠存储 · 时间归档</p><div className="orbit"><div/><Camera size={52}/></div></div>
    <form className="login-card" onSubmit={login}><span className="eyebrow">CAMERA STORAGE GATEWAY</span><h2>登录管理中心</h2><p className="muted">管理你的摄像机、录像与存储空间。</p>
      <label>用户名<input name="username" autoComplete="username" defaultValue="admin" required/></label><label>密码<input name="password" type="password" autoComplete="current-password" required/></label>
      <button className="primary" disabled={busy}>{busy ? '正在登录…' : '登录'}</button><small className="muted">使用部署时设置的管理员账号。</small>
      {toast && <p className="inline-error" role="alert">{toast.text}</p>}
    </form></div>

  const titles: Record<View, [string, string]> = { overview: ['存储总览', '录像流转与空间使用，一目了然。'], files: ['录像文件', '按设备、日期和录像类型查看当前保留的数据。'], devices: ['摄像机管理', '每台设备独立接入，保留策略灵活设置。'], storage: ['真实存储', '完成的录像整文件转存到这里。'] }
  const ordinary = stats?.groups.filter(g => g.kind === 'original').reduce((total, g) => total + g.bytes, 0) ?? 0
  const archived = stats?.groups.filter(g => g.kind === 'archive').reduce((total, g) => total + g.bytes, 0) ?? 0
  const days = [...new Set(stats?.groups.map(g => g.day) ?? [])].slice(-30)
  const dayValues = days.map(day => ({ day, original: stats?.groups.find(g => g.day === day && g.kind === 'original')?.bytes ?? 0, archive: stats?.groups.find(g => g.day === day && g.kind === 'archive')?.bytes ?? 0 }))
  const chartMax = Math.max(1, ...dayValues.map(g => g.original + g.archive))
  const cacheTotal = (status?.cache_bytes ?? 0) + (status?.cache_available_bytes ?? 0)

  return <div className="app-layout"><aside className="sidebar"><div className="brand"><Camera size={25}/><span>CamMon<span className="brand-dot">.</span></span></div><div className="workspace-label">录像管理中心</div>
    <nav>{([
      ['overview', BarChart3, '存储总览'], ['files', Video, '录像文件'], ['devices', Camera, '摄像机管理'], ['storage', Settings, '真实存储'],
    ] as const).map(([key, Icon, label]) => <button key={key} className={view === key ? 'active' : ''} onClick={() => setView(key)}><Icon size={19}/><span>{label}</span></button>)}</nav>
    <div className="sidebar-footer"><span className="small-status"><span className="dot"/>北京时间 · UTC+8</span><div className="user"><span className="avatar">{user[0].toUpperCase()}</span><div><strong>{user}</strong><small>管理员</small></div><button className="icon-button" aria-label="退出登录" onClick={async () => { try { await api('/auth/logout', 'POST'); setUser(null) } catch (e) { failure(e) } }}><LogOut size={17}/></button></div></div>
  </aside><main><header className="topbar"><span>工作空间 <span className="breadcrumb">/</span> {titles[view][0]}</span><span className="top-status"><ShieldCheck size={15}/> 独立设备账户</span></header>
    <div className="page"><div className="page-heading"><div><span className="eyebrow">CAMMON / {view.toUpperCase()}</span><h1>{titles[view][0]}</h1><p>{titles[view][1]}</p></div><div className="heading-actions"><button className="secondary" onClick={reload}><RefreshCw size={16}/>刷新</button>{view === 'devices' && <button className="primary" onClick={() => setDeviceDialog('new')}><Plus size={17}/>添加摄像机</button>}</div></div>
      {status?.protocol_error && <div className="alert"><AlertCircle size={20}/><div><strong>摄像机接入服务需要处理</strong><p>{status.protocol_error}</p></div></div>}
      {status?.worker_error && <div className="alert"><AlertCircle size={20}/><div><strong>后台任务异常</strong><p>{status.worker_error}</p></div></div>}
      {status?.cache_error && <div className="alert"><AlertCircle size={20}/><div><strong>缓存容量告警</strong><p>{status.cache_error}</p></div></div>}
      {status?.metadata.error && <div className="alert"><Database size={20}/><div><strong>元数据暂存在内存中</strong><p>{status.metadata.error}；{status.metadata.pending_rows} 条元数据等待同步，摄像机继续按内存配置服务。</p></div></div>}
      {!!status?.alerts.length && <div className="alert"><AlertCircle size={20}/><div><strong>{status.alerts.length} 段录像需要处理</strong><p>{status.alerts.slice(0, 3).map(item => `${item.path}: ${item.error}`).join('；')}</p><button className="text-button" onClick={() => { setFilter(emptyFilters); setView('files') }}>查看录像详情</button></div></div>}

      {(view === 'overview' || view === 'files') && <>
        {view === 'files' && <section className="panel filter-panel"><div className="panel-heading"><h2>组合筛选</h2><button className="text-button" onClick={() => { setFilter(emptyFilters); setOffset(0) }}>重置</button></div><div className="filters">
          <label>摄像机<select value={filter.device_id} onChange={e => changeFilter('device_id', e.target.value)}><option value="">全部设备</option>{devices.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select></label>
          <label>相机编号 X<input placeholder="全部编号" value={filter.channel} onChange={e => changeFilter('channel', e.target.value)}/></label>
          <label>年份<input type="number" min="1" max="9999" placeholder="全部年份" value={filter.year} onChange={e => changeFilter('year', e.target.value)}/></label>
          <label>月份<select value={filter.month} onChange={e => changeFilter('month', e.target.value)}><option value="">全部月份</option>{Array.from({ length: 12 }, (_, i) => <option key={i} value={i + 1}>{i + 1} 月</option>)}</select></label>
          <label>指定日期<input type="date" value={filter.day} onChange={e => changeFilter('day', e.target.value)}/></label>
          <label>视频类型<select value={filter.kind} onChange={e => changeFilter('kind', e.target.value)}><option value="">全部类型</option><option value="original">普通录像</option><option value="archive">60× 归档</option></select></label>
        </div><small className="muted">普通录像按起始日期归属；归档按归档日期归属。所有条件同时生效。</small></section>}
        <div className="metrics"><Metric title="当前保留大小" value={stats ? bytes(stats.bytes) : '—'} note={`${stats?.count ?? 0} 个录像文件`} icon={<HardDrive size={20}/>} />
          <Metric title="普通录像" value={stats ? bytes(ordinary) : '—'} note="包含缓存中的录像" icon={<Video size={20}/>} />
          <Metric title="60× 归档" value={stats ? bytes(archived) : '—'} note="永久保留 · 不重复计算原件" icon={<Archive size={20}/>} />
          <Metric title="本地缓存" value={status ? bytes(status.cache_bytes) : '—'} note={`${status?.pending_files ?? 0} 个文件等待落盘`} icon={<Activity size={20}/>} />
        </div>
      </>}

      {view === 'overview' && <><div className="overview-grid"><section className="panel chart-panel"><div className="panel-heading"><div><h2>按日期查看存储</h2><p className="muted">最近 30 个有录像的日期</p></div><div className="legend"><span><i className="original-color"/>普通录像</span><span><i className="archive-color"/>60× 归档</span></div></div>
        {dayValues.length ? <div className="chart">{dayValues.map(item => <button key={item.day} className="chart-column" title={`${item.day}\n普通录像 ${bytes(item.original)}\n归档 ${bytes(item.archive)}`} onClick={() => { setFilter({ ...emptyFilters, day: item.day }); setView('files'); setOffset(0) }} aria-label={`查看 ${item.day} 的录像`}><div className="chart-track"><div className="bar archive-color" style={{ height: `${item.archive / chartMax * 100}%` }}/><div className="bar original-color" style={{ height: `${item.original / chartMax * 100}%` }}/></div><small>{item.day.slice(5)}</small></button>)}</div> : <Empty icon={<BarChart3 size={32}/>} title="还没有录像数据" text="摄像机写入第一段录像后，这里会显示空间统计。"/>}
        <div className="panel-foot">点击日期查看文件明细<button className="text-button" onClick={() => { setFilter(emptyFilters); setView('files') }}>全部录像 <ChevronRight size={14}/></button></div></section>
        <section className="panel resource-panel"><div className="panel-heading"><h2>缓存与后端</h2><Server size={19}/></div><div className="storage-state"><span className={`status-icon ${status?.storage_configured ? '' : 'warn'}`}><HardDrive size={23}/></span><div><strong>{status?.storage_configured ? '真实存储已配置' : '真实存储未配置'}</strong><p>{storage.protocol === 'nfs' ? 'NFS' : 'SMB2 / SMB3'} · 整文件转存</p></div></div><p className="address">{storage.address || '先设置一个真实存储地址'}</p><button className="text-button" onClick={() => setView('storage')}>管理存储 <ChevronRight size={15}/></button><hr/><div className="resource-line"><span>本地缓存使用</span><strong>{status ? bytes(status.cache_bytes) : '—'}</strong></div><div className="progress"><div style={{ width: `${cacheTotal ? (status?.cache_bytes ?? 0) / cacheTotal * 100 : 0}%` }}/></div><div className="resource-line muted"><span>剩余可用</span><span>{status ? bytes(status.cache_available_bytes) : '—'}</span></div><div className="resource-bottom"><span className="dot"/>{status?.writing_handles ?? 0} 个写入句柄 · {status?.pending_files ?? 0} 个待落盘文件</div></section></div>
        <section className="panel"><div className="panel-heading"><h2>归档任务</h2><span className="muted">每日 02:00 入队 · 单任务处理</span></div>{status?.jobs.length ? <div className="table-scroll"><table><thead><tr><th>摄像机 / 编号</th><th>录像日期</th><th>状态</th><th>详情</th><th/></tr></thead><tbody>{status.jobs.map(job => <tr key={job.id}><td>{devices.find(d => d.id === job.device_id)?.name ?? job.device_id}<small className="cell-note">编号 {job.channel}</small></td><td>{job.day}</td><td><span className={`badge ${job.status === 'failed' ? 'error' : job.status === 'done' ? 'green' : ''}`}>{stateLabels[job.status]}</span></td><td className="error-cell">{job.error || '—'}</td><td>{job.status === 'failed' && <button className="text-button" onClick={async () => { try { await api(`/jobs/${job.id}/retry`, 'POST'); await reload(); notify('任务已重新入队') } catch (e) { failure(e) } }}>重试</button>}</td></tr>)}</tbody></table></div> : <div className="simple-empty">过期录像的归档任务会显示在这里。</div>}</section>
      </>}

      {view === 'files' && <section className="panel"><div className="panel-heading"><h2>文件明细 <span className="count">{files?.count ?? 0}</span></h2><span className="muted">{fileLoading ? '正在查询…' : '每段录像只计入一次'}</span></div>
        {files?.items.length ? <><div className="table-scroll"><table className="file-table"><thead><tr><th>录像文件</th><th>摄像机 / 编号</th><th>类型</th><th>大小</th><th>存储状态</th><th/></tr></thead><tbody>{files.items.map(file => <tr key={file.id}><td><div className="filename"><span className={`file-icon ${file.kind === 'archive' ? 'purple' : ''}`}>{file.kind === 'archive' ? <Archive size={19}/> : <Video size={19}/>}</span><div><strong title={file.path}>{file.path.split('/').at(-1)}</strong><small>{datetime(file.start)}{file.kind === 'original' ? ` → ${datetime(file.end)}` : ' · 按日归档'}</small>{file.error && <small className="inline-error">{file.error}</small>}</div></div></td><td>{file.device_name}<small className="cell-note">编号 {file.channel}</small></td><td><span className={`badge ${file.kind === 'archive' ? 'purple' : ''}`}>{file.kind === 'archive' ? '60× 归档' : '普通录像'}</span></td><td className="numeric">{bytes(file.size)}</td><td><span className={`badge ${file.state === 'stored' ? 'green' : ''}`}>{stateLabels[file.state] ?? file.state}</span></td><td><a className="icon-button" href={`/api/files/${file.id}/download`} aria-label={`下载 ${file.path}`}><Download size={18}/></a></td></tr>)}</tbody></table></div><div className="pagination"><span>第 {offset + 1}–{Math.min(offset + 50, files.count)} 个，共 {files.count} 个</span><div><button className="icon-button" disabled={offset === 0} onClick={() => setOffset(o => Math.max(0, o - 50))} aria-label="上一页"><ChevronLeft size={18}/></button><button className="icon-button" disabled={offset + 50 >= files.count} onClick={() => setOffset(o => o + 50)} aria-label="下一页"><ChevronRight size={18}/></button></div></div></> : <Empty icon={<Video size={32}/>} title={fileLoading ? '正在加载文件…' : '没有符合条件的录像'} text="调整筛选条件，或等待摄像机写入录像。"/>}
      </section>}

      {view === 'devices' && <>{devices.length ? <div className="device-grid">{devices.map(device => <section className="panel device-card" key={device.id}><div className="device-card-top"><span className="camera-tile"><Camera size={24}/></span><span className={`badge ${device.enabled ? 'green' : ''}`}>{device.enabled ? '已启用' : '已停用'}</span></div><h2>{device.name}</h2><p className="muted">独立共享 · 独立访问账户</p><div className="share-path"><code>{`\\\\${location.hostname}\\${device.share}`}</code><button className="icon-button" aria-label="复制共享路径" onClick={() => copy(`\\\\${location.hostname}\\${device.share}`).then(() => notify('共享路径已复制')).catch(failure)}><Copy size={15}/></button></div><div className="device-details"><span>原录像保留</span><strong>{device.retention_days} 天</strong><span>归档录像</span><strong>60× · 永久保存</strong><span>账户</span><code>{device.username}</code></div><div className="device-actions"><button className="secondary" onClick={() => setDeviceDialog(device)}>编辑设置</button><button className="text-button" onClick={async () => { try { await api(`/devices/${device.id}`, 'PATCH', { enabled: !device.enabled }); await reload(); notify(device.enabled ? '设备已停用，现有录像继续保留' : '设备已启用') } catch (e) { failure(e) } }}>{device.enabled ? '停用' : '启用'}</button></div></section>)}</div> : <section className="panel"><Empty icon={<Camera size={38}/>} title="添加你的第一台摄像机" text="系统会为它生成共享路径、独立账号和密码。"/><div className="empty-action"><button className="primary" onClick={() => setDeviceDialog('new')}><Plus size={16}/>添加摄像机</button></div></section>}
        <div className="hint"><ShieldCheck size={19}/><span>摄像机可以读取旧录像；修改和删除由缓存状态与滚动策略统一管理。</span></div></>}

      {view === 'storage' && <section className="panel settings-panel"><div className="panel-heading"><div><h2>全局真实存储</h2><p className="muted">所有摄像机使用这个位置，并按设备分目录保存。</p></div><HardDrive size={22}/></div><form onSubmit={async event => { event.preventDefault(); setBusy(true); try { const saved = await api<Storage>('/storage', 'PUT', { ...storage, password: storage.password || null }); setStorage(saved); await reload(); notify('存储设置已保存') } catch (e) { failure(e) } finally { setBusy(false) } }}>
        <div className="protocol-switch"><button type="button" className={storage.protocol === 'nfs' ? 'selected' : ''} onClick={() => setStorage(s => ({ ...s, protocol: 'nfs', address: s.protocol === 'nfs' ? s.address : '' }))}><Server size={18}/>NFS<span>NAS 导出目录</span></button><button type="button" className={storage.protocol === 'smb' ? 'selected' : ''} onClick={() => setStorage(s => ({ ...s, protocol: 'smb', address: s.protocol === 'smb' ? s.address : '' }))}><HardDrive size={18}/>SMB2 / SMB3<span>远程共享存储</span></button></div>
        <label>存储地址<input required value={storage.address} placeholder={storage.protocol === 'nfs' ? 'nfs://192.168.1.10/volume1/recordings' : 'smb://192.168.1.10/recordings'} onChange={e => setStorage(s => ({ ...s, address: e.target.value }))}/></label>
        {storage.protocol === 'nfs' ? <><div className="form-grid"><label>NFS 版本<select value={storage.nfs_version} onChange={e => setStorage(s => ({ ...s, nfs_version: Number(e.target.value) as 3 | 4 }))}><option value="3">NFSv3</option><option value="4">NFSv4.0</option></select></label><label>UID<input type="number" min="0" required value={storage.uid} onChange={e => setStorage(s => ({ ...s, uid: Number(e.target.value) }))}/></label><label>GID<input type="number" min="0" required value={storage.gid} onChange={e => setStorage(s => ({ ...s, gid: Number(e.target.value) }))}/></label></div><p className="field-note">填写完整导出目录，并在 NAS 上允许此 UID/GID 读写。</p></> : <><div className="form-grid"><label>用户名<input value={storage.username} onChange={e => setStorage(s => ({ ...s, username: e.target.value }))}/></label><label>密码<input type="password" autoComplete="new-password" value={storage.password ?? ''} placeholder={storage.has_password ? '已保存，留空继续使用' : '共享账户密码'} onChange={e => setStorage(s => ({ ...s, password: e.target.value }))}/></label><label>域（可选）<input value={storage.domain} onChange={e => setStorage(s => ({ ...s, domain: e.target.value }))}/></label></div><label className="checkbox"><input type="checkbox" checked={storage.encrypt} onChange={e => setStorage(s => ({ ...s, encrypt: e.target.checked }))}/>要求 SMB3 加密</label></>}
        <div className="notice"><ShieldCheck size={19}/><p>上传与内容校验完成后才释放缓存。已有录像时，变更存储地址需要先完成手工迁移。</p></div><div className="form-actions"><button className="secondary" type="button" disabled={busy || !storage.address} onClick={async () => { setBusy(true); try { const result = await api<{ message: string }>('/storage/test', 'POST', { ...storage, password: storage.password || null }); notify(result.message) } catch (e) { failure(e) } finally { setBusy(false) } }}><Activity size={16}/>测试读写连接</button><button className="primary" disabled={busy}><Check size={16}/>{busy ? '处理中…' : '保存配置'}</button></div>
      </form></section>}

      {view === 'storage' && status && <section className="panel settings-panel" style={{ marginTop: 24 }}><div className="panel-heading"><div><h2>元数据数据库</h2><p className="muted">保存设备、配置、录像索引和归档任务。</p></div><Database size={22}/></div>
        <div className="device-details"><span>PostgreSQL 状态</span><strong>{!status.metadata.configured ? '尚未配置 · 仅内存运行' : status.metadata.connected ? '已连接' : '使用内存副本'}</strong><span>等待同步</span><strong>{status.metadata.pending_rows} 条</strong><span>最近成功同步</span><strong>{status.metadata.last_sync ? datetime(status.metadata.last_sync) : '—'}</strong><span>Schema</span><code>{status.metadata.schema}</code></div>
        <p className="field-note">断线期间继续使用内存配置，恢复后自动同步。重启从 PostgreSQL 加载已保存的数据，未同步的变更和临时录像会丢失。</p>
      </section>}

      <footer className="page-footer"><span>CamMon · 录像存储网关</span><span>统计当前保留数据 · Asia/Shanghai</span></footer>
    </div></main>
    {toast && <div className={`toast ${toast.error ? 'error' : ''}`} role={toast.error ? 'alert' : 'status'}>{toast.error ? <AlertCircle size={19}/> : <Check size={19}/>}<span>{toast.text}</span><button className="icon-button" onClick={() => setToast(null)} aria-label="关闭提示"><X size={16}/></button></div>}
    {deviceDialog && <Dialog title={deviceDialog === 'new' ? '添加摄像机' : '编辑摄像机'} close={() => setDeviceDialog(null)}><form onSubmit={async event => { event.preventDefault(); const data = new FormData(event.currentTarget); setBusy(true); try { const body = { name: data.get('name'), retention_days: Number(data.get('retention_days')) }; if (deviceDialog === 'new') { const created = await api<Device>('/devices', 'POST', body); setCredentials(created) } else { await api(`/devices/${deviceDialog.id}`, 'PATCH', body); notify('摄像机设置已更新') }; setDeviceDialog(null); await reload() } catch (e) { failure(e) } finally { setBusy(false) } }}><label>设备名称<input name="name" required maxLength={100} autoFocus defaultValue={deviceDialog === 'new' ? '' : deviceDialog.name} placeholder="例如：客厅摄像机"/></label><label>原录像保留天数<input name="retention_days" type="number" min="1" max="36500" required defaultValue={deviceDialog === 'new' ? 30 : deviceDialog.retention_days}/></label><p className="field-note">按北京时间自然日计算，包含当天。过期录像生成每个相机编号独立的 60 倍速归档。</p><div className="form-actions">{deviceDialog !== 'new' && <button type="button" className="text-button" disabled={busy} onClick={async () => { setBusy(true); try { const result = await api<{ password: string }>(`/devices/${deviceDialog.id}/reset-password`, 'POST'); setCredentials({ ...deviceDialog, password: result.password }); setDeviceDialog(null) } catch (e) { failure(e) } finally { setBusy(false) } }}>重置接入密码</button>}<button className="primary" disabled={busy}>{busy ? '正在保存…' : '保存'}</button></div></form></Dialog>}
    {credentials && <Dialog title="摄像机接入信息" close={() => setCredentials(null)}><p className="muted">请保存密码，并将下列信息填写到摄像机的 NAS / 网络存储设置中。</p><div className="credential-list">{[
      ['共享路径', `\\\\${location.hostname}\\${credentials.share}`], ['用户名', credentials.username], ['密码', credentials.password ?? ''],
    ].map(([name, value]) => <div key={name}><small>{name}</small><code>{value}</code><button className="icon-button" aria-label={`复制${name}`} onClick={() => copy(value).then(() => notify(`${name}已复制`)).catch(failure)}><Copy size={16}/></button></div>)}</div><p className="field-note">密码仅在创建或重置时显示。共享根目录下最多两层子文件夹，录像文件使用约定的 MP4 名称。</p><div className="form-actions"><button className="primary" onClick={() => setCredentials(null)}>已保存接入信息</button></div></Dialog>}
  </div>
}

function Metric({ title, value, note, icon }: { title: string; value: string; note: string; icon: ReactNode }) { return <section className="metric"><div><span>{title}</span><span className="metric-icon">{icon}</span></div><strong>{value}</strong><p>{note}</p></section> }
function Empty({ icon, title, text }: { icon: ReactNode; title: string; text: string }) { return <div className="empty"><span>{icon}</span><h3>{title}</h3><p>{text}</p></div> }
