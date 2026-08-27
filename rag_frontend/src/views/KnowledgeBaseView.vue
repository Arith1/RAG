<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, type DocItem } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useFeedback } from '../composables/feedback'
import StatusBadge from '../components/StatusBadge.vue'

const auth = useAuthStore()
const feedback = useFeedback()
const docs = ref<DocItem[]>([])
const loading = ref(false)
const error = ref('')
const query = ref('')
const page = ref(1)
const pageSize = 12

type SortKey = 'uploaded' | 'updated' | 'downloads' | 'chunks'
const sortOptions: { value: SortKey; label: string }[] = [
  { value: 'uploaded', label: '最近上传' },
  { value: 'updated', label: '最近更新' },
  { value: 'downloads', label: '下载最多' },
  { value: 'chunks', label: 'chunk数最多' },
]
const sort = ref<SortKey>('uploaded')
const sortOpen = ref(false)
const sortWrap = ref<HTMLElement | null>(null)

const isLoggedIn = computed(() => auth.isLoggedIn)
const currentSortLabel = computed(
  () => sortOptions.find((o) => o.value === sort.value)?.label ?? '最近上传',
)

const publicDocs = computed(() => docs.value.filter((d) => d.is_public))

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  let list = publicDocs.value
  if (q) {
    list = list.filter(
      (d) =>
        (d.file_name || '').toLowerCase().includes(q) ||
        (d.source || '').toLowerCase().includes(q),
    )
  }
  const arr = [...list]
  if (sort.value === 'downloads') {
    arr.sort((a, b) => (b.download_count ?? 0) - (a.download_count ?? 0))
  } else if (sort.value === 'updated') {
    arr.sort((a, b) => {
      if (a.updated_at && b.updated_at) return b.updated_at.localeCompare(a.updated_at)
      return (b.id ?? 0) - (a.id ?? 0)
    })
  } else if (sort.value === 'chunks') {
    arr.sort((a, b) => (b.chunk_count ?? 0) - (a.chunk_count ?? 0) || (b.id ?? 0) - (a.id ?? 0))
  } else {
    // 最近上传：后端未返回创建时间，按 id（自增）倒序即上传先后
    arr.sort((a, b) => (b.id ?? 0) - (a.id ?? 0))
  }
  return arr
})

const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)))
const pageData = computed(() => {
  const start = (page.value - 1) * pageSize
  return filtered.value.slice(start, start + pageSize)
})

// 搜索词或排序方式变化时回到第一页
watch([query, sort], () => {
  page.value = 1
})

function selectSort(key: SortKey) {
  sort.value = key
  sortOpen.value = false
}

function onClickOutside(e: MouseEvent) {
  if (sortOpen.value && sortWrap.value && !sortWrap.value.contains(e.target as Node)) {
    sortOpen.value = false
  }
}

function fmtTime(iso?: string) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function download(d: DocItem) {
  const token = auth.token
  fetch(`/api/documents/${d.id}/download`, {
    headers: { Authorization: `Bearer ${token}` },
  })
    .then(async (res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const ct = res.headers.get('content-type') ?? ''
      if (ct.includes('application/json')) {
        const data = (await res.json()) as { url?: string }
        if (data.url) {
          window.open(data.url, '_blank', 'noopener')
          return
        }
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = d.file_name
      a.click()
      URL.revokeObjectURL(url)
    })
    .catch((e) => {
      feedback.notify(e instanceof Error ? e.message : String(e), 'error')
    })
}

async function revoke(d: DocItem) {
  const ok = await feedback.confirm({
    message: '确认设为私有？',
    detail: `确认将「${d.file_name}」设为私有？该文档将不再对其他人公开，仅上传者可见。`,
    confirmText: '设为私有',
    danger: true,
  })
  if (!ok) return
  try {
    await api(`/api/documents/${d.id}/revoke`, { method: 'POST' })
    await load()
    feedback.notify('已设为私有', 'success')
  } catch (e) {
    feedback.notify(e instanceof Error ? e.message : String(e), 'error')
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await api<{ total: number; items: DocItem[] }>('/api/documents?limit=500')
    docs.value = data.items
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  document.addEventListener('click', onClickOutside)
  if (isLoggedIn.value) load()
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onClickOutside)
})
</script>

