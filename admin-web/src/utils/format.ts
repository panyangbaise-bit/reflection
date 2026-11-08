/** 展示格式化与校验工具。 */

export function fmtMoney(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  const n = Number(value)
  if (Number.isNaN(n)) return '—'
  return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

export function fmtNumber(value: unknown): string {
  if (value === null || value === undefined) return '—'
  const n = Number(value)
  if (Number.isNaN(n)) return '—'
  return n.toLocaleString('zh-CN')
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { hour12: false })
}

/** catalog_last_sync 为 epoch 秒（int）。 */
export function fmtEpoch(seconds: number | null | undefined): string {
  if (!seconds) return '未记录'
  return new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false })
}

export function parseIsoMs(value: string): number {
  const t = new Date(value).getTime()
  return Number.isNaN(t) ? 0 : t
}

export function parseSummaryJson(raw: string): unknown {
  if (!raw) return {}
  try {
    return JSON.parse(raw)
  } catch {
    return raw
  }
}

export function jsonText(value: unknown): string {
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value)
  }
}

export function shortUuid(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : '—'
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export function isValidUuid(value: string): boolean {
  return UUID_RE.test(value)
}

export type TagType = 'default' | 'success' | 'error' | 'warning' | 'info'

export function statusTagType(status: string | null | undefined): TagType {
  switch (status) {
    case 'succeeded':
    case 'generated':
    case 'completed':
    case 'active':
      return 'success'
    case 'failed':
    case 'error':
      return 'error'
    case 'running':
    case 'queued':
    case 'pending':
      return 'info'
    case 'expired':
    case 'revoked':
      return 'warning'
    default:
      return 'default'
  }
}

export function spanBarColor(status: string | null | undefined): string {
  switch (status) {
    case 'failed':
    case 'error':
      return '#d03050'
    case 'succeeded':
    case 'finished':
    case 'completed':
      return '#4169a1'
    default:
      return '#f0a020'
  }
}
