import { ref } from 'vue'
import { getToken, saveToken } from '../api/http'

/** 全局管理员令牌状态（localStorage 持久化）。 */
export const adminToken = ref(getToken())

export function setAdminToken(value: string): void {
  saveToken(value)
  adminToken.value = value
}

export function clearAdminToken(): void {
  saveToken('')
  adminToken.value = ''
}
