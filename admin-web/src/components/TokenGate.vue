<script setup lang="ts">
import { ref } from 'vue'
import { NButton, NCard, NInput, NSpace, NText } from 'naive-ui'
import { setAdminToken } from '../stores/token'

const draft = ref('')

function enter(): void {
  const value = draft.value.trim()
  if (!value) return
  setAdminToken(value)
}
</script>

<template>
  <div class="gate">
    <n-card class="gate-card" title="📋 复盘了 · 后台管理">
      <n-space vertical size="large">
        <n-text depth="3">
          请输入独立管理员令牌（secrets 目录中的 PA_ADMIN_CREDENTIALS）。
          普通用户登录凭据不能访问后台。
        </n-text>
        <n-input
          v-model:value="draft"
          type="password"
          show-password-on="click"
          placeholder="粘贴管理员令牌"
          size="large"
          @keyup.enter="enter"
        />
        <n-button type="primary" block size="large" :disabled="!draft.trim()" @click="enter">
          进入后台
        </n-button>
        <n-text depth="3" class="pa-muted">环境：fupanle.api · 测试服务器</n-text>
      </n-space>
    </n-card>
  </div>
</template>

<style scoped>
.gate {
  min-height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f5f7fa;
  padding: 24px;
}

.gate-card {
  width: 420px;
  max-width: 100%;
}
</style>
