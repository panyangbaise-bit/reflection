<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  NAlert,
  NButton,
  NCard,
  NDescriptions,
  NDescriptionsItem,
  NGrid,
  NGridItem,
  NSpace,
  NSpin,
  NStatistic,
} from 'naive-ui'
import { fetchOverview } from '../api'
import { useRequest } from '../composables/useRequest'
import type { Overview } from '../types'
import { fmtEpoch, fmtNumber } from '../utils/format'

const overview = ref<Overview | null>(null)
const req = useRequest()

async function load(): Promise<void> {
  const data = await req.run(fetchOverview)
  if (data) overview.value = data
}

onMounted(load)
</script>

<template>
  <div class="page">
    <n-space vertical size="large">
      <div class="page-head">
        <h1>运行概览</h1>
        <n-button size="small" :loading="req.loading" @click="load">刷新</n-button>
      </div>

      <n-alert v-if="req.error" type="error" :bordered="false" closable @close="req.error = null">
        {{ req.error }}
      </n-alert>

      <n-spin :show="req.loading">
        <template v-if="overview">
          <n-grid cols="2 s:2 m:4" :x-gap="12" :y-gap="12" responsive="screen">
            <n-grid-item>
              <n-card>
                <n-statistic label="有效用户" :value="overview.active_users" />
              </n-card>
            </n-grid-item>
            <n-grid-item>
              <n-card>
                <n-statistic label="标的目录" :value="fmtNumber(overview.instruments)" />
              </n-card>
            </n-grid-item>
            <n-grid-item>
              <n-card>
                <n-statistic label="任务成功" :value="overview.jobs.succeeded ?? 0" />
              </n-card>
            </n-grid-item>
            <n-grid-item>
              <n-card>
                <n-statistic label="任务失败" :value="overview.jobs.failed ?? 0" />
              </n-card>
            </n-grid-item>
          </n-grid>

          <n-card title="服务状态">
            <n-descriptions :column="2" size="small">
              <n-descriptions-item label="版本">{{ overview.version || '—' }}</n-descriptions-item>
              <n-descriptions-item label="目录最近同步">
                {{ fmtEpoch(overview.catalog_last_sync) }}
              </n-descriptions-item>
              <n-descriptions-item label="有效用户">{{ overview.active_users }}</n-descriptions-item>
              <n-descriptions-item label="标的">{{ fmtNumber(overview.instruments) }}</n-descriptions-item>
            </n-descriptions>
            <div class="pa-muted" style="margin-top: 8px">
              数据来自管理 API 实时读取；任务统计为累计值。
            </div>
          </n-card>
        </template>
      </n-spin>
    </n-space>
  </div>
</template>
