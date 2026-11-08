<script setup lang="ts">
import { computed } from 'vue'
import { NConfigProvider, NDialogProvider, NMessageProvider, dateZhCN, zhCN } from 'naive-ui'
import AppLayout from './components/AppLayout.vue'
import TokenGate from './components/TokenGate.vue'
import { adminToken } from './stores/token'

const hasToken = computed(() => adminToken.value.length > 0)
</script>

<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN">
    <n-message-provider>
      <n-dialog-provider>
        <TokenGate v-if="!hasToken" />
        <AppLayout v-else />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>
