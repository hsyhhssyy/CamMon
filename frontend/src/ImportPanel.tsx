import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ChevronLeft, ChevronRight, Folder, FolderOpen, Import, Search } from 'lucide-react'
import { api, bytes, datetime, duration, ownershipText, type Device, type ImportFolders, type ImportItem, type ImportJob, type ImportReport } from './api'
import OwnershipPanel from './OwnershipPanel'
import Thumbnail from './Thumbnail'

const labels = { ready: '可导入', imported: '已导入', duplicate: '已存在', skipped: '已跳过', failed: '导入失败' }

export default function ImportPanel({ devices, onImported, failure }: {
  devices: Device[]; onImported: (report: ImportReport) => Promise<void>; failure: (error: unknown) => void
}) {
  const [folder, setFolder] = useState('')
  const [deviceId, setDeviceId] = useState('')
  const [recursive, setRecursive] = useState(true)
  const [browser, setBrowser] = useState<ImportFolders | null>(null)
  const [report, setReport] = useState<ImportReport | null>(null)
  const [job, setJob] = useState<ImportJob | null>(null)
  const [jobsLoaded, setJobsLoaded] = useState(false)
  const [connectionError, setConnectionError] = useState('')
  const [busy, setBusy] = useState(false)
  const callbacks = useRef({ onImported, failure })
  callbacks.current = { onImported, failure }
  const observedJob = useRef<ImportJob | null>(null)
  const generation = useRef(0)
  const running = job?.status === 'running'
  const disabled = busy || running || !jobsLoaded

  function acceptJob(next: ImportJob | null, restore = false) {
    const previous = observedJob.current
    observedJob.current = next
    setJob(next)
    if (restore && next) {
      setFolder(next.folder); setDeviceId(next.device_id); setRecursive(next.recursive)
    }
    if (previous?.id === next?.id && previous?.status === 'running' && next?.status === 'completed' && next.report) {
      void callbacks.current.onImported(next.report).catch(callbacks.current.failure)
    }
  }

  useEffect(() => {
    let active = true
    let restoring = true
    let timer: ReturnType<typeof setTimeout>
    async function refresh() {
      const version = generation.current
      try {
        const jobs = await api<ImportJob[]>('/import/jobs')
        if (!active || version !== generation.current) return
        acceptJob(jobs[0] ?? null, restoring)
        setConnectionError('')
      } catch (error) {
        if (!active || version !== generation.current) return
        setConnectionError(error instanceof Error ? error.message : '请求失败')
        if ((error as Error & { status?: number }).status === 401) callbacks.current.failure(error)
      } finally {
        if (active) {
          restoring = false
          setJobsLoaded(true)
          timer = setTimeout(refresh, 1500)
        }
      }
    }
    void refresh()
    return () => { active = false; clearTimeout(timer) }
  }, [])

  function invalidate() { setReport(null) }
  async function browse(path?: string) {
    setBusy(true)
    try { setBrowser(await api<ImportFolders>('/import/folders' + (path ? '?' + new URLSearchParams({ path }) : ''))) }
    catch (error) { failure(error) } finally { setBusy(false) }
  }
  async function preview(event: FormEvent) {
    event.preventDefault(); setBusy(true); invalidate()
    try { setReport(await api<ImportReport>('/import/preview', 'POST', { folder, device_id: deviceId, recursive })) }
    catch (error) { failure(error) } finally { setBusy(false) }
  }
  async function importFiles() {
    setBusy(true); generation.current += 1
    try {
      const result = await api<ImportJob>('/import', 'POST', { folder, device_id: deviceId, recursive })
      generation.current += 1
      acceptJob(result); setReport(null); setConnectionError('')
    } catch (error) { failure(error) } finally { setBusy(false) }
  }

  return <>
    <OwnershipPanel failure={failure}/>
    {connectionError && <p className="inline-error import-connection" role="alert">进度暂时无法刷新，将自动重试：{connectionError}</p>}
    {job && <section className="panel import-results" aria-label="最近导入任务">
      <div className="panel-heading"><div><h2>{running ? '导入进度' : job.status === 'completed' ? '导入结果' : '导入中断'}</h2><p className="muted">{job.device_name} · {datetime(job.created_at)}</p><p className="muted import-job-path">{job.folder}{job.recursive ? ' · 包含子文件夹' : ''}</p></div><span className={`badge ${running || job.status === 'completed' ? 'green' : 'error'}`}>{running ? '正在导入' : job.status === 'completed' ? '已完成' : '已中断'}</span></div>
      {running && <div className="import-progress" aria-live="polite">
        <p>{job.total_files === null ? '正在扫描文件夹…' : `已处理 ${job.report?.total ?? 0} / ${job.total_files} 个文件`}</p>
        <progress aria-label="导入进度" value={job.total_files === null ? undefined : job.report?.total ?? 0} max={Math.max(job.total_files ?? 1, 1)}/>
        {job.current_path && <code>{job.current_path}</code>}
        <p className="muted">导入在后台继续运行，可以切换或关闭页面，回来后自动显示进度。</p>
      </div>}
      {job.error && <p className="inline-error import-summary" role="alert">{job.error}。已导入的文件保留，可以重新预览并导入剩余文件。</p>}
      {job.report && <>
        <p className="import-summary">{running ? `已处理 ${job.report.total}` : `共 ${job.report.total}`} 个文件 · 已导入 {job.report.imported} 个 · {bytes(job.report.bytes)} · 超期 {job.report.expired} 个</p>
        <p className="import-summary">已关联副本 {job.report.copies} 个 · 归档成功后清理</p>
        <ReportDetails report={job.report} completed={!running}/>
      </>}
    </section>}
    <section className="panel settings-panel"><div className="panel-heading"><div><h2>导入已有录像</h2><p className="muted">把本地磁盘文件夹中的视频归入指定摄像机。</p></div><Import size={22}/></div>
      <form onSubmit={preview}>
        <p className="field-note">先将旧录像放到已配置的本地存储目录（默认 /data）下，再选择容器内的文件夹。可以包含多层子文件夹。</p>
        <label>导入文件夹<div className="import-path"><input required disabled={disabled} value={folder} placeholder="/data/旧录像" onChange={event => { setFolder(event.target.value); invalidate() }}/><button type="button" className="secondary" disabled={disabled} onClick={() => browse()}><FolderOpen size={16}/>浏览文件夹</button></div></label>
        {browser && <div className="folder-browser" role="region" aria-label="服务器文件夹">
          <code>{browser.path}</code>
          <p className="folder-ownership">{ownershipText(browser.ownership)}</p>
          <div className="folder-actions"><button type="button" className="secondary" disabled={disabled || !browser.parent} onClick={() => browse(browser.parent!)}>上一级</button><button type="button" className="primary" disabled={disabled} onClick={() => { setFolder(browser.path); setBrowser(null); invalidate() }}>选择此文件夹</button></div>
          <div className="folder-list">{browser.folders.map(child => <button type="button" key={child.path} disabled={disabled} onClick={() => browse(child.path)}><Folder size={16}/><span>{child.name}<small className="folder-ownership">{ownershipText(child.ownership)}</small></span></button>)}{!browser.folders.length && <p className="muted">没有可浏览的子文件夹</p>}</div>
        </div>}
        <label>归属摄像机<select required disabled={disabled} value={deviceId} onChange={event => { setDeviceId(event.target.value); invalidate() }}><option value="">请选择摄像机</option>{devices.map(device => <option key={device.id} value={device.id}>{device.name} · 保留 {device.retention_days} 天{device.enabled ? '' : ' · 已停用'}</option>)}</select></label>
        {!devices.length && <p className="field-note">请先在摄像机管理中添加设备。</p>}
        <label className="checkbox"><input type="checkbox" disabled={disabled} checked={recursive} onChange={event => { setRecursive(event.target.checked); invalidate() }}/>包含子文件夹</label>
        <p className="field-note">每个导入文件夹及其子目录只能归属一台摄像机，取消递归扫描也不会解除归属。预览仅检查；开始导入时保存目录归属，归档清理视频后仍保留。已有其他摄像机归属的父子目录会阻止导入。</p>
        <p className="field-note">支持 00_20251008080239_20251008082813.mp4 和小米录像 30M33S_1753536633.mp4。前者按文件名识别编号和起止时间；小米录像编号为 00，开始时间取时间戳，结束时间按实际视频时长计算。时间按北京时间显示；不符合命名的文件会跳过。</p>
        <p className="field-note">目标摄像机已有同名录像时，会核对文件大小和内容。相同副本只归档一次，两处原文件都在成功归档后清理；同名但内容不同的文件会保留并显示原因。</p>
        <div className="notice"><Import size={19}/><p>导入会直接管理原位置的文件，无需复制。保留天数从录像日期计算；超期片段在下一次每日归档任务中生成 60 倍速视频，成功归档后删除原文件。导入后请勿移动、改名或修改这些文件。</p></div>
        <div className="form-actions"><button className="secondary" disabled={disabled || !devices.length}><Search size={16}/>{busy ? '正在处理…' : '预览导入'}</button></div>
      </form>
    </section>
    {report && <section className="panel import-results" aria-label="导入预览"><div className="panel-heading"><div><h2>导入预览</h2><p className="muted">共 {report.total} 个文件 · 可导入 {report.importable} 个 · {bytes(report.bytes)} · 超期 {report.expired} 个</p><p className="muted">可关联副本 {report.copies} 个 · 归档成功后清理</p></div><button className="primary" disabled={disabled || (!report.importable && !report.copies)} onClick={importFiles}><Import size={16}/>{busy ? '正在提交…' : '开始导入'}</button></div>
      <PreviewDetails key={report.preview_id ?? 'preview'} report={report} failure={failure}/>
      {!report.total && <div className="simple-empty">该文件夹下没有文件</div>}
    </section>}
  </>
}

