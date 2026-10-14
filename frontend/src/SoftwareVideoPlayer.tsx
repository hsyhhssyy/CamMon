import { useEffect, useRef, useState } from 'react'
import { Maximize, Pause, Play, Volume2 } from 'lucide-react'
import { createSoftwarePlayer, type SoftwarePlayer } from './software-player'
import { createMp4Stream } from './mp4-stream'

function clock(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds))
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`
}

type Props = {
  source: string
  rate: number
  initialTime: number
  initialVolume: number
  expectedDuration: number
  onReady: () => void
  onError: () => void
}

export default function SoftwareVideoPlayer({ source, rate, initialTime, initialVolume, expectedDuration, onReady, onError }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const wrapperRef = useRef<HTMLDivElement>(null)
  const playerRef = useRef<SoftwarePlayer | null>(null)
  const callbacks = useRef({ onReady, onError })
  const rateRef = useRef(rate)
  callbacks.current = { onReady, onError }
  rateRef.current = rate
  const [ready, setReady] = useState(false)
  const [paused, setPaused] = useState(false)
  const [seeking, setSeeking] = useState(false)
  const [currentTime, setCurrentTime] = useState(initialTime)
  const [duration, setDuration] = useState(0)
  const [volume, setVolume] = useState(initialVolume)

  useEffect(() => {
    let disposed = false
    let player: SoftwarePlayer | null = null
    let stream: ReturnType<typeof createMp4Stream> | null = null
    let failed = false
    const fail = () => {
      if (!disposed && !failed) {
        failed = true
        setReady(false)
        if (stream) void stream.stop()
        callbacks.current.onError()
      }
    }
    async function start() {
      player = await createSoftwarePlayer(containerRef.current!)
      if (disposed) return
      playerRef.current = player
      player.on('error', fail)
      player.on('time', value => { if (!disposed) setCurrentTime(Number(value) / 1000) })
      player.on('paused', () => { if (!disposed) setPaused(true) })
      player.on('played', () => { if (!disposed) setPaused(false) })
      player.on('ended', () => { if (!disposed) setPaused(true) })
      player.on('seeking', () => { if (!disposed) setSeeking(true) })
      player.on('seeked', () => { if (!disposed) setSeeking(false) })
      player.on('firstVideoRendered', () => {
        if (!disposed && !failed) {
          setReady(true)
          callbacks.current.onReady()
        }
      })
      stream = createMp4Stream(player, new URL(source, location.href).href)
      await player.load(stream, {
        ext: 'mp4',
        http: { credentials: 'same-origin' },
      })
      if (disposed || failed) return
      if (!player.getStreams().some(stream => stream.mediaType.toLowerCase() === 'video')) throw new Error('No video track')
      // Some camera MP4s have zero track duration despite usable timestamps.
      // Ingestion already probes these files and saves their actual duration.
      setDuration(Number(player.getDuration()) / 1000 || expectedDuration)
      player.setPlaybackRate(rateRef.current)
      player.setVolume(initialVolume)
      await player.play()
      if (!disposed && !failed && initialTime > 0) await player.seek(BigInt(Math.round(initialTime * 1000)))
    }
    const starting = start().catch(fail)
    return () => {
      disposed = true
      playerRef.current = null
      if (stream) void stream.stop()
      // Abort byte reads immediately, then let pending load/play messages
      // settle before closing their worker ports.
      void starting.then(async () => {
        if (!player) return
        if (player.hasVideo() || player.hasAudio()) await player.pause().catch(() => {})
        await player.destroy()
      }).catch(() => {})
    }
  }, [source, initialTime, initialVolume, expectedDuration])

  useEffect(() => { playerRef.current?.setPlaybackRate(rate) }, [rate])

  async function toggle() {
    const player = playerRef.current
    if (!player) return
    try {
      if (paused) { await player.resume(); await player.play() }
      else await player.pause()
    } catch { callbacks.current.onError() }
  }

  async function seek(value: number) {
    if (!ready || seeking || !playerRef.current) return
    setCurrentTime(value)
    setSeeking(true)
    try { await playerRef.current.seek(BigInt(Math.round(value * 1000))) }
    catch { callbacks.current.onError() }
    finally { setSeeking(false) }
  }

  return <div ref={wrapperRef} className="software-player" aria-label="兼容录像播放器" data-ready={ready} data-paused={paused} data-time={currentTime} data-rate={rate}>
    <div ref={containerRef} className="recording-player software-picture"/>
    <div className="software-controls">
      <button className="icon-button" aria-label={paused ? '播放录像' : '暂停录像'} disabled={!ready || seeking} onClick={() => void toggle()}>{paused ? <Play size={18}/> : <Pause size={18}/>}</button>
      <span className="software-time">{clock(currentTime)} / {clock(duration)}</span>
      <input type="range" aria-label="录像进度" min="0" max={Math.max(duration, 1)} step="0.1" value={Math.min(currentTime, duration || 1)} disabled={!ready || seeking} onChange={event => void seek(Number(event.target.value))}/>
      <label className="software-volume"><Volume2 size={17}/><input type="range" aria-label="录像音量" min="0" max="1" step="0.05" value={volume} onChange={event => { const value = Number(event.target.value); setVolume(value); playerRef.current?.setVolume(value) }}/></label>
      <button className="icon-button" aria-label="全屏播放" onClick={() => { if (document.fullscreenElement) void document.exitFullscreen().catch(() => {}); else void wrapperRef.current?.requestFullscreen().catch(() => {}) }}><Maximize size={18}/></button>
    </div>
    <p className="player-mode">正在使用浏览器兼容播放 · 高分辨率录像的流畅度取决于当前设备性能</p>
  </div>
}
