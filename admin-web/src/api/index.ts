import { apiGet, postWithReason } from './http'
import type {
  AdminUser,
  JobRow,
  Overview,
  Paged,
  ReportDetail,
  ReportListItem,
  RunRow,
  ToolCallSpan,
  TraceUser,
} from '../types'

/** GET /overview → {active_users, jobs, instruments, catalog_last_sync, version} */
export function fetchOverview(): Promise<Overview> {
  return apiGet<Overview>('/overview')
}

/** GET /users?limit&offset → {items: AdminUser[]} */
export function fetchUsers(): Promise<{ items: AdminUser[] }> {
  return apiGet<{ items: AdminUser[] }>('/users')
}

/** GET /users/{id}/reports?limit&offset */
export function fetchUserReports(
  userId: string,
  limit = 30,
  offset = 0,
): Promise<Paged<ReportListItem>> {
  return apiGet<Paged<ReportListItem>>(`/users/${userId}/reports`, { limit, offset })
}

/** GET /users/{id}/reports/{report_id} */
export function fetchReportDetail(userId: string, reportId: string): Promise<ReportDetail> {
  return apiGet<ReportDetail>(`/users/${userId}/reports/${reportId}`)
}

/** POST /users/{id}/reports (202)。kind: daily|weekly，period: 用户当日日期，replay 强制重跑。 */
export function triggerReport(
  userId: string,
  body: { kind: 'daily' | 'weekly'; period: string; replay?: boolean },
  reason: string,
): Promise<{ queued: boolean }> {
  return postWithReason<{ queued: boolean }>(`/users/${userId}/reports`, body, reason)
}

/** GET /jobs → 最近 100 条任务 */
export function fetchJobs(): Promise<JobRow[]> {
  return apiGet<JobRow[]>('/jobs')
}

/** GET /trace-users?query&offset&limit */
export function fetchTraceUsers(
  query: string,
  offset = 0,
  limit = 100,
): Promise<Paged<TraceUser>> {
  return apiGet<Paged<TraceUser>>('/trace-users', { query, offset, limit })
}

export interface RunFilters {
  user_id?: string
  status?: string
  triggered_by?: string
  offset?: number
  limit?: number
}

/** GET /runs?user_id&status&triggered_by&offset&limit */
export function fetchRuns(filters: RunFilters): Promise<Paged<RunRow>> {
  return apiGet<Paged<RunRow>>('/runs', { ...filters })
}

/** GET /runs/{run_id}/tool-calls?offset&limit（每页最多 200 条） */
export function fetchToolCalls(
  runId: string,
  offset = 0,
  limit = 200,
): Promise<Paged<ToolCallSpan>> {
  return apiGet<Paged<ToolCallSpan>>(`/runs/${runId}/tool-calls`, { offset, limit })
}
