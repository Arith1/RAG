<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  api,
  changePassword,
  deleteAccount,
  getUserProfile,
  listChatSessions,
  type ChatSessionInfo,
  type DocItem,
  type ProfileInfo,
} from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useFeedback } from '../composables/feedback'
import StatusBadge from '../components/StatusBadge.vue'

const auth = useAuthStore()
const feedback = useFeedback()
const route = useRoute()
const router = useRouter()

const profile = ref<ProfileInfo | null>(null)
const profileLoading = ref(true)
const error = ref('')

const docs = ref<DocItem[]>([])
const docsLoading = ref(false)
const sessions = ref<ChatSessionInfo[]>([])

// 公开文档列表分页
const page = ref(1)
const pageSize = 8

// 修改密码表单
const cpOld = ref('')
const cpNew = ref('')
const cpConfirm = ref('')
const cpLoading = ref(false)
const cpError = ref('')
const cpDone = ref('')

const deleting = ref(false)

const targetId = computed(() => Number(route.params.userId) || auth.user?.id || 0)
const isSelf = computed(() => !!profile.value && profile.value.is_self)

const publicDocs = computed(() =>
  docs.value.filter((d) => d.owner_id === targetId.value && d.is_public),
)
const ownDocs = computed(() => docs.value.filter((d) => d.owner_id === targetId.value))

// 数据概览：仅本人可见私有文档数；下载量为公开文档下载量之和
const publicCount = computed(() => publicDocs.value.length)
const privateCount = computed(() => ownDocs.value.filter((d) => !d.is_public).length)
const totalDownloads = computed(() =>
  publicDocs.value.reduce((sum, d) => sum + (d.download_count ?? 0), 0),
)

// 近期动态：最近上传只显示公开文档 + 最近会话（仅本人）
const recentUploads = computed(() => {
  const list = publicDocs.value
  return [...list]
    .sort((a, b) => (b.id ?? 0) - (a.id ?? 0))
    .slice(0, 5)
})
const recentSessions = computed(() => [...sessions.value].slice(0, 5))

const totalPages = computed(() => Math.max(1, Math.ceil(publicDocs.value.length / pageSize)))
const pageData = computed(() => {
  const start = (page.value - 1) * pageSize
  return publicDocs.value.slice(start, start + pageSize)
})

watch(
  () => route.params.userId,
  () => {
    page.value = 1
    loadProfile()
  },
)

function fmtDate(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' })
}

