<script setup lang="ts">
import type { SyncStatus } from '../api/client'

const props = defineProps<{ status: SyncStatus }>()

const map: Record<SyncStatus, { label: string; cls: string }> = {
  in_sync: { label: '已入库', cls: 'ok' },
  pending: { label: '入库中', cls: 'warn' },
  failed: { label: '失败', cls: 'err' },
}
</script>

<template>
  <span class="badge" :class="map[props.status].cls">
    <span class="dot" aria-hidden="true"></span>
    {{ map[props.status].label }}
  </span>
</template>
