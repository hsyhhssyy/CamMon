import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Search } from 'lucide-react'
import { api, ownershipText, type PathOwnership } from './api'

export default function OwnershipPanel({ initialOwnership, failure }: {
  initialOwnership: PathOwnership; failure: (error: unknown) => void
}) {
  const [path, setPath] = useState(initialOwnership.path)
  const [result, setResult] = useState<PathOwnership | null>(initialOwnership)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])

  async function query(event: FormEvent) {
    event.preventDefault(); setBusy(true); setResult(null); setError('')
    const controller = new AbortController()
    request.current = controller
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, 20000)
    try { setResult(await api<PathOwnership>('/storage/ownership?' + new URLSearchParams({ path }), 'GET', undefined, controller.signal)) }
    catch (error) {
      if (controller.signal.aborted && !timedOut) return
      setError(timedOut ? '查询归属超时，请重试。' : error instanceof Error ? error.message : '查询失败，请重试。')
      if ((error as Error & { status?: number }).status === 401) failure(error)
    } finally { clearTimeout(timer); if (!controller.signal.aborted || timedOut) setBusy(false) }
  }

  return <section className="folder-query" aria-label="文件归属查询">
    <h3>文件归属查询</h3>
    <p className="muted">可输入视频、文件或文件夹的完整路径，也可查询已归档清理的路径。</p>
    <form onSubmit={query}>
      <label>查询路径<div className="import-path"><input required disabled={busy} value={path} placeholder="/data/旧录像/视频.mp4" onChange={event => { setPath(event.target.value); setResult(null); setError('') }}/><button className="secondary" disabled={busy}><Search size={16}/>{busy ? '正在查询…' : '查询归属'}</button></div></label>
      {error && <p className="inline-error" role="alert">{error}</p>}
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
