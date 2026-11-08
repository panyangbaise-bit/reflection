import { reactive } from 'vue'
import { setAdminToken } from '../stores/token'

export interface RequestState {
  loading: boolean
  error: string | null
  run<T>(fn: () => Promise<T>): Promise<T | undefined>
  clearError(): void
}

/**
 * 页面级请求状态封装：loading / error / run。
 * 返回 reactive 对象，模板里直接访问 req.loading / req.error（响应式）。
 * 凭据无效（403「管理员凭据无效」）时自动清空令牌，回到令牌输入视图。
 */
export function useRequest(): RequestState {
  const state = reactive({
    loading: false,
    error: null as string | null,
  })

  async function run<T>(fn: () => Promise<T>): Promise<T | undefined> {
    state.loading = true
    state.error = null
    try {
      return await fn()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      state.error = msg
      if (msg.includes('凭据无效')) setAdminToken('')
      return undefined
    } finally {
      state.loading = false
    }
  }

  function clearError(): void {
    state.error = null
  }

  return Object.assign(state, { run, clearError })
}
