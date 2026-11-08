<script setup lang="ts">
import { computed, h, onMounted, ref, watch, type HTMLAttributes } from 'vue'
import {
  NAlert,
  NButton,
  NCard,
  NCollapse,
  NCollapseItem,
  NDataTable,
  NEmpty,
  NGrid,
  NGridItem,
  NInput,
  NSelect,
  NSpace,
  NSpin,
  NStatistic,
  NTag,
  type DataTableColumns,
  type SelectOption,
} from 'naive-ui'
import { fetchRuns, fetchToolCalls, fetchTraceUsers, type RunFilters } from '../api'
import JsonBlock from '../components/JsonBlock.vue'
import { useRequest } from '../composables/useRequest'
import type { RunRow, ToolCallSpan, TraceUser } from '../types'
import {
  fmtDateTime,
  fmtNumber,
  parseIsoMs,
  parseSummaryJson,
  shortUuid,
  spanBarColor,
  statusTagType,
} from '../utils/format'

const ALL = '__all__'

// ---- 轨迹用户（trace-users） ----
const userQuery = ref('')
const userPage = ref(1)
const traceUsers = ref<TraceUser[]>([])
const userNextOffset = ref<number | null>(null)
const selectedUserValue = ref<string>(ALL)

const usersReq = useRequest()

async function loadTraceUsers(resetPage = true): Promise<void> {
  if (resetPage) userPage.value = 1
  const data = await usersReq.run(() =>
    fetchTraceUsers(userQuery.value.trim(), (userPage.value - 1) * 100, 100),
  )
  if (!data) return
  traceUsers.value = data.items
  userNextOffset.value = data.next_offset
}

const userOptions = computed<SelectOption[]>(() => [
  { label: '全部用户', value: ALL },
  ...traceUsers.value.map((u) => ({
    label: `${u.label || '用户'} · ${u.user_id} · ${u.run_count} 次任务`,
    value: u.user_id,
  })),
])

// ---- 任务列表（runs） ----
const runStatus = ref('全部')
const runType = ref('全部')
const runPage = ref(1)
const runs = ref<RunRow[]>([])
const runsNextOffset = ref<number | null>(null)
const selectedRunId = ref<string | null>(null)

const runsReq = useRequest()

const statusOptions: SelectOption[] = [
  { label: '全部', value: '全部' },
  { label: 'queued', value: 'queued' },
  { label: 'running', value: 'running' },
  { label: 'succeeded', value: 'succeeded' },
  { label: 'failed', value: 'failed' },
]

const typeOptions: SelectOption[] = [
  { label: '全部', value: '全部' },
  { label: 'answer', value: 'answer' },
  { label: 'daily_cn', value: 'daily_cn' },
  { label: 'daily_us', value: 'daily_us' },
  { label: 'daily (旧)', value: 'daily' },
  { label: 'weekly', value: 'weekly' },
]

async function loadRuns(resetPage = true): Promise<void> {
  if (resetPage) runPage.value = 1
  const filters: RunFilters = { offset: (runPage.value - 1) * 30, limit: 30 }
  if (selectedUserValue.value !== ALL) filters.user_id = selectedUserValue.value
  if (runStatus.value !== '全部') filters.status = runStatus.value
  if (runType.value !== '全部') filters.triggered_by = runType.value
  const data = await runsReq.run(() => fetchRuns(filters))
  if (!data) return
  runs.value = data.items
  runsNextOffset.value = data.next_offset
  if (selectedRunId.value && !data.items.some((r) => r.id === selectedRunId.value)) {
    selectedRunId.value = null
  }
}

watch([runStatus, runType], () => loadRuns(true))
watch(selectedUserValue, () => loadRuns(true))

const runColumns: DataTableColumns<RunRow> = [
  {
    title: '时间',
    key: 'created_at',
    width: 160,
    render: (row) => fmtDateTime(row.created_at),
  },
  {
    title: '用户',
    key: 'user_id',
    width: 100,
    render: (row) =>
      h('span', { title: row.user_id }, shortUuid(row.user_id)),
  },
  { title: '类型', key: 'triggered_by', width: 90 },
  {
    title: '状态',
    key: 'status',
    width: 110,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: statusTagType(row.status), bordered: false },
        { default: () => row.status },
      ),
  },
  {
    title: '模型',
    key: 'model',
    width: 180,
    render: (row) => row.usage?.model ?? '—',
  },
  {
    title: '输入 tokens',
    key: 'input_tokens',
    width: 110,
    render: (row) => fmtNumber(row.usage?.input_tokens),
  },
  {
    title: '输出 tokens',
    key: 'output_tokens',
    width: 110,
    render: (row) => fmtNumber(row.usage?.output_tokens),
  },
  {
    title: '任务 ID',
    key: 'id',
    width: 110,
    render: (row) => h('span', { title: row.id }, shortUuid(row.id)),
  },
]