<template>
  <div class="kb-page">
    <div class="kb-inner">
      <div v-if="!isLoggedIn" class="guest">
        <div class="guest-card">
          <h2>登录后浏览共享知识库</h2>
          <p>这里汇集所有用户公开共享的文档，可搜索、按最近上传 / 最近更新 / 下载量 / chunk 数排序并下载。</p>
          <RouterLink to="/login" class="btn btn-primary">去登录</RouterLink>
        </div>
      </div>

      <template v-else>
        <header class="kb-head">
          <h1>知识库</h1>
          <p class="sub">浏览所有用户共享的文档，支持关键词搜索，并按最近上传 / 最近更新 / 下载量 / chunk 数排序。</p>
        </header>

        <p v-if="error" class="error-banner" role="alert">{{ error }}</p>

        <div class="kb-toolbar">
          <div class="kb-search">
            <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">
              <path d="M8.5 4a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9ZM14 14l3 3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            <input v-model="query" class="kb-search-input" type="search" placeholder="搜索文档名 / 来源路径" />
          </div>

          <div ref="sortWrap" class="kb-sort">
            <button
              class="btn btn-subtle kb-sort-btn"
              :class="{ open: sortOpen }"
              aria-haspopup="listbox"
              :aria-expanded="sortOpen"
              @click.stop="sortOpen = !sortOpen"
            >
              <span class="kb-sort-label">排序：</span>{{ currentSortLabel }}
              <svg viewBox="0 0 20 20" width="14" height="14" class="kb-sort-chevron" :class="{ up: sortOpen }" aria-hidden="true">
                <path d="M5 8l5 5 5-5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </button>
            <Transition name="drop">
              <div v-if="sortOpen" class="kb-sort-menu" role="listbox" aria-label="排序方式">
                <button
                  v-for="opt in sortOptions"
                  :key="opt.value"
                  class="kb-sort-option"
                  :class="{ selected: sort === opt.value }"
                  role="option"
                  :aria-selected="sort === opt.value"
                  @click="selectSort(opt.value)"
                >
                  <span>{{ opt.label }}</span>
                  <svg v-if="sort === opt.value" viewBox="0 0 20 20" width="15" height="15" aria-hidden="true">
                    <path d="M4.5 10.5l3.5 3.5 7-8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                </button>
              </div>
            </Transition>
          </div>

          <span class="kb-count">{{ filtered.length }} 个文档</span>
        </div>

        <div class="kb-list">
          <article v-for="d in pageData" :key="d.id" class="kb-item">
            <div class="kb-item-main">
              <div class="kb-item-title">
                <span class="kb-file-icon">
                  <svg viewBox="0 0 20 20" width="15" height="15" aria-hidden="true">
                    <path d="M4 3.5h7L15 7.5v9H4v-13Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" />
                    <path d="M11 3.5v4h4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" />
                  </svg>
                </span>
                <span class="kb-name" :title="d.file_name">{{ d.file_name }}</span>
                <StatusBadge v-if="d.sync_status === 'failed'" status="failed" />
              </div>
              <div class="kb-meta">
                <span v-if="d.source" class="kb-source" :title="d.source">{{ d.source }}</span>
                <RouterLink v-if="d.owner_id" :to="`/profile/${d.owner_id}`" class="kb-owner" :title="`查看该用户个人详情`">上传者 #{{ d.owner_id }}</RouterLink>
                <span v-else class="kb-owner">上传者 —</span>
                <span>{{ d.chunk_count ?? 0 }} 个分块</span>
              </div>
            </div>
            <div class="kb-item-side">
              <span class="kb-stats">
                下载 {{ d.download_count ?? 0 }} 次
                <template v-if="d.updated_at"> · {{ fmtTime(d.updated_at) }}</template>
              </span>
              <button class="btn btn-subtle kb-download" @click="download(d)">
                <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true">
                  <path d="M10 3.5v8m0 0l-3-3m3 3l3-3M4.5 14.5h11" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
                下载
              </button>
              <button v-if="auth.isAdmin" class="btn btn-ghost kb-revoke" title="管理员审核：设为私有" @click="revoke(d)">
                <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true">
                  <path d="M5 8V7a5 5 0 0 1 10 0v1m1 0v9H4V8h12Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" />
                </svg>
                设为私有
              </button>
            </div>
          </article>

          <div v-if="!loading && !filtered.length" class="kb-empty">
            <div class="kb-empty-mark">
              <svg viewBox="0 0 64 64" width="38" height="38" aria-hidden="true">
                <rect width="64" height="64" rx="16" fill="var(--accent-soft)" />
                <path d="M32 15c-4-2.6-8.8-4-14-4v30c5.2 0 10 1.4 14 4 4-2.6 8.8-4 14-4V11c-5.2 0-10 1.4-14 4Z" fill="none" stroke="var(--accent)" stroke-width="3.4" stroke-linejoin="round" />
                <path d="M32 15v30" stroke="var(--accent)" stroke-width="3.4" stroke-linecap="round" />
              </svg>
            </div>
            <h3>{{ query ? '没有匹配的文档' : '还没有共享文档' }}</h3>
            <p>{{ query ? '换个关键词再试试。' : '当有用户把文档设为「共享」后，会出现在这里。' }}</p>
          </div>

          <div v-if="filtered.length > pageSize" class="kb-pager">
            <button class="btn btn-ghost kb-pager-btn" :disabled="page <= 1" @click="page--">上一页</button>
            <span class="kb-pager-info">第 {{ page }} / {{ totalPages }} 页</span>
            <button class="btn btn-ghost kb-pager-btn" :disabled="page >= totalPages" @click="page++">下一页</button>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.kb-page { flex: 1; display: flex; justify-content: center; padding: 0 var(--page-pad); }