function fmtTime(iso?: string | null): string {
  if (!iso) return ''
  const t = new Date(iso.replace(' ', 'T'))
  if (Number.isNaN(t.getTime())) return ''
  const diff = Date.now() - t.getTime()
  const min = Math.floor(diff / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr} 小时前`
  const day = Math.floor(hr / 24)
  if (day < 7) return `${day} 天前`
  const y = t.getFullYear()
  const m = String(t.getMonth() + 1).padStart(2, '0')
  const d = String(t.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

async function loadDocs() {
  if (!profile.value) return
  docsLoading.value = true
  try {
    const data = await api<{ total: number; items: DocItem[] }>('/api/documents?limit=500')
    docs.value = data.items
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    docsLoading.value = false
  }
}

async function loadSessions() {
  if (!isSelf.value) return
  try {
    sessions.value = await listChatSessions()
  } catch {
    sessions.value = []
  }
}

async function loadProfile() {
  profileLoading.value = true
  error.value = ''
  profile.value = null
  docs.value = []
  try {
    const data = await getUserProfile(targetId.value)
    profile.value = data
    page.value = 1
    await Promise.all([loadDocs(), loadSessions()])
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    profileLoading.value = false
  }
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

function openSession(id: string) {
  router.push({ path: '/', query: { session: id } })
}

function goAskOwner() {
  // 一键检索该用户公开文档：跳问答页并预置「指定用户的公开文档」
  router.push({ path: '/', query: { owner_id: String(targetId.value) } })
}

function goAskOwn() {
  // 一键检索自己的文档：跳问答页并预置仅勾选「自己的私有/公开文档」
  router.push({ path: '/', query: { own: '1' } })
}

function goBack() {
  if (window.history.length > 1) router.back()
  else router.push('/')
}

function logout() {
  auth.logout()
  router.push('/')
}

async function changePwd() {
  cpError.value = ''
  cpDone.value = ''
  if (!cpOld.value || !cpNew.value || !cpConfirm.value) {
    cpError.value = '请填写完整'
    return
  }
  if (cpNew.value.length < 6) {
    cpError.value = '新密码至少 6 位'
    return
  }
  if (cpNew.value !== cpConfirm.value) {
    cpError.value = '两次输入的新密码不一致'
    return
  }
  cpLoading.value = true
  try {
    const res = await changePassword(cpOld.value, cpNew.value)
    cpDone.value = res.message || '密码修改成功，请重新登录'
    cpOld.value = ''
    cpNew.value = ''
    cpConfirm.value = ''
    feedback.notify('密码已修改，请重新登录', 'success')
    window.setTimeout(() => {
      auth.logout()
      router.push('/login')
    }, 900)
  } catch (e) {
    cpError.value = e instanceof Error ? e.message : String(e)
  } finally {
    cpLoading.value = false
  }
}

async function requestDeleteAccount() {
  const ok = await feedback.confirm({
    message: '确认删除账号？',
    detail: '账号将被标记为已删除，无法再登录；文档与计费记录均保留，此操作不可撤销。',
    confirmText: '确认删除',
    danger: true,
  })
  if (!ok) return
  deleting.value = true
  try {
    const res = await deleteAccount()
    feedback.notify(res.message || '已提交账号删除请求', 'success')
    auth.logout()
    router.push('/login')
  } catch (e) {
    feedback.notify(e instanceof Error ? e.message : String(e), 'error')
  } finally {
    deleting.value = false
  }
}

onMounted(loadProfile)
</script>

<template>
  <div class="profile-page">
    <div class="profile-inner">
      <!-- 页头 -->
      <header class="pf-head">
        <div>
          <h1>个人详情</h1>
          <p class="sub">
            {{ isSelf ? '你的账号信息、数据概览与公开文档。' : profile ? `${profile.username} 的公开信息。` : '' }}
          </p>
        </div>
        <button class="btn btn-ghost back-btn" @click="goBack">← 返回</button>
      </header>

      <p v-if="error" class="error-banner" role="alert">
        {{ error }}
        <RouterLink v-if="error.includes('用户不存在')" to="/" class="error-link">回到问答首页</RouterLink>
      </p>

      <template v-if="profileLoading">
        <div class="loading-block">加载中…</div>
      </template>

      <template v-else-if="profile">
        <!-- 账号信息卡 -->
        <section class="card account-card">
          <div class="avatar-xl">{{ profile.username.slice(0, 1).toUpperCase() }}</div>
          <div class="account-main">
            <div class="account-name-row">
              <h2>{{ profile.username }}</h2>
              <span class="role" :class="{ admin: profile.role === 'admin' }">
                {{ profile.role === 'admin' ? '管理员' : '普通用户' }}
              </span>
              <span v-if="isSelf" class="me-tag">我</span>
            </div>
            <p class="account-meta">注册于 {{ fmtDate(profile.created_at) }}</p>
          </div>
          <div class="account-actions">
            <button v-if="!isSelf" class="btn btn-primary" @click="goAskOwner">
              <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true">
                <path d="M3 8h10M8 3v10" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
              </svg>
              检索 TA 的共享文档
            </button>
            <template v-else>
              <button class="btn btn-subtle" @click="goAskOwn">检索我的文档</button>
              <button class="btn btn-subtle" @click="logout">退出登录</button>
            </template>
          </div>
        </section>

        <!-- 数据概览 -->
        <section class="stats-row" aria-label="数据概览">
          <div class="stat-card">
            <span class="stat-num">{{ publicCount }}</span>
            <span class="stat-label">公开文档</span>
          </div>
          <div v-if="isSelf" class="stat-card">
            <span class="stat-num">{{ privateCount }}</span>
            <span class="stat-label">私有文档</span>
          </div>
          <div class="stat-card">
            <span class="stat-num">{{ totalDownloads }}</span>
            <span class="stat-label">共享文档下载量</span>
          </div>
        </section>

        <!-- 近期动态 -->
        <section class="card activity-card">
          <h3 class="section-title">近期动态</h3>
          <div class="activity-grid" :class="{ single: !isSelf }">
            <div class="activity-col">
              <h4>最近上传</h4>
              <ul v-if="recentUploads.length" class="activity-list">
                <li v-for="d in recentUploads" :key="d.id" class="activity-item">
                  <span class="activity-icon">
                    <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true">
                      <path d="M4 3.5h7L15 7.5v9H4v-13Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" />
                      <path d="M11 3.5v4h4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" />
                    </svg>
                  </span>
                  <span class="activity-main">
                    <span class="activity-name" :title="d.file_name">{{ d.file_name }}</span>
                    <span class="activity-sub">
                      {{ d.is_public ? '公开' : '私有' }} · {{ fmtTime(d.updated_at) }}
                    </span>
                  </span>
                  <StatusBadge v-if="d.sync_status === 'failed'" status="failed" />
                </li>
              </ul>
              <p v-else class="activity-empty">暂无上传记录</p>
            </div>

            <div v-if="isSelf" class="activity-col">
              <h4>最近会话</h4>
              <ul v-if="recentSessions.length" class="activity-list">
                <li
                  v-for="s in recentSessions"
                  :key="s.session_id"
                  class="activity-item clickable"
                  role="button"
                  tabindex="0"
                  :title="s.last_message_preview || s.title"
                  @click="openSession(s.session_id)"
                  @keydown.enter="openSession(s.session_id)"
                >
                  <span class="activity-icon chat">
                    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                      <path d="M3 2.5h10v8H7.2L4 13V10.5H3v-8Zm2 3h6M5 8h4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                  </span>
                  <span class="activity-main">
                    <span class="activity-name">{{ s.title }}</span>
                    <span class="activity-sub">{{ s.last_message_preview || '…' }} · {{ fmtTime(s.last_message_at) }}</span>
                  </span>
                </li>
              </ul>
              <p v-else class="activity-empty">暂无会话记录</p>
            </div>
          </div>
        </section>

        <!-- 公开文档 -->
        <section class="public-docs">
          <div class="section-head">
            <h3 class="section-title">公开文档</h3>
            <span class="count" v-if="!docsLoading">{{ publicCount }} 个文档</span>
          </div>

          <div v-if="docsLoading" class="loading-block">加载中…</div>

          <div v-else-if="publicDocs.length" class="kb-list">
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
                  <span>{{ d.chunk_count ?? 0 }} 个分块</span>
                  <template v-if="d.updated_at"> · 更新于 {{ fmtTime(d.updated_at) }}</template>
                </div>
              </div>
              <div class="kb-item-side">
                <span class="kb-stats">下载 {{ d.download_count ?? 0 }} 次</span>
                <button class="btn btn-subtle kb-download" @click="download(d)">
                  <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true">
                    <path d="M10 3.5v8m0 0l-3-3m3 3l3-3M4.5 14.5h11" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                  下载
                </button>
              </div>
            </article>

            <div v-if="publicDocs.length > pageSize" class="kb-pager">
              <button class="btn btn-ghost kb-pager-btn" :disabled="page <= 1" @click="page--">上一页</button>
              <span class="kb-pager-info">第 {{ page }} / {{ totalPages }} 页</span>
              <button class="btn btn-ghost kb-pager-btn" :disabled="page >= totalPages" @click="page++">下一页</button>
            </div>
          </div>

          <div v-else class="kb-empty">
            <div class="kb-empty-mark">
              <svg viewBox="0 0 64 64" width="36" height="36" aria-hidden="true">
                <rect width="64" height="64" rx="16" fill="var(--accent-soft)" />
                <path d="M32 15c-4-2.6-8.8-4-14-4v30c5.2 0 10 1.4 14 4 4-2.6 8.8-4 14-4V11c-5.2 0-10 1.4-14 4Z" fill="none" stroke="var(--accent)" stroke-width="3.4" stroke-linejoin="round" />
                <path d="M32 15v30" stroke="var(--accent)" stroke-width="3.4" stroke-linecap="round" />
              </svg>
            </div>
            <h3>暂无公开文档</h3>
            <p>{{ isSelf ? '把文档设为「共享」后，其他用户将在这里看到它。' : '该用户还没有共享文档。' }}</p>
          </div>
        </section>

        <!-- 账号管理（仅本人） -->
        <section v-if="isSelf" class="card mgmt-card">
          <h3 class="section-title">账号管理</h3>
          <div class="mgmt-grid">
            <div class="mgmt-block">
              <h4>修改密码</h4>
              <form class="cp-form" @submit.prevent="changePwd">
                <input v-model="cpOld" class="field" type="password" autocomplete="current-password" placeholder="当前密码" />
                <input v-model="cpNew" class="field" type="password" autocomplete="new-password" placeholder="新密码（至少 6 位）" />
                <input v-model="cpConfirm" class="field" type="password" autocomplete="new-password" placeholder="确认新密码" />
                <p v-if="cpError" class="cp-msg err" role="alert">{{ cpError }}</p>
                <p v-if="cpDone" class="cp-msg ok" role="status">{{ cpDone }}</p>
                <button class="btn btn-primary" type="submit" :disabled="cpLoading">
                  {{ cpLoading ? '提交中…' : '修改密码' }}
                </button>
              </form>
            </div>

            <div class="mgmt-block danger">
              <h4>删除账号</h4>
              <p class="danger-desc">
                删除后你的全部文档与向量将被清理，账号将无法登录，且此操作不可撤销。请谨慎操作。
              </p>
              <template v-if="auth.isAdmin">
                <p class="danger-desc">管理员账号不支持自助删除，请联系系统管理员处理。</p>
              </template>
              <button v-else class="btn btn-danger" :disabled="deleting" @click="requestDeleteAccount">
                {{ deleting ? '提交中…' : '删除账号' }}
              </button>
            </div>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
.profile-page {
  flex: 1;
  display: flex;
  justify-content: center;
  padding: 0 var(--page-pad) 40px;
}
.profile-inner {
  width: 100%;
  max-width: 1000px;
}

.pf-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  padding: 26px 0 6px;
}
.pf-head h1 {
  margin: 0;
  font-size: 21px;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.pf-head .sub {
  margin: 6px 0 0;
  color: var(--text-2);
  font-size: 13.5px;
}
.back-btn { flex: none; }

.error-banner {
  color: var(--err);
  font-size: 13.5px;
  background: var(--err-soft);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  margin: 14px 0;
  text-align: left;
}
.error-link { color: var(--err); font-weight: 500; margin-left: 8px; }
.error-link:hover { text-decoration: underline; }

.loading-block {
  padding: 60px 20px;
  text-align: center;
  color: var(--text-3);
  font-size: 14px;
}

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: 22px 24px;
  margin-top: 18px;
}

/* 账号信息卡 */
.account-card {
  display: flex;
  align-items: center;
  gap: 18px;
}
.avatar-xl {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 64px;
  height: 64px;
  border-radius: 50%;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 26px;
  font-weight: 700;
  flex: none;
}
.account-main { min-width: 0; flex: 1; }
.account-name-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.account-name-row h2 {
  margin: 0;
  font-size: 19px;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.role {
  font-size: 11px;
  color: var(--text-3);
  padding: 3px 9px;
  border-radius: var(--radius-pill);
  background: var(--neutral-soft);
}
.role.admin { color: var(--accent); background: var(--accent-soft); }
.me-tag {
  font-size: 11px;
  color: var(--ok);
  background: var(--ok-soft);
  padding: 3px 9px;
  border-radius: var(--radius-pill);
}
.account-meta { margin: 8px 0 0; color: var(--text-3); font-size: 13px; }
.account-actions { flex: none; display: flex; align-items: center; gap: 10px; }

/* 数据概览 */
.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px;
  margin-top: 18px;
}
.stat-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-xs);
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.stat-num { font-size: 26px; font-weight: 700; letter-spacing: -0.02em; color: var(--accent); }
.stat-label { font-size: 13px; color: var(--text-2); }

/* 近期动态 */
.activity-card { padding-bottom: 18px; }
.section-title { margin: 0; font-size: 15.5px; font-weight: 650; }
.activity-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 28px;
  margin-top: 14px;
}
.activity-grid.single { grid-template-columns: 1fr; }
.activity-col h4 {
  margin: 0 0 10px;
  font-size: 13px;
  font-weight: 550;
  color: var(--text-2);
}
.activity-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.activity-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  transition: background 0.14s ease;
}
.activity-item.clickable { cursor: pointer; }
.activity-item.clickable:hover { background: var(--surface-3); }
.activity-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 7px;
  background: var(--accent-soft);
  color: var(--accent);
  flex: none;
}
.activity-icon.chat { background: var(--neutral-soft); color: var(--neutral); }
.activity-main { min-width: 0; display: flex; flex-direction: column; }
.activity-name {
  font-size: 13.5px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.activity-sub {
  font-size: 12px;
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.activity-empty { margin: 0; color: var(--text-3); font-size: 13px; padding: 8px 10px; }

/* 公开文档 */
.public-docs { margin-top: 18px; }
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.section-head .count { font-size: 12.5px; color: var(--text-3); white-space: nowrap; }

.kb-list { display: flex; flex-direction: column; gap: 10px; }
.kb-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 16px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-xs);
  transition: border-color 0.16s ease, box-shadow 0.16s ease;
}
.kb-item:hover { border-color: var(--border-strong); box-shadow: var(--shadow-sm); }
.kb-item-main { min-width: 0; }
.kb-item-title { display: flex; align-items: center; gap: 9px; }
.kb-file-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 7px;
  background: var(--accent-soft);
  color: var(--accent);
  flex: none;
}
.kb-name {
  font-size: 14.5px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kb-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 7px;
  font-size: 12px;
  color: var(--text-3);
  flex-wrap: wrap;
}
.kb-source {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 360px;
}
.kb-item-side { display: flex; align-items: center; gap: 14px; flex: none; }
.kb-stats { font-size: 12px; color: var(--text-3); white-space: nowrap; }

.kb-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px 20px;
  text-align: center;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
}
.kb-empty-mark {
  width: 56px;
  height: 56px;
  border-radius: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 14px;
}
.kb-empty h3 { margin: 0 0 6px; font-size: 16px; font-weight: 650; }
.kb-empty p { margin: 0; color: var(--text-2); font-size: 13.5px; }

.kb-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  padding: 16px 0 4px;
}
.kb-pager-btn { min-width: 76px; justify-content: center; }
.kb-pager-info { font-size: 12.5px; color: var(--text-3); white-space: nowrap; }

/* 账号管理 */
.mgmt-card { padding-bottom: 20px; }
.mgmt-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 32px;
  margin-top: 16px;
}
.mgmt-block h4 { margin: 0 0 10px; font-size: 13.5px; font-weight: 600; }
.cp-form { display: flex; flex-direction: column; gap: 10px; }
.cp-form .btn { align-self: flex-start; }
.cp-msg { margin: 0; font-size: 13px; }
.cp-msg.err { color: var(--err); }
.cp-msg.ok { color: var(--ok); }
.danger-desc { margin: 0 0 14px; color: var(--text-2); font-size: 13px; line-height: 1.7; }
.mgmt-block.danger {
  padding-left: 32px;
  border-left: 1px solid var(--border);
}

@media (max-width: 720px) {
  .activity-grid, .mgmt-grid { grid-template-columns: 1fr; gap: 18px; }
  .mgmt-block.danger { padding-left: 0; border-left: none; padding-top: 18px; border-top: 1px solid var(--border); }
  .account-card { flex-wrap: wrap; }
  .account-actions { width: 100%; }
  .account-actions .btn { width: 100%; }
  .kb-item { flex-direction: column; align-items: stretch; gap: 10px; }
  .kb-item-side { justify-content: space-between; }
  .kb-source { max-width: 100%; }
}
</style>
