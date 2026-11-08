<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  NAlert,
  NCard,
  NCollapse,
  NCollapseItem,
  NDataTable,
  NEmpty,
  NGrid,
  NGridItem,
  NSelect,
  NSpace,
  NSpin,
  NStatistic,
  NTag,
  type DataTableColumns,
  type SelectOption,
} from 'naive-ui'
import { fetchReportDetail, fetchUserReports, fetchUsers } from '../api'
import UserSelect from '../components/UserSelect.vue'
import JsonBlock from '../components/JsonBlock.vue'
import { useRequest } from '../composables/useRequest'
import type {
  AdminUser,
  ReportCoverage,
  ReportDetail,
  ReportListItem,
  ReportSnapshot,
  SnapshotAnalysis,
  SnapshotPosition,
} from '../types'
import { fmtDateTime, fmtMoney, shortUuid } from '../utils/format'

const users = ref<AdminUser[]>([])
const reports = ref<ReportListItem[]>([])
const detail = ref<ReportDetail | null>(null)
const selectedUserId = ref<string | null>(null)
const selectedReportId = ref<string | null>(null)

const usersReq = useRequest()
const reportsReq = useRequest()
const detailReq = useRequest()

async function loadUsers(): Promise<void> {
  const data = await usersReq.run(fetchUsers)
  if (!data) return
  users.value = data.items
  if (!selectedUserId.value && users.value.length > 0) {
    selectedUserId.value = users.value[0].id
  }
}

async function loadReports(): Promise<void> {
  selectedReportId.value = null
  detail.value = null
  reports.value = []
  if (!selectedUserId.value) return
  const data = await reportsReq.run(() => fetchUserReports(selectedUserId.value as string, 30))
  if (data) reports.value = data.items
}

async function loadDetail(): Promise<void> {
  detail.value = null
  if (!selectedUserId.value || !selectedReportId.value) return
  const data = await detailReq.run(() =>
    fetchReportDetail(selectedUserId.value as string, selectedReportId.value as string),
  )
  if (data) detail.value = data
}

watch(selectedUserId, loadReports)
watch(selectedReportId, loadDetail)
onMounted(loadUsers)

const reportOptions = computed<SelectOption[]>(() =>
  reports.value.map((r) => ({
    label: `${r.title || '未命名报告'} · ${r.period} · ${
      r.analysis_kind === 'ai' ? 'AI' : '事实'
    }${r.read ? ' · 已读' : ''}`,
    value: r.id,
  })),
)

const snapshot = computed<ReportSnapshot>(() => detail.value?.snapshot ?? {})
const analysis = computed<SnapshotAnalysis>(() => snapshot.value.analysis ?? {})
const coverage = computed<ReportCoverage>(() => snapshot.value.coverage ?? {})
const positions = computed<SnapshotPosition[]>(() => snapshot.value.positions ?? [])

const positionColumns: DataTableColumns<SnapshotPosition> = [
  {
    title: '标的',
    key: 'symbol',
    render: (row) => `${row.symbol ?? ''} ${row.name ?? ''}`.trim() || '—',
  },
  { title: '市场', key: 'market', width: 90 },
  { title: '数量', key: 'quantity', width: 110, render: (row) => fmtMoney(row.quantity) },
  { title: '成本', key: 'average_cost', width: 110, render: (row) => fmtMoney(row.average_cost) },
  { title: '现价', key: 'price', width: 110, render: (row) => fmtMoney(row.price) },
  {
    title: '状态',
    key: 'quote_status',
    width: 120,
    render: (row) => row.quote_status ?? '—',
  },
  { title: '市值', key: 'market_value', width: 130, render: (row) => fmtMoney(row.market_value) },
  {
    title: '市值(CNY)',
    key: 'market_value_cny',
    width: 130,
    render: (row) => fmtMoney(row.market_value_cny),
  },
]
</script>

