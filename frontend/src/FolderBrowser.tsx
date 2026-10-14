import { useEffect, useRef, useState } from 'react'
import { Folder, RefreshCw, X } from 'lucide-react'
import { api, ownershipText, type ImportFolders } from './api'
import OwnershipPanel from './OwnershipPanel'

export default function FolderBrowser({ initialPath, select, close, failure }: {
  initialPath?: string; select: (path: string) => void; close: () => void; failure: (error: unknown) => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [path, setPath] = useState(initialPath)
  const [attempt, setAttempt] = useState(0)
  const [listing, setListing] = useState<ImportFolders | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const failureRef = useRef(failure)
  failureRef.current = failure

  useEffect(() => { dialog.current?.showModal() }, [])
  useEffect(() => {
    let active = true
    let timedOut = false
    const controller = new AbortController()
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, 20000)
    async function load() {
      try {
        const result = await api<ImportFolders>('/import/folders' + (path ? '?' + new URLSearchParams({ path }) : ''), 'GET', undefined, controller.signal)
        if (active) setListing(result)
      } catch (error) {
        if (!active) return
        setError(timedOut ? '加载文件夹超时，请重试。' : error instanceof Error ? error.message : '加载文件夹失败，请重试。')
        if ((error as Error & { status?: number }).status === 401) failureRef.current(error)
      } finally {
        clearTimeout(timer)
        if (active) setLoading(false)
      }
    }
    void load()
    return () => { active = false; clearTimeout(timer); controller.abort() }
  }, [path, attempt])

  function navigate(next?: string) {
    setLoading(true); setError(''); setListing(null); setPath(next); setAttempt(value => value + 1)
  }

  return <dialog ref={dialog} className="folder-dialog" aria-label="浏览文件夹" onCancel={close} onClose={close}>
    <div className="dialog-title"><h2>浏览文件夹</h2><button type="button" className="icon-button" aria-label="关闭" onClick={close}><X size={20}/></button></div>
    <div className="folder-browser" role="region" aria-label="服务器文件夹" aria-busy={loading}>
      <code>{listing?.path ?? path ?? '本地存储根目录'}</code>
      {loading && <p className="folder-loading" role="status">正在加载文件夹和摄像机归属…</p>}
      {error && <div className="folder-error"><p className="inline-error" role="alert">{error}</p><button type="button" className="secondary" onClick={() => navigate(path)}><RefreshCw size={16}/>重试</button></div>}
      {listing && <>
        <p className={`folder-ownership ${listing.ownership.status === 'conflict' ? 'inline-error' : ''}`}>{ownershipText(listing.ownership)}</p>
        <div className="folder-list">{listing.folders.map(child => <button type="button" key={child.path} onClick={() => navigate(child.path)}><Folder size={16}/><span>{child.name}<small className={`folder-ownership ${child.ownership.status === 'conflict' ? 'inline-error' : ''}`}>{ownershipText(child.ownership)}</small></span></button>)}{!listing.folders.length && <p className="muted">没有可浏览的子文件夹</p>}</div>
        {listing.path === listing.root && <p className="field-note">存储根目录是公共父目录，请进入一个独立的子文件夹后选择。</p>}
      </>}
    </div>
    {listing && <OwnershipPanel key={listing.path} initialOwnership={listing.ownership} failure={failure}/>}
    <div className="folder-actions">
      <button type="button" className="secondary" disabled={loading} onClick={() => navigate()}>存储根目录</button>
      <button type="button" className="secondary" disabled={loading || !listing?.parent} onClick={() => navigate(listing!.parent!)}>上一级</button>
      <button type="button" className="primary" disabled={loading || !listing || listing.path === listing.root} onClick={() => select(listing!.path)}>选择此文件夹</button>
    </div>
  </dialog>
}
