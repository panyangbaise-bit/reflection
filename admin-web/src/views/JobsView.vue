<script setup lang="ts">
import { h, onMounted, ref } from 'vue'
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NSpace,
  NSpin,
  NTag,
  type DataTableColumns,
} from 'naive-ui'
import { fetchJobs } from '../api'
import { useRequest } from '../composables/useRequest'
import type { JobRow } from '../types'
import { fmtDateTime, shortUuid, statusTagType } from '../utils/format'

const jobs = ref<JobRow[]>([])
const req = useRequest()

async function load(): Promise<void> {
  const data = await req.run(fetchJobs)
  if (data) jobs.value = data
}

onMounted(load)

const columns: DataTableColumns<JobRow> = [
  {
    title: 'ID',
    key: 'id',
    width: 110,
    render: (row) =>
      h(
        'span',
        { title: row.id },
        shortUuid(row.id),
      ),
  },
  { title: '类型', key: 'kind', width: 140 },
  {
    title: '状态',
    key: 'status',
    width: 120,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: statusTagType(row.status), bordered: false },
        { default: () => row.status },
      ),
  },
  { title: '尝试', key: 'attempt', width: 80 },
  {
    title: '错误代码',
    key: 'error_code',
    width: 200,
    render: (row) =>
      row.error_code
        ? h(NTag, { size: 'small', type: 'error', bordered: false }, { default: () => row.error_code })
        : '—',
  },
  { title: '到期时间', key: 'due_at', render: (row) => fmtDateTime(row.due_at) },
]
</script>

<template>
  <div class="page">
    <n-space vertical size="large">
      <div class="page-head">
        <h1>任务状态</h1>
        <n-button size="small" :loading="req.loading" @click="load">刷新</n-button>
      </div>

      <n-alert v-if="req.error" type="error" :bordered="false" closable @close="req.error = null">
        {{ req.error }}
      </n-alert>

      <n-spin :show="req.loading">
        <n-card>
          <div class="pa-muted" style="margin-bottom: 12px">
            共 {{ jobs.length }} 条（最近 100 条，按创建时间倒序）。
          </div>
          <n-data-table :columns="columns" :data="jobs" :bordered="false" size="small" />
        </n-card>
      </n-spin>
    </n-space>
  </div>
</template>
