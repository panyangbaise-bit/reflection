<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  NAlert,
  NButton,
  NCard,
  NCheckbox,
  NGrid,
  NGridItem,
  NSpin,
  NTag,
  NTooltip,
  useMessage,
} from 'naive-ui'
import { fetchUsers, triggerReport } from '../api'
import UserSelect from '../components/UserSelect.vue'
import { useRequest } from '../composables/useRequest'
import type { AdminUser } from '../types'
import { shortUuid } from '../utils/format'

const message = useMessage()
const users = ref<AdminUser[]>([])
const selectedUserId = ref<string | null>(null)
const replay = ref(false)
const dailyBusy = ref(false)
const weeklyBusy = ref(false)

const req = useRequest()

async function loadUsers(): Promise<void> {
  const data = await req.run(fetchUsers)
  if (!data) return
  users.value = data.items
  if (!selectedUserId.value && users.value.length > 0) {
    selectedUserId.value = users.value[0].id
  }
}

onMounted(loadUsers)

const selectedUser = computed<AdminUser | null>(
  () => users.value.find((u) => u.id === selectedUserId.value) ?? null,
)

const dailyDisabled = computed(() => (selectedUser.value?.daily_today ?? false) && !replay.value)
const weeklyDisabled = computed(() => (selectedUser.value?.weekly_today ?? false) && !replay.value)

async function fire(kind: 'daily' | 'weekly', label: string): Promise<void> {
  const user = selectedUser.value
  if (!user) return
  if (kind === 'daily') dailyBusy.value = true
  else weeklyBusy.value = true
  try {
    const data = await triggerReport(
      user.id,
      { kind, period: user.today, replay: replay.value },
      replay.value ? 'admin-ui-replay' : 'admin-ui-trigger',
    )
    if (data?.queued) {
      message.success(
        `${label}已${replay.value ? '重跑' : '入队'}（period=${user.today}），约 1 分钟后可在「报告查看」看到。`,
      )
      await loadUsers()
    }
  } finally {
    if (kind === 'daily') dailyBusy.value = false
    else weeklyBusy.value = false
  }
}
</script>

<template>
  <div class="page">
    <n-space vertical size="large">
      <div class="page-head">
        <h1>触发报告</h1>
      </div>

      <div class="pa-muted" style="margin-top: -16px">
        为选中用户手动生成当日报告；盘后复盘与每周复盘以外的高级类型尚未实现。
      </div>

      <n-alert v-if="req.error" type="error" :bordered="false" closable @close="req.error = null">
        {{ req.error }}
      </n-alert>

      <n-card>
        <n-space vertical :size="12">
          <div>
            <div class="pa-muted" style="margin-bottom: 4px">选择用户</div>
            <n-spin :show="req.loading">
              <user-select v-model="selectedUserId" :users="users" />
            </n-spin>
          </div>
          <n-alert v-if="users.length === 0 && !req.loading" type="info" :bordered="false">
            没有可操作的用户。
          </n-alert>
          <div v-if="selectedUser" class="pa-muted">
            用户 {{ selectedUser.id }} · 今日（用户时区）：{{ selectedUser.today }}
          </div>
        </n-space>
      </n-card>

      <template v-if="selectedUser">
        <n-grid cols="1 m:2" :x-gap="12" :y-gap="12" responsive="screen">
          <n-grid-item>
            <n-card title="盘后复盘（今日）">
              <n-tag
                :type="selectedUser.daily_today ? 'success' : 'warning'"
                :bordered="false"
                size="small"
              >
                {{ selectedUser.daily_today ? '已生成' : '未生成' }}
              </n-tag>
              <div class="pa-muted" style="margin: 8px 0 12px">
                交易日收盘后生成，含估值快照与 AI 复盘摘要。
              </div>
              <n-button
                type="primary"
                block
                :disabled="dailyDisabled"
                :loading="dailyBusy"
                @click="fire('daily', '盘后复盘')"
              >
                {{ replay ? '重跑盘后复盘' : '生成盘后复盘' }}
              </n-button>
            </n-card>
          </n-grid-item>
          <n-grid-item>
            <n-card title="每周复盘（今日）">
              <n-tag
                :type="selectedUser.weekly_today ? 'success' : 'warning'"
                :bordered="false"
                size="small"
              >
                {{ selectedUser.weekly_today ? '已生成' : '未生成' }}
              </n-tag>
              <div class="pa-muted" style="margin: 8px 0 12px">
                按周汇总持仓变化与结构，含 AI 复盘摘要。
              </div>
              <n-button
                type="primary"
                block
                :disabled="weeklyDisabled"
                :loading="weeklyBusy"
                @click="fire('weekly', '每周复盘')"
              >
                {{ replay ? '重跑每周复盘' : '生成每周复盘' }}
              </n-button>
            </n-card>
          </n-grid-item>
        </n-grid>

        <n-card>
          <n-checkbox v-model:checked="replay">
            强制重跑：清除该类型今日报告与任务链后重新生成
          </n-checkbox>
          <div class="pa-muted" style="margin-top: 8px">
            勾选后上面的按钮不再因「已生成」而禁用；点击将先清理再重新入队（审计原因：
            admin-ui-replay）。
          </div>
        </n-card>

        <n-card title="高级报告类型（未实现）">
          <n-grid cols="1 m:2" :x-gap="12" :y-gap="12">
            <n-grid-item>
              <n-tooltip trigger="hover">
                <template #trigger>
                  <n-button block disabled>清仓分析报告</n-button>
                </template>
                尚未实现
              </n-tooltip>
            </n-grid-item>
            <n-grid-item>
              <n-tooltip trigger="hover">
                <template #trigger>
                  <n-button block disabled>重点新闻推送报告</n-button>
                </template>
                高级功能，尚未实现
              </n-tooltip>
            </n-grid-item>
          </n-grid>
        </n-card>
      </template>

      <div v-if="selectedUser" class="pa-muted" style="margin-top: -8px">
        当前选择用户：{{ shortUuid(selectedUser.id) }}（{{ selectedUser.phone_masked || '无手机号' }}）
      </div>
    </n-space>
  </div>
</template>
