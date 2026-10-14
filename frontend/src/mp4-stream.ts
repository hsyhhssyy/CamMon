import type AVPlayer from '@libmedia/avplayer/AVPlayer'

const CHUNK = 1024 * 1024
const EOF = -(1 << 20) // libmedia IOError.END
type Box = { start: number; end: number; body: number; type: string }

function tag(data: Uint8Array, offset: number): string {
  return String.fromCharCode(...data.subarray(offset, offset + 4))
}

function boxes(data: Uint8Array, from = 0, to = data.length): Box[] {
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength)
  const result: Box[] = []
  for (let offset = from; offset + 8 <= to;) {
    let length = view.getUint32(offset)
    const header = length === 1 ? 16 : 8
    if (header === 16) {
      if (offset + 16 > to) break
      length = Number(view.getBigUint64(offset + 8))
    }
    if (length === 0) length = to - offset
    if (!Number.isSafeInteger(length) || length < header) break
    result.push({ start: offset, end: offset + length, body: offset + header, type: tag(data, offset + 4) })
    if (offset + length > to) break
    offset += length
  }
  return result
}

export function mp4VideoCodec(head: Uint8Array): string | null {
  const walk = (from: number, to: number): string | null => {
    for (const box of boxes(head, from, to)) {
      if (box.end > head.length) continue
      if (['moov', 'trak', 'mdia', 'minf', 'stbl'].includes(box.type)) {
        const codec = walk(box.body, box.end)
        if (codec) return codec
      }
      if (box.type !== 'stsd') continue
      for (const entry of boxes(head, box.body + 8, box.end)) {
        if (!['hvc1', 'hev1', 'avc1', 'avc3'].includes(entry.type)) continue
        const config = boxes(head, entry.body + 78, entry.end)
        const avcc = config.find(child => child.type === 'avcC')
        if (avcc && avcc.body + 4 <= head.length) {
          const profile = [...head.subarray(avcc.body + 1, avcc.body + 4)].map(value => value.toString(16).padStart(2, '0')).join('')
          return `${entry.type}.${profile}`
        }
        const hvcc = config.find(child => child.type === 'hvcC')
        if (hvcc && hvcc.body + 13 <= head.length) {
          const view = new DataView(head.buffer, head.byteOffset, head.byteLength)
          const profile = head[hvcc.body + 1]
          let compatibility = view.getUint32(hvcc.body + 2), reversed = 0
          for (let i = 0; i < 32; i++) { reversed = (reversed << 1) | (compatibility & 1); compatibility >>>= 1 }
          const space = ['', 'A', 'B', 'C'][profile >>> 6]
          const tier = profile & 32 ? 'H' : 'L'
          const constraints = [...head.subarray(hvcc.body + 6, hvcc.body + 12)]
          while (constraints.length && constraints.at(-1) === 0) constraints.pop()
          return `${entry.type}.${space}${profile & 31}.${(reversed >>> 0).toString(16)}.${tier}${head[hvcc.body + 12]}${constraints.map(value => '.' + value.toString(16)).join('')}`
        }
        return entry.type
      }
    }
    return null
  }
  try { return walk(0, head.length) } catch { return null }
}