type PreviewImage = { url?: string; error?: string }

function PreviewDetails({ report, failure }: { report: ImportReport; failure: (error: unknown) => void }) {
  const [page, setPage] = useState(report)
  const [loading, setLoading] = useState(false)
  const [images, setImages] = useState<Record<string, PreviewImage>>({})
  const cache = useRef<Record<string, PreviewImage>>({})
  const failureRef = useRef(failure)
  failureRef.current = failure
  useEffect(() => () => { Object.values(cache.current).forEach(image => { if (image.url) URL.revokeObjectURL(image.url) }) }, [])
  useEffect(() => {
    if (!page.preview_id || loading) return
    const controller = new AbortController()
    async function generate() {
      for (const [index, item] of page.items.entries()) {
        if (controller.signal.aborted) return
        if (!item.thumbnail_key || cache.current[item.thumbnail_key]) continue
        let image: PreviewImage
        try {
          const response = await fetch(`/api/import/previews/${page.preview_id}/thumbnail?index=${(page.offset ?? 0) + index}`, { credentials: 'same-origin', signal: controller.signal })
          if (!response.ok) {
            const detail = await response.json()
            if (response.status === 401) {
              failureRef.current(Object.assign(new Error(detail.detail), { status: 401 })); return
            }
            throw new Error(detail.detail || '无法生成首帧')
          }
          const blob = await response.blob()
          if (controller.signal.aborted) return
          image = { url: URL.createObjectURL(blob) }
        } catch (error) {
          if (controller.signal.aborted) return
          image = { error: error instanceof Error ? error.message : '无法生成首帧' }
        }
        cache.current[item.thumbnail_key] = image
        setImages({ ...cache.current })
      }
    }
    void generate()
    return () => controller.abort()
  }, [page, loading])

  async function changePage(offset: number) {
    setLoading(true)
    try { setPage(await api<ImportReport>(`/import/previews/${page.preview_id}?offset=${offset}&limit=25`)) }
    catch (error) { failure(error) } finally { setLoading(false) }
  }
  return <>
    <p className="import-summary">已存在 {report.duplicates} 个 · 跳过 {report.skipped} 个 · 失败 {report.failed} 个。首帧按当前页逐张生成，导入后继续使用。</p>
    <ImportItems items={page.items} images={images} preview/>
    {!!page.total && <PageControls offset={page.offset ?? 0} count={page.total} limit={page.limit ?? 25} loading={loading} change={offset => void changePage(offset)}/>}
  </>
}

