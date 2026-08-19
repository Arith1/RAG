<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api/client'
import { useAuthStore } from '../stores/auth'

interface DocItem {
  id: number
  file_name: string
  version: string
  source: string
  chunk_count: number
  sync_status: 'pending' | 'in_sync' | 'failed'
}

interface QueueStats {
  enabled: boolean
  stream_len: number
  pending: number
  dead_letter: number
  inflight: number
  [k: string]: unknown
}

const auth = useAuthStore()
const docs = ref<DocItem[]>([])
const stats = ref<QueueStats | null>(null)
const error = ref('')
const loading = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    docs.value = await api<DocItem[]>('/api/documents')
    stats.value = await api<QueueStats>('/api/ingest/stats').catch(() => null)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function upload(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  const form = new FormData()
  form.append('file', file)
  try {
    const data = await api<{ message: string }>('/api/documents/upload', {
      method: 'POST',
      body: form,
    })
    alert(data.message)
    ;(e.target as HTMLInputElement).value = ''
    setTimeout(load, 1500) // 轮询等入库
  } catch (err) {
    alert(err instanceof Error ? err.message : String(err))
  }
}

async function remove(d: DocItem) {
  if (!confirm(`确认删除 ${d.file_name}？`)) return
  try {
    await api(`/api/documents/${d.id}`, { method: 'DELETE' })
    await load()
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e))
  }
}

onMounted(load)
</script>

<template>
  <div class="docs">
    <div class="head">
      <h2>已入库文档</h2>
      <div class="actions">
        <span v-if="stats" class="stats">
          队列: stream {{ stats.stream_len }} · 待处理 {{ stats.pending }} · 死信 {{ stats.dead_letter }}
        </span>
        <label v-if="auth.isAdmin" class="upload">
          上传文档
          <input type="file" accept=".md,.txt,.docx,.pdf" @change="upload" />
        </label>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>

    <table v-if="docs.length">
      <thead>
        <tr><th>文件名</th><th>版本</th><th>chunks</th><th>状态</th><th v-if="auth.isAdmin"></th></tr>
      </thead>
      <tbody>
        <tr v-for="d in docs" :key="d.id">
          <td>{{ d.file_name }}</td>
          <td>v{{ d.version }}</td>
          <td>{{ d.chunk_count }}</td>
          <td><span :class="['badge', d.sync_status]">{{ d.sync_status }}</span></td>
          <td v-if="auth.isAdmin">
            <button class="del" :disabled="d.sync_status === 'pending'" @click="remove(d)">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else-if="!loading" class="empty">知识库暂无文档</p>
  </div>
</template>

<style scoped>
.head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.actions { display: flex; align-items: center; gap: 16px; }
.stats { font-size: 12px; color: #86909c; }
.upload {
  background: #4e6ef2; color: #fff; padding: 6px 14px; border-radius: 6px;
  cursor: pointer; font-size: 13px;
}
.upload input { display: none; }
table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid #f0f1f3; font-size: 14px; }
th { background: #fafbfc; color: #86909c; font-weight: 500; }
.badge { padding: 1px 8px; border-radius: 10px; font-size: 12px; }
.badge.in_sync { background: #e8f7ee; color: #00b42a; }
.badge.pending { background: #fff7e8; color: #ff7d00; }
.badge.failed { background: #ffece8; color: #f53f3f; }
.del { border: 1px solid #f53f3f; color: #f53f3f; background: #fff; border-radius: 5px; padding: 2px 10px; cursor: pointer; }
.del:disabled { opacity: 0.4; cursor: not-allowed; }
.err { color: #f53f3f; margin-bottom: 12px; }
.empty { color: #c9cdd4; text-align: center; padding: 40px; }
</style>
