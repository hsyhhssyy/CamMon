import { useState, type FormEvent } from 'react'
import { Folder, FolderOpen, Import, Search } from 'lucide-react'
import { api, bytes, type Device, type ImportFolders, type ImportReport } from './api'

const labels = { ready: '可导入', imported: '已导入', duplicate: '已存在', skipped: '已跳过', failed: '导入失败' }

export default function ImportPanel({ devices, onImported, failure }: {
  devices: Device[]; onImported: (report: ImportReport) => Promise<void>; failure: (error: unknown) => void
}) {
  const [folder, setFolder] = useState('')
  const [deviceId, setDeviceId] = useState('')
  const [recursive, setRecursive] = useState(true)
  const [browser, setBrowser] = useState<ImportFolders | null>(null)
  const [report, setReport] = useState<ImportReport | null>(null)
  const [completed, setCompleted] = useState(false)
  const [busy, setBusy] = useState(false)

  function invalidate() { setReport(null); setCompleted(false) }
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
    setBusy(true)
    try {
      const result = await api<ImportReport>('/import', 'POST', { folder, device_id: deviceId, recursive })
      setReport(result); setCompleted(true); await onImported(result)
    } catch (error) { invalidate(); failure(error) } finally { setBusy(false) }
  }

  return <>
    <section className="panel settings-panel"><div className="panel-heading"><div><h2>导入已有录像</h2><p className="muted">把本地磁盘文件夹中的视频归入指定摄像机。</p></div><Import size={22}/></div>
      <form onSubmit={preview}>
        <p className="field-note">先将旧录像放到已配置的本地存储目录（默认 /data）下，再选择容器内的文件夹。可以包含多层子文件夹。</p>
        <label>导入文件夹<div className="import-path"><input required disabled={busy} value={folder} placeholder="/data/旧录像" onChange={event => { setFolder(event.target.value); invalidate() }}/><button type="button" className="secondary" disabled={busy} onClick={() => browse()}><FolderOpen size={16}/>浏览文件夹</button></div></label>
        {browser && <div className="folder-browser" aria-label="服务器文件夹">
          <code>{browser.path}</code>
          <div className="folder-actions"><button type="button" className="secondary" disabled={busy || !browser.parent} onClick={() => browse(browser.parent!)}>上一级</button><button type="button" className="primary" disabled={busy} onClick={() => { setFolder(browser.path); setBrowser(null); invalidate() }}>选择此文件夹</button></div>
          <div className="folder-list">{browser.folders.map(child => <button type="button" key={child.path} disabled={busy} onClick={() => browse(child.path)}><Folder size={16}/>{child.name}</button>)}{!browser.folders.length && <p className="muted">没有可浏览的子文件夹</p>}</div>
        </div>}
        <label>归属摄像机<select required disabled={busy} value={deviceId} onChange={event => { setDeviceId(event.target.value); invalidate() }}><option value="">请选择摄像机</option>{devices.map(device => <option key={device.id} value={device.id}>{device.name} · 保留 {device.retention_days} 天{device.enabled ? '' : ' · 已停用'}</option>)}</select></label>
        {!devices.length && <p className="field-note">请先在摄像机管理中添加设备。</p>}
        <label className="checkbox"><input type="checkbox" disabled={busy} checked={recursive} onChange={event => { setRecursive(event.target.checked); invalidate() }}/>包含子文件夹</label>
        <p className="field-note">文件名应为 00_20251008080239_20251008082813.mp4。编号和起止时间按文件名识别，时间按北京时间解释；不符合命名的文件会跳过。</p>
        <p className="field-note">目标摄像机已有同名录像时，会核对文件大小和内容。相同副本只归档一次，两处原文件都在成功归档后清理；同名但内容不同的文件会保留并显示原因。</p>
        <div className="notice"><Import size={19}/><p>导入会直接管理原位置的文件，无需复制。保留天数从录像日期计算；超期片段在下一次每日归档任务中生成 60 倍速视频，成功归档后删除原文件。导入后请勿移动、改名或修改这些文件。</p></div>
        <div className="form-actions"><button className="secondary" disabled={busy || !devices.length}><Search size={16}/>{busy ? '正在处理…' : '预览导入'}</button></div>
      </form>
    </section>
    {report && <section className="panel import-results"><div className="panel-heading"><div><h2>{completed ? '导入结果' : '导入预览'}</h2><p className="muted">共 {report.total} 个文件 · {completed ? `已导入 ${report.imported}` : `可导入 ${report.importable}`} 个 · {bytes(report.bytes)} · 超期 {report.expired} 个</p><p className="muted">{completed ? '已关联副本' : '可关联副本'} {report.copies} 个 · 归档成功后清理</p></div>{!completed && <button className="primary" disabled={busy || (!report.importable && !report.copies)} onClick={importFiles}><Import size={16}/>{busy ? '正在导入…' : '开始导入'}</button>}</div>
      <p className="import-summary">已存在 {report.duplicates} 个 · 跳过 {report.skipped} 个 · 失败 {report.failed} 个。{completed && '已导入的录像可在录像文件页面查看和下载。'}{report.total > report.items.length && `列表仅显示前 ${report.items.length} 个文件。`}</p>
      <div className="table-scroll"><table><thead><tr><th>源文件</th><th>录像日期 / 编号</th><th>大小</th><th>处理结果</th></tr></thead><tbody>{report.items.map(item => <tr key={item.path}><td className="import-source"><code>{item.path}</code></td><td>{item.day ?? '—'}<small className="cell-note">{item.channel && `X = ${item.channel}`}{item.expired && ' · 超期'}</small></td><td className="numeric">{item.size === undefined ? '—' : bytes(item.size)}</td><td><span className={`badge ${item.status === 'failed' ? 'error' : item.status === 'ready' || item.status === 'imported' ? 'green' : ''}`}>{labels[item.status]}</span>{item.reason && <small className="cell-note">{item.reason}</small>}</td></tr>)}</tbody></table></div>
      {!report.total && <div className="simple-empty">该文件夹下没有文件</div>}
    </section>}
  </>
}