// Xiaomi fragmented MP4s carry a populated SIDX but no MFRA, and leave track
// duration at zero. libmedia seeks using MFRA. Present an equivalent index in
// the browser's virtual input without rewriting or downloading the recording.
function fragmentIndex(head: Uint8Array, fileSize: number): Uint8Array {
  const top = boxes(head)
  const moov = top.find(box => box.type === 'moov' && box.end <= head.length)
  const sidx = top.find(box => box.type === 'sidx')
  if (!moov || !sidx || sidx.body + 24 > head.length) return new Uint8Array()
  const children = boxes(head, moov.body, moov.end)
  if (!children.some(box => box.type === 'mvex')) return new Uint8Array()
  const view = new DataView(head.buffer, head.byteOffset, head.byteLength)
  const p = sidx.body
  const version = head[p]
  if (version !== 0 && version !== 1) return new Uint8Array()
  const trackId = view.getUint32(p + 4)
  const timescale = view.getUint32(p + 8)
  let trackScale = 0
  for (const trak of children.filter(box => box.type === 'trak')) {
    const parts = boxes(head, trak.body, trak.end)
    const tkhd = parts.find(box => box.type === 'tkhd')
    const mdia = parts.find(box => box.type === 'mdia')
    if (!tkhd || !mdia) continue
    const idOffset = tkhd.body + (head[tkhd.body] === 1 ? 20 : 12)
    if (idOffset + 4 > tkhd.end || view.getUint32(idOffset) !== trackId) continue
    const mdhd = boxes(head, mdia.body, mdia.end).find(box => box.type === 'mdhd')
    if (mdhd) {
      const scaleOffset = mdhd.body + (head[mdhd.body] === 1 ? 20 : 12)
      if (scaleOffset + 4 <= mdhd.end) trackScale = view.getUint32(scaleOffset)
    }
  }
  if (!timescale || !trackScale) return new Uint8Array()
  const countOffset = p + (version === 1 ? 30 : 22)
  if (countOffset + 2 > head.length) return new Uint8Array()
  const count = view.getUint16(countOffset)
  const entriesOffset = countOffset + 2
  if (!count || entriesOffset + count * 12 > Math.min(head.length, sidx.end)) return new Uint8Array()
  let time = version === 1 ? view.getBigUint64(p + 12) : BigInt(view.getUint32(p + 12))
  const firstOffset = version === 1 ? Number(view.getBigUint64(p + 20)) : view.getUint32(p + 16)
  let position = sidx.end + firstOffset
  const entries: { time: bigint; position: number }[] = []
  for (let i = 0; i < count; i++) {
    const offset = entriesOffset + i * 12
    const size = view.getUint32(offset)
    const duration = view.getUint32(offset + 4)
    const sap = view.getUint32(offset + 8)
    // Hierarchical indexes and invalid/out-of-file references need a different
    // parser; never manufacture seek offsets for them.
    if (size >>> 31 || !size || !duration || !Number.isSafeInteger(position) || position + size > fileSize) return new Uint8Array()
    if (sap >>> 31 && ((sap >>> 28) & 7) <= 3) {
      entries.push({ time: time * BigInt(trackScale) / BigInt(timescale), position })
    }
    position += size
    time += BigInt(duration)
  }
  if (!entries.length) return new Uint8Array()
  const tfraSize = 24 + entries.length * 19
  const index = new Uint8Array(8 + tfraSize + 16)
  const output = new DataView(index.buffer)
  const writeBox = (offset: number, size: number, name: string) => {
    output.setUint32(offset, size)
    index.set([...name].map(char => char.charCodeAt(0)), offset + 4)
  }
  writeBox(0, index.length, 'mfra')
  writeBox(8, tfraSize, 'tfra')
  index[16] = 1 // 64-bit time and file offsets
  output.setUint32(20, trackId)
  output.setUint32(28, entries.length)
  entries.forEach((entry, i) => {
    const offset = 32 + i * 19
    output.setBigUint64(offset, entry.time)
    output.setBigUint64(offset + 8, BigInt(entry.position))
    index.set([1, 1, 1], offset + 16)
  })
  writeBox(8 + tfraSize, 16, 'mfro')
  output.setUint32(index.length - 4, index.length)
  return index
}

export function createMp4Stream(player: AVPlayer, url: string) {
  const Base = (player.constructor as typeof AVPlayer).IOLoader.CustomIOLoader
  return new class extends Base {
    private controller = new AbortController()
    private position = 0
    private length = 0
    private cacheStart = 0
    private cache: Uint8Array = new Uint8Array()
    private index: Uint8Array = new Uint8Array()
    get ext() { return 'mp4' }
    get flags() { return 1 | 4 } // SEEKABLE | NETWORK
    get minBuffer() { return 2 }

    private async range(start: number, end: number): Promise<Uint8Array> {
      const response = await fetch(url, {
        headers: { Range: `bytes=${start}-${end}` },
        credentials: 'same-origin', signal: this.controller.signal,
      })
      if (response.status !== 206 || response.headers.get('Content-Range') !== `bytes ${start}-${end}/${this.length}`) {
        await response.body?.cancel()
        throw new Error('Recording service did not return the requested byte range')
      }
      const data = new Uint8Array(await response.arrayBuffer())
      if (data.length !== end - start + 1) throw new Error('Incomplete recording range')
      return data
    }

    async open() {
      const response = await fetch(url, { method: 'HEAD', credentials: 'same-origin', signal: this.controller.signal })
      this.length = Number(response.headers.get('Content-Length'))
      if (!response.ok || !Number.isSafeInteger(this.length) || this.length <= 0) throw new Error('Recording unavailable')
      this.cache = await this.range(0, Math.min(CHUNK, this.length) - 1)
      try { this.index = fragmentIndex(this.cache, this.length) } catch { this.index = new Uint8Array() }
      if (this.index.length && this.length >= 12) {
        const tail = await this.range(this.length - 12, this.length - 1)
        if (tag(tail, 0) === 'mfro') this.index = new Uint8Array()
      }
      return 0
    }

    async read(buffer: { length: number; set: (data: ArrayLike<number>, offset?: number) => void }) {
      if (this.controller.signal.aborted || this.position >= this.length + this.index.length) return EOF
      if (this.position >= this.length) {
        const data = this.index.subarray(this.position - this.length, this.position - this.length + buffer.length)
        buffer.set(data)
        this.position += data.length
        return data.length
      }
      if (this.position < this.cacheStart || this.position >= this.cacheStart + this.cache.length) {
        this.cacheStart = this.position
        this.cache = await this.range(this.position, Math.min(this.position + CHUNK, this.length) - 1)
      }
      const offset = this.position - this.cacheStart
      const data = this.cache.subarray(offset, offset + buffer.length)
      buffer.set(data)
      this.position += data.length
      return data.length
    }

    async seek(position: bigint) {
      const value = Number(position)
      if (!Number.isSafeInteger(value) || value < 0 || value > this.length + this.index.length) throw new Error('Invalid seek')
      this.position = value
      return 0
    }
    async size() { return BigInt(this.length + this.index.length) }
    async stop() { this.controller.abort(); this.cache = new Uint8Array() }
  }()
}
