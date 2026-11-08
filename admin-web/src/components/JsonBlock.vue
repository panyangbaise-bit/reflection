<script setup lang="ts">
import { computed } from 'vue'
import { jsonText } from '../utils/format'

const props = defineProps<{ value: unknown }>()

const text = computed(() => jsonText(props.value))
</script>

<template>
  <pre class="json-pre">{{ text }}</pre>
</template>
