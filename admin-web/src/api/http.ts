import axios, { type AxiosError } from 'axios'

const TOKEN_KEY = 'pa-admin-token'

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

export function saveToken(value: string): void {
  try {
    if (value) localStorage.setItem(TOKEN_KEY, value)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* 存储不可用（隐私模式等）时仅会话内生效 */
  }
}

export class ApiError extends Error {
  status: number
  code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

interface ErrorBody {
  error?: { code?: string; message?: string; fields?: unknown }
}

/** 与后端 /api/admin/v1 通信的 axios 实例；响应已解包为 data.data。 */
export const http = axios.create({
  baseURL: '/api/admin/v1',
  timeout: 30000,
})

http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (response) => response.data?.data,
  (error: AxiosError<ErrorBody>) => {
    const status = error.response?.status ?? 0
    const payload = error.response?.data ?? {}
    const message = payload.error?.message ?? error.message ?? '未知错误'
    const code = payload.error?.code ?? ''
    if (status === 401) {
      throw new ApiError(401, code, '需要管理员身份：请在顶部输入管理员令牌。')
    }
    if (status === 403) {
      if (message.includes('凭据无效')) {
        throw new ApiError(403, code, '管理员凭据无效：请检查令牌后重新输入。')
      }
      throw new ApiError(403, code, `权限不足：${message}（请检查令牌角色）。`)
    }
    if (status === 404) {
      throw new ApiError(404, code, `未找到：${message || '请检查用户/报告 ID 是否正确。'}`)
    }
    if (status === 422) {
      throw new ApiError(422, code, `请求无效：${message || '参数校验未通过'}`)
    }
    throw new ApiError(status, code, `请求失败（HTTP ${status || '网络错误'}）：${message}`)
  },
)

export async function apiGet<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  // 响应拦截器已把 data.data 解包返回；这里做显式断言以兼容不同 axios 版本的泛型签名。
  return (await http.get(url, { params })) as unknown as T
}

export async function apiPost<T>(url: string, body?: unknown, reason?: string): Promise<T> {
  const headers: Record<string, string> = {}
  if (reason) headers['X-Admin-Reason'] = reason
  return (await http.post(url, body, { headers })) as unknown as T
}

/**
 * 带审计原因的管理端 POST。reason 为操作说明（后端要求 5–500 字符），
 * 会写入 AuditLog（Admin API 的 X-Admin-Reason 审计头）。
 */
export async function postWithReason<T>(
  path: string,
  body: unknown,
  reason: string,
): Promise<T> {
  const trimmed = reason.trim()
  if (trimmed.length < 5) {
    throw new ApiError(0, 'invalid_reason', '操作原因至少 5 个字符（用于审计）。')
  }
  return apiPost<T>(path, body, trimmed)
}
