/** Admin API 响应结构（与 api.py /api/admin/v1/* 严格对应，勿改动后端）。 */

export interface Overview {
  active_users: number
  jobs: Record<string, number>
  instruments: number
  catalog_last_sync: number | null
  version: string
}

export interface AdminUser {
  id: string
  phone_masked: string | null
  created_at: string
  has_password: boolean
  today: string
  daily_today: boolean
  weekly_today: boolean
}

export interface ReportCoverage {
  priced?: number
  total?: number
}

export interface ReportListItem {
  id: string
  kind: string
  period: string
  title: string
  summary: string | null
  complete: boolean | null
  coverage: ReportCoverage | null
  market_value_cny: string | number | null
  analysis_kind: string | null
  analysis_status: string | null
  created_at: string
  read: boolean
}

export interface SnapshotPosition {
  symbol?: string | null
  name?: string | null
  market?: string | null
  quantity?: number | string | null
  average_cost?: number | string | null
  price?: number | string | null
  quote_status?: string | null
  market_value?: number | string | null
  market_value_cny?: number | string | null
  [key: string]: unknown
}

export interface SnapshotAnalysis {
  kind?: string | null
  status?: string | null
  provider?: string | null
  disclosure_version?: string | null
}

export interface ReportSnapshot {
  complete?: boolean | null
  coverage?: ReportCoverage | null
  market_value_cny?: string | number | null
  analysis?: SnapshotAnalysis | null
  positions?: SnapshotPosition[] | null
  [key: string]: unknown
}

export interface ReportDetail {
  id: string
  kind: string
  period: string
  title: string
  summary: string | null
  snapshot: ReportSnapshot
  created_at: string
  read: boolean
}

export interface JobRow {
  id: string
  kind: string
  status: string
  attempt: number
  error_code: string | null
  due_at: string | null
}

export interface TraceUser {
  user_id: string
  label: string | null
  run_count: number
  last_run_at: string
}

export interface RunUsage {
  model?: string
  input_tokens?: number
  output_tokens?: number
  degraded_reason?: string | null
}

export interface RunRow {
  id: string
  run_id: string | null
  user_id: string
  triggered_by: string
  status: string
  created_at: string
  attempt: number
  error_code: string | null
  usage: RunUsage
}

export interface ToolCallContent {
  input?: unknown
  output?: unknown
  preview?: string
  truncated?: boolean
  [key: string]: unknown
}

/** 与 AgentToolCall 序列化一致（api.py admin_tool_calls：除 user_id 外的全部列）。 */
export interface ToolCallSpan {
  id: string
  run_id: string | null
  job_id: string
  round: number
  tool_name: string
  call_id: string
  triggered_by: string
  status: string
  arguments: Record<string, unknown>
  result_summary: string
  content: ToolCallContent | null
  error_code: string | null
  finished_at: string | null
  duration_ms: number
  created_at: string
}

export interface Paged<T> {
  items: T[]
  next_offset: number | null
}