function runRowProps(row: RunRow): HTMLAttributes {
  return {
    class: 'clickable-row',
    onClick: () => {
      selectedRunId.value = row.id
    },
  }
}

function runRowClass(row: RunRow): string {
  return row.id === selectedRunId.value ? 'row-selected' : ''
}

const runOptions = computed<SelectOption[]>(() =>
  runs.value.map((r) => ({
    label: `${fmtDateTime(r.created_at)} · ${r.triggered_by} · ${r.status} · ${shortUuid(r.id)}`,
    value: r.id,
  })),
)

// ---- 工具调用（tool-calls / 执行链路） ----
const spanPage = ref(1)
const spans = ref<ToolCallSpan[]>([])
const spansNextOffset = ref<number | null>(null)

const callsReq = useRequest()

async function loadSpans(resetPage = true): Promise<void> {
  if (resetPage) spanPage.value = 1
  if (!selectedRunId.value) {
    spans.value = []
    spansNextOffset.value = null
    return
  }
  const data = await callsReq.run(() =>
    fetchToolCalls(selectedRunId.value as string, (spanPage.value - 1) * 200, 200),
  )
  if (!data) return
  spans.value = data.items
  spansNextOffset.value = data.next_offset
}

watch(selectedRunId, () => loadSpans(true))

const selectedRun = computed<RunRow | null>(
  () => runs.value.find((r) => r.id === selectedRunId.value) ?? null,
)

interface SpanMeta {
  leftPct: number
  widthPct: number
  offsetSec: number
  durationSec: number
  totalSec: number
  color: string
}

const spanMeta = computed<SpanMeta[]>(() => {
  const list = spans.value
  if (list.length === 0) return []
  const starts = list.map((s) => parseIsoMs(s.created_at))
  const ends = list.map((s) => parseIsoMs(s.finished_at ?? s.created_at))
  const start = Math.min(...starts)
  const end = Math.max(...ends)
  const total = Math.max(end - start, 1)
  return list.map((s) => {
    const offset = Math.max(0, parseIsoMs(s.created_at) - start)
    const duration = s.duration_ms ?? 0
    const leftPct = Math.min(100, Math.max(0, (offset / total) * 100))
    const widthPct = Math.min(100 - leftPct, Math.max(0.3, (duration / total) * 100))
    return {
      leftPct,
      widthPct,
      offsetSec: offset / 1000,
      durationSec: duration / 1000,
      totalSec: total / 1000,
      color: spanBarColor(s.status),
    }
  })
})

function spanTitle(span: ToolCallSpan): string {
  const symbol = span.tool_name === 'llm' ? '◆ 模型' : '↳ 工具'
  const dur = ((span.duration_ms ?? 0) / 1000).toFixed(2)
  return `${symbol} · 第 ${span.round} 轮 · ${span.tool_name} · ${span.status} · ${dur}s`
}

function spanInput(span: ToolCallSpan): unknown {
  if (span.content && !span.content.truncated && span.content.input !== undefined) {
    return span.content.input
  }
  return span.arguments ?? {}
}

onMounted(() => loadTraceUsers(true))
</script>

