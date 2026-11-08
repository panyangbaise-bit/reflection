<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import {
  NButton,
  NInput,
  NLayout,
  NLayoutContent,
  NLayoutHeader,
  NLayoutSider,
  NMenu,
  NSpace,
  NText,
  useMessage,
  type MenuOption,
} from 'naive-ui'
import { adminToken, clearAdminToken, setAdminToken } from '../stores/token'

const route = useRoute()
const router = useRouter()
const message = useMessage()

const menuOptions: MenuOption[] = [
  { label: '📊 运行概览', key: 'overview' },
  { label: '📄 报告查看', key: 'reports' },
  { label: '⚡ 触发报告', key: 'trigger' },
  { label: '🗂 任务状态', key: 'jobs' },
  { label: '🧭 AI 调用轨迹', key: 'traces' },
]

const activeKey = computed(() => String(route.name ?? 'overview'))

function onMenuSelect(key: string): void {
  void router.push({ name: key })
}

const draft = ref(adminToken.value)
watch(adminToken, (value) => {
  draft.value = value
})

function applyToken(): void {
  const value = draft.value.trim()
  if (!value) {
    message.warning('令牌不能为空')
    return
  }
  setAdminToken(value)
  message.success('令牌已保存')
}

function clearToken(): void {
  clearAdminToken()
  message.info('令牌已清除')
}
</script>

<template>
  <n-layout has-sider position="absolute" style="top: 0; bottom: 0; left: 0; right: 0">
    <n-layout-sider bordered collapse-mode="width" :width="220" :collapsed-width="64" show-trigger>
      <div class="brand">
        <span class="brand-icon">📋</span>
        <span class="brand-text">复盘了 · 后台管理</span>
      </div>
      <n-menu
        :value="activeKey"
        :options="menuOptions"
        :collapsed-width="64"
        :collapsed-icon-size="20"
        @update:value="onMenuSelect"
      />
    </n-layout-sider>
    <n-layout>
      <n-layout-header bordered class="header">
        <n-text class="header-env">环境：fupanle.api · 测试服务器</n-text>
        <n-space align="center" :size="8">
          <n-input
            v-model:value="draft"
            type="password"
            show-password-on="click"
            placeholder="管理员令牌"
            class="token-input"
            @keyup.enter="applyToken"
          />
          <n-button size="small" type="primary" @click="applyToken">保存</n-button>
          <n-button size="small" @click="clearToken">清除</n-button>
        </n-space>
      </n-layout-header>
      <n-layout-content :native-scrollbar="true">
        <router-view />
      </n-layout-content>
    </n-layout>
  </n-layout>
</template>

<style scoped>
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 56px;
  padding: 0 16px;
  font-weight: 700;
  white-space: nowrap;
  overflow: hidden;
}

.brand-icon {
  font-size: 20px;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  height: 56px;
}

.header-env {
  color: #697386;
  font-size: 13px;
}

.token-input {
  width: 240px;
}
</style>
