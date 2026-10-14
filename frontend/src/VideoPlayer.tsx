import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Download, RefreshCw } from 'lucide-react'
import { bytes, datetime, type Recording } from './api'
import { mp4VideoCodec } from './mp4-stream'

const SoftwareVideoPlayer = lazy(() => import('./SoftwareVideoPlayer'))

export default function VideoPlayer({ file }: { file: Recording }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [rate, setRate] = useState('1')
  const [mode, setMode] = useState<'native' | 'software'>('native')
  const [attempt, setAttempt] = useState(0)
  const [initialTime, setInitialTime] = useState(0)
  const [initialVolume, setInitialVolume] = useState(1)
  const [nativeSource, setNativeSource] = useState<string>()
  const switching = useRef(false)
  const generation = useRef(0)
  const requestRef = useRef<AbortController | null>(null)
  const source = `/api/files/${file.id}/stream`

  useEffect(() => {
    const video = videoRef.current
    const controller = new AbortController()
    // A small header probe avoids letting an audio-only native player scan
    // a complete fragmented HEVC recording before discovering no video codec.
    async function choosePlayer() {
      let codec: string | null = null
      try {
        const response = await fetch(source, { headers: { Range: 'bytes=0-65535' }, credentials: 'same-origin', signal: controller.signal })
        if (response.status === 206 && Number(response.headers.get('Content-Length')) <= 65536) codec = mp4VideoCodec(new Uint8Array(await response.arrayBuffer()))
        else await response.body?.cancel()
      } catch { /* Let the native error path report network/storage errors. */ }
      if (controller.signal.aborted) return
      if (codec && video && !video.canPlayType(`video/mp4; codecs="${codec}"`)) setMode('software')
      else setNativeSource(source)
    }
    void choosePlayer()
    return () => {
      generation.current++
      controller.abort()
      requestRef.current?.abort()
      if (!video) return
      video.pause()
      video.removeAttribute('src')
      video.load()
    }
  }, [source])

  async function playbackError(fallback = true) {
    if (switching.current || error) return
    switching.current = true
    const current = generation.current
    requestRef.current?.abort()
    const controller = new AbortController()
    requestRef.current = controller
    let message = '无法播放此录像。视频可能尚未写完、已损坏，或使用了当前浏览器不支持的编码；可下载后使用本地播放器查看。'
    try {
      const response = await fetch(source, { method: 'HEAD', credentials: 'same-origin', signal: controller.signal })
      if (current !== generation.current) return
      if (response.status === 401) message = '登录已过期，请重新登录后播放。'
      else if (response.status === 410) message = '原录像已归档，请在录像列表选择对应的 60× 归档播放。'
      else if (!response.ok) message = '录像暂时无法读取，请刷新列表并检查存储连接后重试。'
      else if (fallback) {
        const video = videoRef.current
        setInitialTime(video?.videoWidth ? video.currentTime : 0)
        setInitialVolume(video?.muted ? 0 : video?.volume ?? 1)
        video?.pause()
        video?.removeAttribute('src')
        video?.load()
        setError(null)
        setLoading(true)
        setMode('software')
        switching.current = false
        return
      }
    } catch {
      if (current !== generation.current || controller.signal.aborted) return
      message = '无法连接录像服务，请检查网络连接后重试。'
    }
    if (current !== generation.current) return
    switching.current = false
    setLoading(false)
    setError(message)
  }

  function retry() {
    generation.current++
    requestRef.current?.abort()
    switching.current = false
    setError(null)
    setLoading(true)
    if (mode === 'software') setAttempt(value => value + 1)
    else videoRef.current?.load()
  }

  return <>
    <div className="player-details"><strong>{file.path.split('/').at(-1)}</strong><p>{file.device_name} · 编号 {file.channel} · {bytes(file.size)}</p><p>{file.kind === 'archive' ? `${file.day} · 60× 归档` : `${datetime(file.start)} → ${datetime(file.end)}`}</p></div>
    {mode === 'native' ? <video ref={videoRef} className="recording-player" src={nativeSource} poster={file.thumbnail_path ? `/api/files/${file.id}/thumbnail` : undefined} controls playsInline autoPlay preload="metadata" aria-label="录像播放器"
      onLoadedMetadata={() => { if (videoRef.current?.videoWidth === 0) void playbackError(); else { setLoading(false); if (videoRef.current) videoRef.current.playbackRate = Number(rate) } }}
      onLoadedData={() => { if (videoRef.current?.videoWidth === 0) void playbackError() }}
      onError={() => void playbackError()}>浏览器不支持视频播放，请下载录像查看。</video>
      : <Suspense fallback={null}><SoftwareVideoPlayer key={attempt} source={source} rate={Number(rate)} initialTime={initialTime} initialVolume={initialVolume} expectedDuration={file.duration ?? file.end - file.start} onReady={() => { setLoading(false); setError(null) }} onError={() => void playbackError(false)}/></Suspense>}
    {loading && <p className="player-loading" role="status">{mode === 'software' ? '正在启用浏览器兼容播放…' : '正在加载录像…'}</p>}
    {error && <div className="player-error" role="alert"><p>{error}</p><button className="text-button" onClick={retry}><RefreshCw size={14}/>重新加载</button></div>}
    <div className="player-actions"><label>播放速度<select value={rate} onChange={event => { setRate(event.target.value); if (videoRef.current) videoRef.current.playbackRate = Number(event.target.value) }}>{['0.5', '1', '1.5', '2', '4'].map(value => <option key={value} value={value}>{value}×</option>)}</select></label><a className="secondary" href={`/api/files/${file.id}/download`}><Download size={16}/>下载录像</a></div>
    {file.kind === 'archive' && <p className="field-note">归档已按原录像时间压缩 60 倍；选择 1× 播放速度时，即为原录像的 60 倍速。</p>}
  </>
}
