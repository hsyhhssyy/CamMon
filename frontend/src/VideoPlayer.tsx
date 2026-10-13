import { useEffect, useRef, useState } from 'react'
import { Download, RefreshCw } from 'lucide-react'
import { bytes, datetime, type Recording } from './api'

export default function VideoPlayer({ file }: { file: Recording }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [rate, setRate] = useState('1')
  const source = `/api/files/${file.id}/stream`

  useEffect(() => {
    const video = videoRef.current
    return () => {
      if (!video) return
      video.pause()
      video.removeAttribute('src')
      video.load()
    }
  }, [])

  async function playbackError() {
    setLoading(false)
    let message = '无法播放此录像。视频可能尚未写完、已损坏，或使用了当前浏览器不支持的编码；可下载后使用本地播放器查看。'
    try {
      const response = await fetch(source, { method: 'HEAD', credentials: 'same-origin' })
      if (response.status === 401) message = '登录已过期，请重新登录后播放。'
      else if (response.status === 410) message = '原录像已归档，请在录像列表选择对应的 60× 归档播放。'
      else if (!response.ok) message = '录像暂时无法读取，请刷新列表并检查存储连接后重试。'
    } catch {
      message = '无法连接录像服务，请检查网络连接后重试。'
    }
    setError(message)
  }

  function retry() {
    setError(null)
    setLoading(true)
    videoRef.current?.load()
  }

  return <>
    <div className="player-details"><strong>{file.path.split('/').at(-1)}</strong><p>{file.device_name} · 编号 {file.channel} · {bytes(file.size)}</p><p>{file.kind === 'archive' ? `${file.day} · 60× 归档` : `${datetime(file.start)} → ${datetime(file.end)}`}</p></div>
    <video ref={videoRef} className="recording-player" src={source} controls playsInline autoPlay preload="metadata" aria-label="录像播放器"
      onLoadedMetadata={() => { setLoading(false); if (videoRef.current) videoRef.current.playbackRate = Number(rate) }}
      onError={() => void playbackError()}>浏览器不支持视频播放，请下载录像查看。</video>
    {loading && <p className="player-loading" role="status">正在加载录像…</p>}
    {error && <div className="player-error" role="alert"><p>{error}</p><button className="text-button" onClick={retry}><RefreshCw size={14}/>重新加载</button></div>}
    <div className="player-actions"><label>播放速度<select value={rate} onChange={event => { setRate(event.target.value); if (videoRef.current) videoRef.current.playbackRate = Number(event.target.value) }}>{['0.5', '1', '1.5', '2', '4'].map(value => <option key={value} value={value}>{value}×</option>)}</select></label><a className="secondary" href={`/api/files/${file.id}/download`}><Download size={16}/>下载录像</a></div>
    {file.kind === 'archive' && <p className="field-note">归档已按原录像时间压缩 60 倍；选择 1× 播放速度时，即为原录像的 60 倍速。</p>}
  </>
}
