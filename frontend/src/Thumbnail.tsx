import { useState } from 'react'
import { Image, LoaderCircle } from 'lucide-react'

export default function Thumbnail({ src, alt, pending = false, message = '首帧暂不可用' }: {
  src?: string; alt: string; pending?: boolean; message?: string
}) {
  const [result, setResult] = useState<{ src?: string; status: 'loaded' | 'failed' }>()
  const failed = result?.src === src && result?.status === 'failed'
  const loaded = result?.src === src && result?.status === 'loaded'
  const waiting = pending || (!!src && !failed && !loaded)
  return <div className="video-thumbnail" title={loaded ? '视频首帧' : message}>
    {src && !failed && <img src={src} alt={alt} loading="lazy" onLoad={() => setResult({ src, status: 'loaded' })} onError={() => setResult({ src, status: 'failed' })}/>}
    {!loaded && <span className="thumbnail-placeholder">{waiting ? <LoaderCircle aria-hidden="true" size={22} className="thumbnail-spinner"/> : <Image aria-hidden="true" size={24}/>}<small>{waiting ? src ? '正在加载首帧…' : '正在生成首帧…' : message}</small></span>}
    {loaded && <span className="thumbnail-caption">首帧</span>}
  </div>
}