<template>
  <div class="page">
    <n-space vertical size="large">
      <div class="page-head">
        <h1>报告查看</h1>
      </div>

      <n-alert
        v-if="usersReq.error"
        type="error"
        :bordered="false"
        closable
        @close="usersReq.clearError"
      >
        {{ usersReq.error }}
      </n-alert>

      <n-card>
        <n-space vertical :size="12">
          <div>
            <div class="pa-muted" style="margin-bottom: 4px">选择用户</div>
            <n-spin :show="usersReq.loading">
              <user-select v-model="selectedUserId" :users="users" />
            </n-spin>
          </div>
          <n-alert v-if="users.length === 0 && !usersReq.loading" type="info" :bordered="false">
            没有可操作的用户。
          </n-alert>
        </n-space>
      </n-card>

      <template v-if="selectedUserId">
        <n-card title="选择报告">
          <n-alert
            v-if="reportsReq.error"
            type="error"
            :bordered="false"
            closable
            @close="reportsReq.clearError"
            style="margin-bottom: 12px"
          >
            {{ reportsReq.error }}
          </n-alert>
          <n-spin :show="reportsReq.loading">
            <n-select
              v-model:value="selectedReportId"
              :options="reportOptions"
              placeholder="选择报告"
              filterable
              :disabled="reports.length === 0"
            />
            <div class="pa-muted" style="margin-top: 8px">
              共 {{ reports.length }} 份报告（最近 30 份），按生成时间倒序。
            </div>
            <n-empty
              v-if="reports.length === 0 && !reportsReq.loading"
              description="该用户还没有生成过报告。到「触发报告」页可手动生成盘后复盘或周报。"
              style="margin-top: 16px"
            />
          </n-spin>
        </n-card>

        <n-card v-if="detailReq.error" title="报告详情">
          <n-alert
            type="error"
            :bordered="false"
            closable
            @close="detailReq.clearError"
          >
            {{ detailReq.error }}
          </n-alert>
        </n-card>

        <n-spin :show="detailReq.loading">
          <template v-if="detail">
            <n-grid cols="2 s:2 m:4" :x-gap="12" :y-gap="12" responsive="screen">
              <n-grid-item>
                <n-card>
                  <n-statistic
                    label="类型"
                    :value="detail.kind.startsWith('daily') ? '收盘总结' : '每周复盘'"
                  />
                </n-card>
              </n-grid-item>
              <n-grid-item>
                <n-card><n-statistic label="期间" :value="detail.period" /></n-card>
              </n-grid-item>
              <n-grid-item>
                <n-card>
                  <n-statistic
                    label="估值覆盖"
                    :value="`${coverage.priced ?? '—'}/${coverage.total ?? '—'}`"
                  />
                </n-card>
              </n-grid-item>
              <n-grid-item>
                <n-card>
                  <n-statistic label="行情估值(CNY)" :value="fmtMoney(snapshot.market_value_cny)" />
                </n-card>
              </n-grid-item>
            </n-grid>

            <n-card>
              <n-space :size="8">
                <n-tag
                  :type="analysis.kind === 'ai' ? 'success' : 'default'"
                  :bordered="false"
                  size="small"
                >
                  {{ analysis.kind === 'ai' ? 'AI 摘要' : '事实基线' }}
                </n-tag>
                <n-tag
                  :type="analysis.status === 'generated' ? 'success' : 'warning'"
                  :bordered="false"
                  size="small"
                >
                  分析状态 {{ analysis.status ?? '—' }}
                </n-tag>
                <n-tag type="info" :bordered="false" size="small">
                  {{ detail.read ? '已读' : '未读' }}
                </n-tag>
              </n-space>
              <div class="pa-muted" style="margin-top: 8px">
                生成时间 {{ fmtDateTime(detail.created_at) }} · 报告 ID
                {{ shortUuid(detail.id) }}
              </div>
            </n-card>

            <n-card title="摘要">
              <div style="white-space: pre-wrap; line-height: 1.7">{{ detail.summary || '—' }}</div>
            </n-card>

            <n-card v-if="positions.length > 0" title="持仓快照">
              <n-data-table
                :columns="positionColumns"
                :data="positions"
                :bordered="false"
                size="small"
                :scroll-x="960"
              />
            </n-card>

            <n-card title="原始快照 JSON">
              <n-collapse>
                <n-collapse-item title="查看完整快照（JSON）" name="snapshot">
                  <JsonBlock :value="snapshot" />
                </n-collapse-item>
              </n-collapse>
            </n-card>
          </template>
        </n-spin>
      </template>
    </n-space>
  </div>
</template>