function ReportDetails({ report, completed = false }: { report: ImportReport; completed?: boolean }) {
  const [offset, setOffset] = useState(0)
  return <>
    <p className="import-summary">已存在 {report.duplicates} 个 · 跳过 {report.skipped} 个 · 失败 {report.failed} 个。{completed && '已导入的录像可在录像文件页面查看和下载。'}{report.total > report.items.length && `列表仅显示前 ${report.items.length} 个文件。`}</p>
    <ImportItems items={report.items.slice(offset, offset + 25)}/>
    {report.items.length > 25 && <PageControls offset={offset} count={report.items.length} limit={25} change={setOffset}/>}
    {completed && !report.total && <div className="simple-empty">该文件夹下没有文件</div>}
  </>
}

function ImportItems({ items, images = {}, preview = false }: { items: ImportItem[]; images?: Record<string, PreviewImage>; preview?: boolean }) {
  return <div className="import-file-list">{items.map(item => {
    const image = item.thumbnail_key ? images[item.thumbnail_key] : undefined
    return <article className={`import-file-card ${preview ? 'with-preview' : ''}`} key={item.path} aria-label={item.path}>
      {preview && <Thumbnail src={image?.url} alt={`${item.path} 的首帧`} pending={!!item.thumbnail_key && !image} message={image?.error ? '首帧暂不可用' : '无视频首帧'}/>}
      <div className="import-file-content"><div className="recording-title"><code>{item.path}</code><div className="recording-badges"><span className={`badge ${item.status === 'failed' ? 'error' : item.status === 'ready' || item.status === 'imported' ? 'green' : ''}`}>{labels[item.status]}</span>{item.expired && <span className="badge">超期</span>}</div></div>
        <dl className="recording-metadata"><div><dt>录像日期</dt><dd>{item.day ?? '—'}</dd></div><div><dt>相机编号</dt><dd>{item.channel ?? '—'}</dd></div><div><dt>文件大小</dt><dd>{item.size === undefined ? '—' : bytes(item.size)}</dd></div>{item.start !== undefined && item.end !== undefined && <><div className="recording-time"><dt>起止时间</dt><dd>{datetime(item.start)} → {datetime(item.end)}</dd></div><div><dt>录像时长</dt><dd>{duration(item.duration ?? item.end - item.start)}</dd></div></>}</dl>
        {item.reason && <p className={`import-file-note ${item.status === 'failed' ? 'inline-error' : 'muted'}`}>{item.reason}</p>}
        {image?.error && <p className="import-file-note muted" title={image.error}>无法生成首帧，仍可按处理结果导入。</p>}
      </div>
    </article>
  })}</div>
}

function PageControls({ offset, count, limit, loading = false, change }: { offset: number; count: number; limit: number; loading?: boolean; change: (offset: number) => void }) {
  return <div className="pagination"><span>{loading ? '正在加载…' : `第 ${offset + 1}–${Math.min(offset + limit, count)} 个，共 ${count} 个 · 每页 ${limit} 个`}</span><div><button type="button" className="icon-button" disabled={loading || offset === 0} onClick={() => change(Math.max(0, offset - limit))} aria-label="导入上一页"><ChevronLeft size={18}/></button><button type="button" className="icon-button" disabled={loading || offset + limit >= count} onClick={() => change(offset + limit)} aria-label="导入下一页"><ChevronRight size={18}/></button></div></div>
}
