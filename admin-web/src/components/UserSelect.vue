<script setup lang="ts">
import { computed } from 'vue'
import { NSelect, type SelectOption } from 'naive-ui'
import type { AdminUser } from '../types'

const props = defineProps<{
  users: AdminUser[]
  modelValue: string | null
  placeholder?: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string | null): void
}>()

const options = computed<SelectOption[]>(() =>
  props.users.map((u) => ({
    label: `${u.phone_masked || '无手机号'} · ${u.id.slice(0, 8)} · 盘后${
      u.daily_today ? '✓' : '—'
    } / 周报${u.weekly_today ? '✓' : '—'}`,
    value: u.id,
  })),
)
</script>

<template>
  <n-select
    :value="modelValue"
    :options="options"
    :placeholder="placeholder ?? '选择用户'"
    filterable
    @update:value="emit('update:modelValue', $event)"
  />
</template>
