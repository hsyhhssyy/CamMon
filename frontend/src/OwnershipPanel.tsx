import { useState, type FormEvent } from 'react'
import { Search } from 'lucide-react'
import { api, ownershipText, type PathOwnership } from './api'

export default function OwnershipPanel({ failure }: { failure: (error: unknown) => void }) {
  const [path, setPath] = useState('')
  const [result, setResult] = useState<PathOwnership | null>(null)
  const [busy, setBusy] = useState(false)

  async function query(event: FormEvent) {
    event.preventDefault(); setBusy(true); setResult(null)
    try { setResult(await api<PathOwnership>('/storage/ownership?' + new URLSearchParams({ path }))) }
    catch (error) { failure(error) } finally { setBusy(false) }
  }

  return <section className="panel settings-panel" aria-label="文件归属查询">
    <div className="panel-heading"><div><h2>文件归属查询</h2><p className="muted">查看本地存储中的视频、文件或文件夹归属哪台摄像机。</p></div><Search size={22}/></div>
    <form onSubmit={query}>
      <label>查询路径<div className="import-path"><input required disabled={busy} value={path} placeholder="/data/旧录像/视频.mp4" onChange={event => { setPath(event.target.value); setResult(null) }}/><button className="secondary" disabled={busy}><Search size={16}/>{busy ? '正在查询…' : '查询归属'}</button></div></label>
      {result && <div className="ownership-result" role="status">
        <code>{result.path}</code>
        <p className={result.status === 'conflict' ? 'inline-error' : ''}>{ownershipText(result)}</p>
        {!!result.recording_ids.length && <p className="muted">已登记录像或录像副本关联。</p>}
        {result.status === 'owned' && result.kind !== 'directory' && !result.recording_ids.length && <p className="muted">按所在目录归属，尚未登记为录像。</p>}
        {result.status === 'container' && <p className="muted">父目录本身未绑定摄像机。请选择各摄像机的独立子目录进行导入。</p>}
        {result.status === 'conflict' && <p className="muted">已有数据包含多个摄像机的归属，导入会阻止继续分配。请检查历史关联。</p>}
        {!result.exists && <p className="muted">{result.status === 'unassigned' ? '磁盘上不存在此路径，也没有登记归属。' : '磁盘上已无此路径，以上为保存的归属信息。'}</p>}
      </div>}
    </form>
  </section>
}