<template>
  <div class="page">
    <n-space vertical size="large">
      <div class="page-head">
        <h1>AI 调用轨迹</h1>
      </div>

      <n-card title="轨迹用户">
        <n-space align="center" :size="8" :wrap="false">
          <n-input
            v-model:value="userQuery"
            placeholder="搜索用户 ID 或 Sandbox 账户"
            clearable
            style="max-width: 420px; width: 100%"
            @keyup.enter="loadTraceUsers(true)"
          />
          <n-button type="primary" :loading="usersReq.loading" @click="loadTraceUsers(true)">
            搜索
          </n-button>
          <n-button :disabled="userPage <= 1" @click="userPage--; loadTraceUsers(false)">
            上一页
          </n-button>
          <n-button
            :disabled="userNextOffset === null"
            @click="userPage++; loadTraceUsers(false)"
          >
            下一页
          </n-button>
        </n-space>
        <div class="pa-muted" style="margin: 8px 0">
          第 {{ userPage }} 页 · 每页最多 100 人<template v-if="userNextOffset !== null">
            · 还有更多用户，可切换用户页或搜索
          </template>
        </div>
        <n-alert
          v-if="usersReq.error"
          type="error"
          :bordered="false"
          closable
          @close="usersReq.clearError"
          style="margin-bottom: 12px"
        >
          {{ usersReq.error }}
        </n-alert>
        <div class="pa-muted" style="margin-bottom: 4px">选择用户（全部用户 = 不过滤）</div>
        <n-select v-model:value="selectedUserValue" :options="userOptions" />
        <n-empty
          v-if="traceUsers.length === 0 && !usersReq.loading && !usersReq.error"
          description="没有匹配的轨迹用户。"
          style="margin-top: 12px"
        />
      </n-card>

      <n-card title="任务列表">
        <n-space align="center" :size="8" :wrap="false" style="margin-bottom: 8px">
          <n-select
            v-model:value="runStatus"
            :options="statusOptions"
            placeholder="任务状态"
            style="width: 140px"
          />
          <n-select
            v-model:value="runType"
            :options="typeOptions"
            placeholder="任务类型"
            style="width: 140px"
          />
          <n-button :disabled="runPage <= 1" @click="runPage--; loadRuns(false)">
            上一页
          </n-button>
          <n-button :disabled="runsNextOffset === null" @click="runPage++; loadRuns(false)">
            下一页
          </n-button>
        </n-space>
        <div class="pa-muted" style="margin-bottom: 8px">
          第 {{ runPage }} 页 · 每页最多 30 条<template v-if="runsNextOffset !== null">
            · 还有更多任务，可切换任务页
          </template>
          · 点击表格行查看执行链路
        </div>
        <n-alert
          v-if="runsReq.error"
          type="error"
          :bordered="false"
          closable
          @close="runsReq.clearError"
          style="margin-bottom: 12px"
        >
          {{ runsReq.error }}
        </n-alert>
        <n-spin :show="runsReq.loading">
          <n-data-table
            :columns="runColumns"
            :data="runs"
            :bordered="false"
            size="small"
            :scroll-x="1000"
            :row-props="runRowProps"
            :row-class-name="runRowClass"
          />
          <n-empty
            v-if="runs.length === 0 && !runsReq.loading && !runsReq.error"
            description="该筛选条件下没有任务。"
            style="margin-top: 12px"
          />
        </n-spin>
      </n-card>

      <n-card v-if="selectedRun" title="执行链路">
        <n-space align="center" :size="8" :wrap="false" style="margin-bottom: 12px">
          <n-select
            v-model:value="selectedRunId"
            :options="runOptions"
            placeholder="查看任务"
            filterable
            style="max-width: 520px; width: 100%"
          />
          <n-button :disabled="spanPage <= 1" @click="spanPage--; loadSpans(false)">
            轨迹上一页
          </n-button>
          <n-button
            :disabled="spansNextOffset === null"
            @click="spanPage++; loadSpans(false)"
          >
            轨迹下一页
          </n-button>
        </n-space>

        <n-grid cols="2 s:2 m:4" :x-gap="12" :y-gap="12" responsive="screen">
          <n-grid-item>
            <n-card>
              <n-statistic label="状态" :value="selectedRun.status" />
            </n-card>
          </n-grid-item>
          <n-grid-item>
            <n-card>
              <n-statistic label="调用数" :value="spans.length" />
            </n-card>
          </n-grid-item>
          <n-grid-item>
            <n-card>
              <n-statistic label="输入 tokens" :value="fmtNumber(selectedRun.usage?.input_tokens)" />
            </n-card>
          </n-grid-item>
          <n-grid-item>
            <n-card>
              <n-statistic label="输出 tokens" :value="fmtNumber(selectedRun.usage?.output_tokens)" />
            </n-card>
          </n-grid-item>
        </n-grid>

        <div class="pa-muted" style="margin: 12px 0">
          用户 {{ selectedRun.user_id }} · 任务 {{ selectedRun.id }} · 尝试
          {{ selectedRun.attempt }} 次
        </div>

        <n-alert v-if="selectedRun.error_code" type="error" :bordered="false" style="margin-bottom: 12px">
          任务错误：{{ selectedRun.error_code }}
        </n-alert>
        <n-alert
          v-if="callsReq.error"
          type="error"
          :bordered="false"
          closable
          @close="callsReq.clearError"
          style="margin-bottom: 12px"
        >
          {{ callsReq.error }}
        </n-alert>

        <n-spin :show="callsReq.loading">
          <n-alert
            v-if="spans.length === 0 && !callsReq.loading && !callsReq.error"
            type="info"
            :bordered="false"
            style="margin-bottom: 12px"
          >
            该任务没有保存轨迹，可能尚未执行或轨迹已过期。
          </n-alert>

          <template v-if="spans.length > 0">
            <div class="pa-muted" style="margin-bottom: 8px">
              第 {{ spanPage }} 页（每页最多 200 条调用） · 链路跨度
              {{ (spanMeta[0]?.totalSec ?? 0).toFixed(2) }} 秒 · 依照记录时间排序，工具归入同一模型轮次
            </div>
            <n-collapse>
              <n-collapse-item v-for="(span, i) in spans" :key="span.id" :name="span.id">
                <template #header>{{ spanTitle(span) }}</template>
                <div class="tl-track">
                  <div
                    class="tl-bar"
                    :style="{
                      left: spanMeta[i].leftPct + '%',
                      width: spanMeta[i].widthPct + '%',
                      background: spanMeta[i].color,
                    }"
                  />
                </div>
                <div class="pa-muted" style="margin: 6px 0">
                  开始 +{{ spanMeta[i].offsetSec.toFixed(2) }}s · 耗时
                  {{ spanMeta[i].durationSec.toFixed(2) }}s / 链路
                  {{ spanMeta[i].totalSec.toFixed(2) }}s
                </div>
                <div class="pa-muted" style="margin-bottom: 10px">
                  调用 ID：{{ span.call_id }} · 开始：{{ span.created_at }} · 结束：{{
                    span.finished_at || '—'
                  }}
                </div>
                <n-grid cols="1 m:2" :x-gap="12" :y-gap="12">
                  <n-grid-item>
                    <div class="pa-muted" style="margin-bottom: 4px">
                      <strong>输入 / 参数</strong>
                    </div>
                    <JsonBlock :value="spanInput(span)" />
                  </n-grid-item>
                  <n-grid-item>
                    <div class="pa-muted" style="margin-bottom: 4px">
                      <strong>输出 / 统计</strong>
                    </div>
                    <JsonBlock :value="parseSummaryJson(span.result_summary)" />
                    <template
                      v-if="span.content && !span.content.truncated && span.content.output !== undefined"
                    >
                      <div class="pa-muted" style="margin: 8px 0 4px">
                        <strong>完整输出</strong>
                      </div>
                      <JsonBlock :value="span.content.output" />
                    </template>
                  </n-grid-item>
                </n-grid>
                <n-alert
                  v-if="span.content && span.content.truncated"
                  type="warning"
                  :bordered="false"
                  style="margin-top: 10px"
                >
                  内容超过保存上限，以下为截断预览。
                </n-alert>
                <pre v-if="span.content && span.content.truncated" class="json-pre" style="margin-top: 8px">{{ span.content.preview ?? '' }}</pre>
                <n-alert
                  v-else-if="span.content === null"
                  type="info"
                  :bordered="false"
                  style="margin-top: 10px"
                >
                  该轨迹未记录完整内容（可能创建于启用前）。
                </n-alert>
                <n-alert
                  v-if="span.error_code"
                  type="error"
                  :bordered="false"
                  style="margin-top: 10px"
                >
                  错误代码：{{ span.error_code }}
                </n-alert>
              </n-collapse-item>
            </n-collapse>
            <div v-if="spansNextOffset !== null" class="pa-muted" style="margin-top: 8px">
              还有更多调用，可切换轨迹页查看。
            </div>
          </template>
        </n-spin>

        <div class="pa-muted" style="margin-top: 12px">
          启用完整记录后，管理员可查看私有输入和工具结果；不记录模型推理。轨迹默认保留 30 天。
        </div>
      </n-card>
    </n-space>
  </div>
</template>