.kb-inner { width: 100%; max-width: 1000px; }

.guest { display: flex; justify-content: center; padding: 80px 20px; }
.guest-card { max-width: 420px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); box-shadow: var(--shadow-sm); padding: 34px 30px; text-align: center; }
.guest-card h2 { margin: 0 0 10px; font-size: 20px; font-weight: 650; }
.guest-card p { margin: 0 0 22px; color: var(--text-2); font-size: 14px; line-height: 1.7; }

.kb-head { padding: 26px 0 6px; }
.kb-head h1 { margin: 0; font-size: 21px; font-weight: 650; letter-spacing: -0.01em; }
.kb-head .sub { margin: 6px 0 0; color: var(--text-2); font-size: 13.5px; }

.error-banner {
  color: var(--err);
  font-size: 13.5px;
  background: var(--err-soft);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  margin: 14px 0;
  text-align: left;
}

.kb-toolbar { display: flex; align-items: center; gap: 12px; margin: 14px 0 16px; flex-wrap: wrap; }
.kb-search { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 220px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0 12px; color: var(--text-3); transition: border-color 0.16s ease, box-shadow 0.16s ease; }
.kb-search:focus-within { border-color: var(--accent); box-shadow: var(--shadow-focus); }
.kb-search-input { flex: 1; border: none; background: transparent; outline: none; padding: 9px 0; font-size: 14px; color: var(--text); }
.kb-search-input::placeholder { color: var(--text-3); }

.kb-sort { position: relative; flex: none; }
.kb-sort-btn { padding: 8px 12px; font-size: 13.5px; color: var(--text-2); }
.kb-sort-btn:hover:not(:disabled), .kb-sort-btn.open { color: var(--text); border-color: var(--border-strong); background: var(--surface-2); }
.kb-sort-label { color: var(--text-3); font-weight: 450; }
.kb-sort-chevron { color: var(--text-3); transition: transform 0.16s ease; }
.kb-sort-chevron.up { transform: rotate(180deg); }

.kb-sort-menu {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  min-width: 176px;
  padding: 5px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-md);
  z-index: 30;
}
.kb-sort-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  width: 100%;
  padding: 8px 11px;
  font-size: 13.5px;
  color: var(--text-2);
  border-radius: var(--radius-sm);
  text-align: left;
  transition: background 0.14s ease, color 0.14s ease;
}
.kb-sort-option:hover { background: var(--surface-3); color: var(--text); }
.kb-sort-option.selected { color: var(--accent); font-weight: 500; }

.drop-enter-active, .drop-leave-active { transition: opacity 0.14s ease, transform 0.14s ease; }
.drop-enter-from, .drop-leave-to { opacity: 0; transform: translateY(-4px); }

.kb-count { font-size: 12.5px; color: var(--text-3); white-space: nowrap; }

.kb-list { display: flex; flex-direction: column; gap: 10px; }
.kb-item { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 14px 16px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md); box-shadow: var(--shadow-xs); transition: border-color 0.16s ease, box-shadow 0.16s ease; }
.kb-item:hover { border-color: var(--border-strong); box-shadow: var(--shadow-sm); }
.kb-item-main { min-width: 0; }
.kb-item-title { display: flex; align-items: center; gap: 9px; }
.kb-file-icon { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; border-radius: 7px; background: var(--accent-soft); color: var(--accent); flex: none; }
.kb-name { font-size: 14.5px; font-weight: 500; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kb-meta { display: flex; align-items: center; gap: 10px; margin-top: 7px; font-size: 12px; color: var(--text-3); flex-wrap: wrap; }
.kb-source { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 360px; }
.kb-owner { color: var(--text-2); }
.kb-owner:hover { color: var(--accent); text-decoration: underline; }
.kb-item-side { display: flex; align-items: center; gap: 14px; flex: none; }
.kb-stats { font-size: 12px; color: var(--text-3); white-space: nowrap; }

.kb-empty { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 64px 20px; text-align: center; }
.kb-empty-mark { width: 60px; height: 60px; border-radius: 18px; display: flex; align-items: center; justify-content: center; margin-bottom: 16px; }
.kb-empty h3 { margin: 0 0 6px; font-size: 18px; font-weight: 650; }
.kb-empty p { margin: 0; color: var(--text-2); font-size: 14px; }

.kb-pager { display: flex; align-items: center; justify-content: center; gap: 14px; padding: 20px 0 6px; }
.kb-pager-btn { min-width: 76px; justify-content: center; }
.kb-pager-info { font-size: 12.5px; color: var(--text-3); white-space: nowrap; }

@media (max-width: 640px) {
  .kb-item { flex-direction: column; align-items: stretch; gap: 10px; }
  .kb-item-side { justify-content: space-between; }
  .kb-source { max-width: 100%; }
}
</style>
