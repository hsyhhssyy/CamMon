export type Device = { id: string; name: string; share: string; username: string; retention_days: number; enabled: number; password?: string }
export type Recording = { id: number; device_id: string; device_name: string; path: string; channel: string; day: string; start: number; end: number; kind: 'original' | 'archive'; state: string; size: number; error: string | null; duration: number | null }
export type Storage = { protocol: 'smb' | 'nfs'; address: string; username: string; password?: string | null; has_password?: boolean; domain: string; encrypt: boolean; nfs_version: 3 | 4; uid: number; gid: number }
export type Job = { id: string; day: string; channel: string; device_id: string; status: string; error: string | null }
export type MetadataStatus = { configured: boolean; connected: boolean; schema: string; pending_rows: number; last_sync: number | null; error: string | null }
export type Status = { storage_configured: boolean; cache_bytes: number; cache_available_bytes: number; cache_limit_bytes: number; cache_error: string | null; pending_files: number; writing_handles: number; protocol_error: string | null; worker_error: string | null; alerts: { id: number; path: string; error: string }[]; jobs: Job[]; metadata: MetadataStatus }
export type Statistics = { count: number; bytes: number; groups: { day: string; kind: string; count: number; bytes: number }[] }
export type FilePage = { items: Recording[]; count: number; bytes: number }

export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, {
    method, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CamMon-Request': '1' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail ?? '请求失败')
    const error = new Error(detail) as Error & { status: number }
    error.status = response.status
    throw error
  }
  return data as T
}

export function bytes(value: number): string {
  if (value < 1024) return `${value} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let number = value / 1024, i = 0
  while (number >= 1024 && i < units.length - 1) { number /= 1024; i++ }
  return `${number.toFixed(number >= 100 ? 0 : 2)} ${units[i]}`
}

export function datetime(value: number) {
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'medium', hour12: false }).format(value * 1000)
}
