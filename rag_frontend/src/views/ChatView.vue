<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  streamChat,
  listChatSessions,
  getChatSession,
  deleteChatSession,
  renameChatSession,
  searchUsers,
  getUserProfile,
  type SourceItem,
  type ChatSessionInfo,
  type UserSearchItem,
} from '../api/client'
import { useAuthStore } from '../stores/auth'
import SourceStrip from '../components/SourceStrip.vue'

interface MessageItem {
  role: 'user' | 'assistant'
  content: string
  sources: SourceItem[]
  intent?: string
  error?: boolean
}

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const messages = ref<MessageItem[]>([])
const input = ref('')
const sessionId = ref<string | null>(null)
const loadingSession = ref(false)  // 切换历史会话时的加载占位，避免闪现新建会话空态
const streaming = ref(false)
const waitStart = ref(0)
const waitSeconds = ref(0)
const waitPhase = ref<'retrieving' | 'generating'>('retrieving')
let waitTimer: number | undefined
const box = ref<HTMLElement | null>(null)
const inputEl = ref<HTMLTextAreaElement | null>(null)

const isLoggedIn = computed(() => auth.isLoggedIn)

const suggestions = [
  '我上传的文档里有哪些关键结论？',
  '把文档里的要点整理成一份清单',
  '别人共享给我的资料讲了什么？',
  '复杂问题可以拆成子问题来回答吗？',
]

const currentTitle = computed(
  () => sessions.value.find((s) => s.session_id === sessionId.value)?.title ?? '新会话',
)

/* ── 会话检索范围（4 复选框，首问后锁定） ───────────────── */
interface ScopeState {
  ownPrivate: boolean
  ownPublic: boolean
  kbPublic: boolean
  ownerIds: number[]
  ownerNames: string[]
}
const scope = reactive<ScopeState>({
  ownPrivate: true,
  ownPublic: true,
  kbPublic: true,
  ownerIds: [],
  ownerNames: [],
})
const scopeLocked = ref(false)
const scopeError = ref('')

// 「指定用户的公开文档」搜索多选器
const userQuery = ref('')
const userResults = ref<UserSearchItem[]>([])
const userSearching = ref(false)
const userSearchOpen = ref(false)
let userSearchTimer: number | undefined

const scopeSummary = computed(() => {
  const parts: string[] = []
  if (scope.ownPrivate) parts.push('我的私有文档')
  if (scope.ownPublic) parts.push('我的公开文档')
  if (scope.kbPublic) parts.push('知识库里的公开文档')
  if (scope.ownerIds.length) {
    const names = scope.ownerNames.length
      ? scope.ownerNames.join('、')
      : scope.ownerIds.map((id) => `#${id}`).join('、')
    parts.push(`指定用户：${names}`)
  }
  return parts.length ? parts.join(' · ') : '未选择任何范围'
})

const scopeValid = computed(
  () => scope.ownPrivate || scope.ownPublic || scope.kbPublic || scope.ownerIds.length > 0,
)

// #3 与 #4 互斥：勾 #3 清空 #4；选 #4 取消 #3
const scopeKbChecked = computed({
  get: () => scope.kbPublic,
  set: (v: boolean) => toggleKbPublic(v),
})
const scopeUserChecked = computed({
  get: () => scope.ownerIds.length > 0 && !scope.kbPublic,
  set: (v: boolean) => {
    if (v) {
      scope.kbPublic = false
      userSearchOpen.value = true
    } else {
      scope.ownerIds = []
      scope.ownerNames = []
      userSearchOpen.value = false
    }
  },
})

function resetScope() {
  scope.ownPrivate = true
  scope.ownPublic = true
  scope.kbPublic = true
  scope.ownerIds = []
  scope.ownerNames = []
  scopeError.value = ''
  scopeLocked.value = false
  userSearchOpen.value = false
}

async function applyRouteScope() {
  // 从个人主页深链进入：/?owner_id=<id> 只检索该用户公开文档；/?own=1 只检索自己的文档
  const ownerId = Number(route.query.owner_id)
  if (Number.isInteger(ownerId) && ownerId > 0) {
    scope.ownPrivate = false
    scope.ownPublic = false
    scope.kbPublic = false
    scope.ownerIds = [ownerId]
    scope.ownerNames = []
    try {
      const p = await getUserProfile(ownerId)
      scope.ownerNames = [p.username]
    } catch {
      // 拿不到用户名时用 #id 兜底
    }
  } else if (route.query.own !== undefined) {
    scope.ownPrivate = true
    scope.ownPublic = true
    scope.kbPublic = false
    scope.ownerIds = []
    scope.ownerNames = []
  }
}

function toggleKbPublic(v: boolean) {
  scope.kbPublic = v
  if (v) {
    scope.ownerIds = []
    scope.ownerNames = []
  }
}

async function addOwner(u: UserSearchItem) {
  if (!scope.ownerIds.includes(u.id)) {
    scope.ownerIds.push(u.id)
    scope.ownerNames.push(u.username)
    scope.kbPublic = false // #3 与 #4 互斥
  }
  userQuery.value = ''
  userSearchOpen.value = false
  scopeError.value = ''
}

function removeOwner(id: number) {
  const idx = scope.ownerIds.indexOf(id)
  if (idx >= 0) {
    scope.ownerIds.splice(idx, 1)
    scope.ownerNames.splice(idx, 1)
  }
}

async function runUserSearch() {
  const q = userQuery.value.trim()
  if (!q) {
    userResults.value = []
    userSearching.value = false
    return
  }
  userSearching.value = true
  try {
    userResults.value = await searchUsers(q)
    userSearchOpen.value = true
  } catch {
    userResults.value = []
  } finally {
    userSearching.value = false
  }
}

function onUserQueryInput() {
  if (userSearchTimer !== undefined) window.clearTimeout(userSearchTimer)
  userSearchTimer = window.setTimeout(runUserSearch, 250)
}

function onUserFocus() {
  if (userQuery.value.trim()) userSearchOpen.value = true
}

/* ── 侧边栏（DeepSeek 风格，可折叠） ───────────────────── */
const sessions = ref<ChatSessionInfo[]>([])
const sidebarOpen = ref(typeof window === 'undefined' ? true : window.innerWidth > 768)
const isMobile = ref(false)
const loadingSessions = ref(false)
const editingId = ref<string | null>(null)
const editingTitle = ref('')
const confirmDeleteId = ref<string | null>(null)

function onResize() {
  isMobile.value = window.innerWidth <= 768
}

function formatTime(iso: string | null): string {
  if (!iso) return ''
  const t = new Date(iso.replace(' ', 'T'))
  if (Number.isNaN(t.getTime())) return iso
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

async function refreshSessions() {
  if (!isLoggedIn.value) return
  loadingSessions.value = true
  try {
    sessions.value = await listChatSessions()
  } catch {
    // 静默失败：保留旧列表，不打断问答
  } finally {
    loadingSessions.value = false
  }
}

function newConversation() {
  if (streaming.value) return
  sessionId.value = null
  messages.value = []
  loadingSession.value = false
  input.value = ''
  confirmDeleteId.value = null
  resetScope()
  // 清除个人主页深链的 query（owner_id/own/session），避免刷新后检索范围被重新预置
  if (route.query.owner_id !== undefined || route.query.own !== undefined || route.query.session !== undefined) {
    router.replace({ path: '/', query: {} })
  }
  nextTick(() => inputEl.value?.focus())
}

async function openSession(id: string) {
  if (streaming.value || id === sessionId.value) return
  sessionId.value = id
  input.value = ''
  scopeLocked.value = true
  // 加载完成前先显示「加载中」占位，避免闪现空会话（新建会话）欢迎页
  loadingSession.value = true
  try {
    const detail = await getChatSession(id)
    scope.ownPrivate = detail.retrieve_own_private
    scope.ownPublic = detail.retrieve_own_public
    scope.kbPublic = detail.retrieve_kb_public
    scope.ownerIds = detail.retrieve_owner_ids ?? []
    scope.ownerNames = detail.retrieve_owner_names ?? []
    messages.value = detail.messages.map((m) => ({
      role: m.role,
      content: m.content,
      sources: m.sources ?? [],
      intent: m.intent ?? undefined,
      error: false,
    }))
  } catch {
    messages.value = []
  } finally {
    loadingSession.value = false
  }
  scrollToBottom()
}

function startRename(s: ChatSessionInfo) {
  editingId.value = s.session_id
  editingTitle.value = s.title
  confirmDeleteId.value = null
}
function renameInputRef(el: unknown) {
  if (el instanceof HTMLInputElement) el.focus()
}
function cancelRename() {
  editingId.value = null
}
async function commitRename(s: ChatSessionInfo) {
  if (editingId.value !== s.session_id) return
  const title = editingTitle.value.trim()
  editingId.value = null
  if (!title || title === s.title) return
  try {
    await renameChatSession(s.session_id, title)
    s.title = title
  } catch {
    // 静默失败
  }
}

function askDelete(s: ChatSessionInfo) {
  confirmDeleteId.value = s.session_id
  window.setTimeout(() => {
    if (confirmDeleteId.value === s.session_id) confirmDeleteId.value = null
  }, 3000)
}
async function removeSession(s: ChatSessionInfo, confirmed = false) {
  if (!confirmed) {
    askDelete(s)
    return
  }
  confirmDeleteId.value = null
  try {
    await deleteChatSession(s.session_id)
    if (sessionId.value === s.session_id) {
      sessionId.value = null
      messages.value = []
      loadingSession.value = false
    }
    sessions.value = sessions.value.filter((x) => x.session_id !== s.session_id)
  } catch {
    // 静默失败
  }
}

/* ── 问答主流程 ───────────────────────────────────────── */
function scrollToBottom() {
  nextTick(() => {
    const el = box.value
    if (!el) return
    el.scrollTop = el.scrollHeight
    // 等一帧：图片 / 来源卡片等异步内容渲染完成后再定位一次，确保滚到底部
    requestAnimationFrame(() => {
      const el2 = box.value
      if (el2) el2.scrollTop = el2.scrollHeight
    })
  })
}

function startWaitTimer() {
  waitStart.value = Date.now()
  waitSeconds.value = 0
  waitPhase.value = 'retrieving'
  if (waitTimer !== undefined) window.clearInterval(waitTimer)
  waitTimer = window.setInterval(() => {
    waitSeconds.value = (Date.now() - waitStart.value) / 1000
  }, 100)
}

function stopWaitTimer() {
  if (waitTimer !== undefined) {
    window.clearInterval(waitTimer)
    waitTimer = undefined
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || streaming.value || !isLoggedIn.value) return
  if (!scopeValid.value) {
    scopeError.value = '请至少勾选一种检索范围'
    return
  }
  input.value = ''
  autoGrow()
  messages.value.push({ role: 'user', content: text, sources: [] })
  const assistant = reactive<MessageItem>({ role: 'assistant', content: '', sources: [] })
  messages.value.push(assistant)
  streaming.value = true
  startWaitTimer()
  scrollToBottom()

  const body = {
    content: text,
    session_id: sessionId.value,
    retrieve_own_private: scope.ownPrivate,
    retrieve_own_public: scope.ownPublic,
    retrieve_kb_public: scope.kbPublic,
    retrieve_owner_ids: scope.ownerIds,
  }
  try {
    await streamChat(body, (evt) => {
      if (evt.type === 'meta') {
        if (evt.session_id) sessionId.value = evt.session_id
        assistant.sources = evt.sources
        assistant.intent = evt.intent
        waitPhase.value = 'generating'
        scrollToBottom()
      } else if (evt.type === 'token') {
        assistant.content += evt.text
        scrollToBottom()
      } else if (evt.type === 'answer') {
        // 闲聊/提示类回复只发 answer 事件（带 session_id），不发 meta，需同步更新会话 id 以便续接
        if (evt.session_id) sessionId.value = evt.session_id
        assistant.content = evt.answer
        assistant.sources = evt.sources
        assistant.intent = evt.intent
      } else if (evt.type === 'error') {
        assistant.content = evt.message
        assistant.error = true
      }
    })
  } catch (e) {
    assistant.content = e instanceof Error ? e.message : String(e)
    assistant.error = true
  } finally {
    streaming.value = false
    stopWaitTimer()
    scopeLocked.value = true
    await refreshSessions()
    scrollToBottom()
  }
}

function autoGrow() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 200) + 'px'
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

watch(isLoggedIn, (v) => {
  if (v) {
    refreshSessions()
  } else {
    sessions.value = []
    sessionId.value = null
    messages.value = []
  }
})

onMounted(() => {
  onResize()
  window.addEventListener('resize', onResize)
  if (isLoggedIn.value) {
    refreshSessions()
    applyRouteScope()
    // 支持从个人详情「最近会话」深链打开：/?session=<id>
    const sid = route.query.session
    if (typeof sid === 'string' && sid) {
      openSession(sid)
    }
  }
})
onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  stopWaitTimer()
})
</script>

<template>
  <div class="chat-layout">
    <!-- 历史会话侧边栏（仅登录用户） -->
    <aside v-if="isLoggedIn" class="sidebar" :class="{ open: sidebarOpen, 'is-mobile': isMobile }">
      <template v-if="sidebarOpen">
        <div class="sidebar-head">
          <button class="new-chat-btn" :disabled="streaming" @click="newConversation">
            <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
              <path d="M8 3v10M3 8h10" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
            新建会话
          </button>
          <button class="icon-btn" title="收起侧边栏" @click="sidebarOpen = false">
            <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
              <path d="M10 4L6 8l4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
        </div>

        <div class="sidebar-list scroll-thin">
          <div v-if="loadingSessions && !sessions.length" class="sidebar-tip">加载中…</div>
          <div v-else-if="!sessions.length" class="sidebar-tip">暂无历史会话，开始提问吧</div>

          <div
            v-for="s in sessions"
            :key="s.session_id"
            class="session-item"
            :class="{ active: s.session_id === sessionId }"
            @click="openSession(s.session_id)"
          >
            <span class="session-mark" aria-hidden="true">
              <svg viewBox="0 0 16 16" width="14" height="14">
                <path d="M3 2.5h10v8H7.2L4 13V10.5H3v-8Zm2 3h6M5 8h4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </span>
            <div class="session-body">
              <template v-if="editingId === s.session_id">
                <input
                  v-model="editingTitle"
                  class="rename-input"
                  maxlength="128"
                  placeholder="会话标题"
                  @click.stop
                  @keydown.enter="commitRename(s)"
                  @keydown.esc="cancelRename()"
                  @blur="commitRename(s)"
                  :ref="editingId === s.session_id ? renameInputRef : undefined"
                />
              </template>
              <template v-else>
                <span class="session-title">{{ s.title }}</span>
                <span class="session-preview">{{ s.last_message_preview || '…' }}</span>
                <span class="session-time">{{ formatTime(s.last_message_at) }}</span>
              </template>
            </div>
            <div class="session-actions" @click.stop>
              <template v-if="confirmDeleteId === s.session_id">
                <button class="mini-btn danger" title="确认删除" @click="removeSession(s, true)">确认</button>
                <button class="mini-btn" title="取消" @click="confirmDeleteId = null">取消</button>
              </template>
              <template v-else>
                <button class="icon-btn" title="重命名" @click="startRename(s)">
                  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                    <path d="M11.3 2.7a1.6 1.6 0 0 1 2.3 0l-.4-.4a1.6 1.6 0 0 1 0 2.3L5.5 12.3 2 13.3l1-3.5 8.3-7.1Z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" />
                  </svg>
                </button>
                <button class="icon-btn danger" title="删除" @click="askDelete(s)">
                  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                    <path d="M3 4.5h10M6.5 4.5V3.2h3v1.3M5 4.5l.5 8.3h5l.5-8.3M6.7 7v3.2M9.3 7v3.2" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                </button>
              </template>
            </div>
          </div>
        </div>

        <div class="sidebar-foot">
          <span>会话默认保留 1 天</span>
        </div>
      </template>

      <template v-else>
        <button class="rail-btn" title="展开侧边栏" @click="sidebarOpen = true">
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <button class="rail-btn" title="新建会话" :disabled="streaming" @click="newConversation">
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <path d="M8 3v10M3 8h10" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
          </svg>
        </button>
      </template>
    </aside>

    <div v-if="isMobile && isLoggedIn && sidebarOpen" class="sidebar-backdrop" @click="sidebarOpen = false"></div>

    <!-- 主问答区 -->
    <div class="chat-main">
      <header class="chat-head">
        <div class="chat-title">
          <button v-if="isLoggedIn" class="icon-btn head-toggle" :title="sidebarOpen ? '收起侧边栏' : '展开侧边栏'" @click="sidebarOpen = !sidebarOpen">
            <svg v-if="!sidebarOpen" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
              <path d="M2 4h12M2 8h12M2 12h12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
            <svg v-else viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
              <path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
          <span class="pulse" aria-hidden="true"></span>
          <h1>知识库问答</h1>
          <span v-if="isLoggedIn && sessionId" class="session-chip" :title="currentTitle">{{ currentTitle }}</span>
        </div>
        <div v-if="isLoggedIn" class="chat-actions">
          <button class="btn btn-subtle" @click="newConversation">新会话</button>
        </div>
      </header>

      <div class="chat-inner">
        <div ref="box" class="messages scroll-thin">
          <!-- 加载历史会话中（避免闪现新建会话空态） -->
          <div v-if="loadingSession" class="empty loading-session">
            <div class="loading-session-inner">
              <span class="ls-dots">
                <span class="waiting-dot" aria-hidden="true"></span>
                <span class="waiting-dot" aria-hidden="true"></span>
                <span class="waiting-dot" aria-hidden="true"></span>
              </span>
              <p class="ls-text">正在加载会话…</p>
            </div>
          </div>

          <!-- 空状态 / 访客态 -->
          <div v-else-if="!messages.length" class="empty">
            <div class="empty-hero">
              <div class="empty-mark">
                <svg viewBox="0 0 64 64" width="46" height="46" aria-hidden="true">
                  <rect width="64" height="64" rx="16" fill="var(--accent)" />
                  <path d="M32 15c-4-2.6-8.8-4-14-4v30c5.2 0 10 1.4 14 4 4-2.6 8.8-4 14-4V11c-5.2 0-10 1.4-14 4Z" fill="none" stroke="#fff" stroke-width="3.4" stroke-linejoin="round" />
                  <path d="M32 15v30" stroke="#fff" stroke-width="3.4" stroke-linecap="round" />
                  <path d="M38 7l1.1 3.1L42 11l-2.9.9L38 15l-1.1-3.1L34 11l2.9-.9L38 7Z" fill="#bcd0ff" />
                </svg>
              </div>
              <h2>{{ isLoggedIn ? '今天想问点什么？' : '登录后即可向知识库提问' }}</h2>
              <p class="empty-sub">
                答案会同时检索<b class="hl">你自己的文档</b>与<b class="hl">他人共享的文档</b>，
                并给出可追溯的来源引用。
              </p>
            </div>

            <div v-if="isLoggedIn" class="suggestions">
              <button
                v-for="s in suggestions"
                :key="s"
                class="suggestion"
                @click="input = s; autoGrow()"
              >
                <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                  <path d="M3 8h10M8 3v10" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
                </svg>
                {{ s }}
              </button>
            </div>

            <div v-if="isLoggedIn && !scopeLocked" class="scope-picker">
              <div class="scope-head">
                <span class="scope-title">本次会话的检索范围</span>
                <span class="scope-hint">首条消息发送后锁定，如需更改请新建会话</span>
              </div>
              <div class="scope-options">
                <label class="scope-option">
                  <input type="checkbox" v-model="scope.ownPrivate" />
                  <span class="scope-label">是否检索我的私有文档</span>
                </label>
                <label class="scope-option">
                  <input type="checkbox" v-model="scope.ownPublic" />
                  <span class="scope-label">是否检索我的公开文档</span>
                </label>
                <label class="scope-option">
                  <input type="checkbox" v-model="scopeKbChecked" />
                  <span class="scope-label">是否检索知识库里的公开文档</span>
                </label>
                <label class="scope-option" :class="{ muted: scope.kbPublic }">
                  <input type="checkbox" v-model="scopeUserChecked" :disabled="scope.kbPublic" />
                  <span class="scope-label">是否检索指定用户的公开文档</span>
                </label>
              </div>

              <div v-if="!scope.kbPublic" class="scope-user-search">
                <div v-if="scope.ownerIds.length" class="scope-chips">
                  <span v-for="(name, i) in scope.ownerNames" :key="scope.ownerIds[i]" class="scope-chip">
                    {{ name || `用户 #${scope.ownerIds[i]}` }}
                    <button type="button" class="chip-x" :aria-label="`移除 ${name}`" @click="removeOwner(scope.ownerIds[i])">×</button>
                  </span>
                </div>
                <div class="scope-user-input-row">
                  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                    <circle cx="7" cy="7" r="4.2" fill="none" stroke="currentColor" stroke-width="1.4" />
                    <path d="M10.2 10.2L13 13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
                  </svg>
                  <input
                    v-model="userQuery"
                    class="scope-user-input"
                    placeholder="搜索用户名以指定检索该用户的公开文档（可多选）"
                    @input="onUserQueryInput"
                    @focus="onUserFocus"
                    @keydown.esc="userSearchOpen = false"
                  />
                </div>
                <div v-if="userSearchOpen && (userSearching || userResults.length || userQuery.trim())" class="user-dropdown">
                  <div v-if="userSearching" class="user-drop-tip">搜索中…</div>
                  <template v-else>
                    <button
                      v-for="u in userResults"
                      :key="u.id"
                      type="button"
                      class="user-option"
                      @mousedown.prevent
                      @click="addOwner(u)"
                    >
                      <span class="user-avatar">{{ u.username.slice(0, 1).toUpperCase() }}</span>
                      <span class="user-name">{{ u.username }}</span>
                      <span class="user-role" :class="{ admin: u.role === 'admin' }">{{ u.role === 'admin' ? '管理员' : '用户' }}</span>
                    </button>
                    <p v-if="!userResults.length" class="user-drop-tip">未找到匹配用户</p>
                  </template>
                </div>
              </div>

              <p v-if="scopeError" class="scope-error" role="alert">{{ scopeError }}</p>
            </div>

            <p v-if="!isLoggedIn" class="guest-note">
              登录后可管理自己的文档，并把它们共享给其他用户检索。
              <RouterLink to="/login" class="login-link">去登录 →</RouterLink>
            </p>
            <p v-else class="feature-note">复杂问题会被自动拆成最多 5 个子问题分别检索，再汇总成一份带引用的回答。</p>
          </div>

          <!-- 对话记录 -->
          <div v-for="(m, i) in messages" :key="i" class="msg-row" :class="m.role">
            <div v-if="m.role === 'user'" class="user-bubble">{{ m.content }}</div>

            <div v-else class="assistant">
              <div v-if="m.sources.length" class="meta-line">
                <span class="meta-intent">{{ m.intent === 'chat' ? '闲聊' : m.intent === 'other' ? '提示' : '知识库检索' }}</span>
                <span v-if="m.sources.some((s) => s.question)" class="meta-split">
                  已拆分为 {{ m.sources.filter((s) => s.question).length + 1 }} 个子问题分别检索
                </span>
                <span v-else class="meta-count">检索到 {{ m.sources.length }} 条来源</span>
              </div>

              <div class="answer" :class="{ error: m.error }">
                <template v-if="m.content">{{ m.content }}<span v-if="streaming && i === messages.length - 1" class="caret" aria-hidden="true">▍</span></template>
                <div v-else-if="streaming && i === messages.length - 1" class="waiting">
                  <span class="waiting-dot" aria-hidden="true"></span>
                  <span class="waiting-text">{{ waitPhase === 'generating' ? '正在生成回答' : '正在检索知识库' }}</span>
                  <span class="waiting-time">{{ waitSeconds.toFixed(1) }}s</span>
                </div>
              </div>

              <SourceStrip v-if="m.sources.length" :sources="m.sources" />
            </div>
          </div>

        </div>

        <!-- 输入 / 登录门 -->
        <div class="composer-wrap">
          <div v-if="isLoggedIn && scopeLocked && (sessionId || messages.length)" class="scope-readonly">
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
              <path d="M2.5 4h11M2.5 8h11M2.5 12h7" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
            </svg>
            <span class="scope-ro-label">检索范围</span>
            <span class="scope-ro-value">{{ scopeSummary }}</span>
            <span v-if="scope.ownerIds.length || !scopeValid" class="scope-ro-tag" title="首条消息后锁定，如需更改请新建会话">已锁定</span>
          </div>
          <div v-if="isLoggedIn" class="composer">
            <textarea
              ref="inputEl"
              v-model="input"
              class="composer-input scroll-thin"
              rows="1"
              placeholder="输入问题，例如：“我从共享文档里看到了什么？”"
              :disabled="streaming"
              @input="autoGrow"
              @keydown="onKeydown"
            ></textarea>
            <button
              class="send"
              :disabled="streaming || !input.trim()"
              aria-label="发送"
              @click="send"
            >
              <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
                <path d="M3 10l13-6-4.5 13L9 12l-6-2Z" fill="currentColor" />
              </svg>
            </button>
          </div>

          <div v-else class="login-gate">
            <span class="gate-text">登录后即可开始提问和上传文档</span>
            <RouterLink to="/login" class="btn btn-primary gap">登录 / 注册</RouterLink>
          </div>

          <p class="composer-hint">
            支持多轮对话与追问指代 · Enter 发送 · Shift+Enter 换行
            <span class="hint-sep">·</span> 答案来源可追溯
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── 布局：侧边栏 + 主问答区 ─────────────────────────── */
.chat-layout {
  flex: 1;
  display: flex;
  min-height: 0;
  align-items: stretch;
}

.sidebar {
  flex: none;
  width: 52px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 12px 0;
  background: var(--bg-soft);
  border-right: 1px solid var(--border);
  transition: width 0.22s ease;
  overflow: hidden;
}
.sidebar.open {
  width: 280px;
  align-items: stretch;
  padding: 12px 10px;
  gap: 0;
}

.sidebar-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.new-chat-btn {
  flex: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 36px;
  border-radius: var(--radius-sm);
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--text);
  font-weight: 550;
  font-size: 13.5px;
  transition: border-color 0.16s ease, color 0.16s ease, box-shadow 0.16s ease;
}
.new-chat-btn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
  box-shadow: var(--shadow-xs);
}
.new-chat-btn:disabled { opacity: 0.5; cursor: not-allowed; }

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  flex: none;
  border-radius: 8px;
  color: var(--text-3);
  transition: background 0.14s ease, color 0.14s ease;
}
.icon-btn:hover { background: var(--surface-3); color: var(--text); }
.icon-btn.danger:hover { background: var(--err-soft); color: var(--err); }

.rail-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  flex: none;
  border-radius: var(--radius-sm);
  color: var(--text-2);
  transition: background 0.14s ease, color 0.14s ease;
}
.rail-btn:hover { background: var(--surface-3); color: var(--text); }
.rail-btn:disabled { opacity: 0.5; cursor: not-allowed; }

.sidebar-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-bottom: 6px;
}
.sidebar-tip {
  padding: 22px 12px;
  text-align: center;
  font-size: 12.5px;
  color: var(--text-3);
}

.session-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 9px 8px;
  border-radius: var(--radius-sm);
  color: var(--text-2);
  cursor: pointer;
  text-align: left;
  transition: background 0.14s ease;
}
.session-item:hover { background: var(--surface-3); }
.session-item.active { background: var(--accent-soft); }
.session-mark {
  flex: none;
  display: inline-flex;
  color: var(--text-3);
}
.session-item.active .session-mark { color: var(--accent); }
.session-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.session-title {
  font-size: 13.5px;
  font-weight: 550;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-preview {
  font-size: 12px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-time { font-size: 11px; color: var(--text-3); }
.session-actions {
  flex: none;
  display: none;
  align-items: center;
  gap: 2px;
}
.session-item:hover .session-actions,
.session-item.active .session-actions { display: inline-flex; }

.rename-input {
  width: 100%;
  height: 26px;
  border: 1px solid var(--accent);
  border-radius: 7px;
  padding: 0 8px;
  font-size: 13px;
  background: var(--surface);
  color: var(--text);
}
.rename-input:focus { box-shadow: var(--shadow-focus); }

.mini-btn {
  height: 24px;
  padding: 0 8px;
  border-radius: 7px;
  font-size: 12px;
  color: var(--text-2);
  background: var(--surface-3);
}
.mini-btn.danger { color: var(--err); background: var(--err-soft); }
.mini-btn:hover { filter: brightness(0.97); }

.sidebar-foot {
  padding: 10px 4px 2px;
  margin-top: 8px;
  border-top: 1px solid var(--border);
  font-size: 11px;
  color: var(--text-3);
  text-align: center;
}

.sidebar-backdrop {
  position: fixed;
  inset: var(--nav-h) 0 0 0;
  background: rgba(15, 23, 42, 0.28);
  z-index: 29;
}

/* ── 主问答区 ───────────────────────────────────────── */
.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 0 var(--page-pad);
}
.chat-inner {
  width: 100%;
  max-width: var(--content-max);
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
}

.chat-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  max-width: var(--content-max);
  padding: 22px 0 8px;
}
.chat-title {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.head-toggle { margin-left: -8px; }
.chat-title h1 {
  margin: 0;
  font-size: 18px;
  font-weight: 650;
  letter-spacing: -0.01em;
  white-space: nowrap;
}
.pulse {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-soft);
}
.session-chip {
  max-width: 260px;
  font-size: 12px;
  color: var(--text-3);
  background: var(--surface-3);
  padding: 3px 10px;
  border-radius: var(--radius-pill);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chat-actions { display: flex; align-items: center; gap: 10px; }

.messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 4px 28px;
  scroll-behavior: smooth;
}

/* Empty / guest */
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: calc(100% - 20px);
  text-align: center;
  padding: 40px 12px;
}
.empty-hero { max-width: 520px; }
.empty-mark {
  display: inline-flex;
  width: 64px;
  height: 64px;
  border-radius: 18px;
  background: var(--accent-soft);
  align-items: center;
  justify-content: center;
  margin-bottom: 20px;
}
.empty-mark svg { border-radius: 12px; }
.empty-hero h2 {
  margin: 0 0 10px;
  font-size: 24px;
  font-weight: 650;
  letter-spacing: -0.015em;
}
.empty-sub {
  color: var(--text-2);
  font-size: 14.5px;
  line-height: 1.7;
  margin: 0;
}
.hl { color: var(--text); font-weight: 600; }
.loading-session { display: flex; }
.loading-session-inner {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  color: var(--text-3);
  font-size: 13.5px;
}
.ls-dots { display: flex; gap: 6px; }

.suggestions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 10px;
  margin-top: 30px;
  max-width: 560px;
}
.suggestion {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 9px 14px;
  border: 1px solid var(--border);
  background: var(--surface);
  border-radius: var(--radius-pill);
  color: var(--text-2);
  font-size: 13px;
  transition: border-color 0.16s ease, color 0.16s ease, box-shadow 0.16s ease;
}
.suggestion svg { color: var(--accent); }
.suggestion:hover {
  border-color: var(--accent);
  color: var(--text);
  box-shadow: var(--shadow-xs);
}
.feature-note {
  margin-top: 30px;
  font-size: 12.5px;
  color: var(--text-3);
}
.guest-note {
  margin-top: 26px;
  font-size: 13px;
  color: var(--text-2);
}
.login-link {
  font-weight: 600;
  margin-left: 4px;
}

/* Messages */
.msg-row {
  display: flex;
  margin: 18px 0;
}
.msg-row.user { justify-content: flex-end; }
.user-bubble {
  max-width: 78%;
  background: var(--accent);
  color: var(--text-on-accent);
  padding: 11px 16px;
  border-radius: var(--radius-lg) var(--radius-lg) var(--radius-xs) var(--radius-lg);
  font-size: 15px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  box-shadow: var(--shadow-xs);
}
.msg-row.assistant { justify-content: flex-start; }
.assistant {
  width: 100%;
  max-width: 100%;
}
.meta-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-3);
  margin-bottom: 8px;
}
.meta-intent {
  color: var(--accent);
  background: var(--accent-soft);
  padding: 2px 8px;
  border-radius: var(--radius-pill);
  font-weight: 500;
}
.meta-split, .meta-count { color: var(--text-3); }
.answer {
  font-size: 15.5px;
  line-height: 1.8;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}
.answer.error { color: var(--err); }
.caret {
  display: inline-block;
  width: 8px;
  color: var(--accent);
  animation: blink 1s steps(2, start) infinite;
}
@keyframes blink { 50% { opacity: 0; } }

/* 等待检索 / 生成时的状态提示（DeepSeek 风格） */
.waiting {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  color: var(--text-2);
  font-size: 14px;
  line-height: 1.6;
  min-height: 24px;
}
.waiting-dot {
  width: 8px;
  height: 8px;
  flex: none;
  border-radius: 50%;
  background: var(--accent);
  animation: waitingPulse 1.2s ease-in-out infinite;
}
@keyframes waitingPulse {
  0%, 100% { opacity: 0.25; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1.12); }
}
.waiting-text { font-weight: 500; }
.waiting-time {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  padding: 1px 8px;
  border-radius: var(--radius-pill);
  font-variant-numeric: tabular-nums;
}

/* Composer */
.composer-wrap { padding: 10px 0 18px; }
.composer {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  padding: 8px 8px 8px 16px;
  box-shadow: var(--shadow-sm);
  transition: border-color 0.16s ease, box-shadow 0.16s ease;
}
.composer:focus-within {
  border-color: var(--accent);
  box-shadow: var(--shadow-focus), var(--shadow-sm);
}
.composer-input {
  flex: 1;
  border: none;
  background: transparent;
  resize: none;
  font-size: 15px;
  line-height: 1.6;
  padding: 6px 0;
  max-height: 200px;
  outline: none;
  color: var(--text);
}
.composer-input::placeholder { color: var(--text-3); }
.composer-input:disabled { opacity: 0.6; }
.send {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 12px;
  background: var(--accent);
  color: #fff;
  transition: background 0.16s ease, opacity 0.16s ease;
}
.send:hover:not(:disabled) { background: var(--accent-strong); }
.send:disabled { background: var(--surface-3); color: var(--text-3); cursor: not-allowed; }
.composer-hint {
  margin: 8px 4px 0;
  font-size: 12px;
  color: var(--text-3);
  text-align: center;
}
.hint-sep { margin: 0 4px; }

.login-gate {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  padding: 16px 18px;
  box-shadow: var(--shadow-sm);
}
.gate-text { color: var(--text-2); font-size: 14px; }
.gap { gap: 6px; }

.scope-picker {
  margin-top: 26px;
  max-width: 560px;
  margin-left: auto;
  margin-right: auto;
  text-align: left;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 14px 16px;
  box-shadow: var(--shadow-xs);
}
.scope-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.scope-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.scope-hint {
  font-size: 11.5px;
  color: var(--text-3);
}
.scope-options {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px 18px;
}
.scope-option {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-2);
  cursor: pointer;
  user-select: none;
}
.scope-option.muted { opacity: 0.55; }
.scope-option input[type='checkbox'] {
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
  flex: none;
  margin: 0;
  cursor: pointer;
}
.scope-label { line-height: 1.5; }
.scope-user-search { margin-top: 12px; }
.scope-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.scope-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px 3px 10px;
  font-size: 12px;
  color: var(--accent);
  background: var(--accent-soft);
  border-radius: var(--radius-pill);
}
.chip-x {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  border-radius: 50%;
  font-size: 12px;
  line-height: 1;
  color: var(--accent);
  background: transparent;
}
.chip-x:hover { background: var(--accent); color: #fff; }
.scope-user-input-row {
  position: relative;
  display: flex;
  align-items: center;
}
.scope-user-input-row svg {
  position: absolute;
  left: 10px;
  color: var(--text-3);
  pointer-events: none;
}
.scope-user-input {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  padding: 8px 12px 8px 30px;
  font-size: 13px;
  color: var(--text);
  outline: none;
  transition: border-color 0.16s ease, box-shadow 0.16s ease;
}
.scope-user-input::placeholder { color: var(--text-3); }
.scope-user-input:focus {
  border-color: var(--accent);
  box-shadow: var(--shadow-focus);
}
.user-dropdown {
  position: absolute;
  z-index: 40;
  left: 0;
  right: 0;
  margin-top: 4px;
  max-height: 220px;
  overflow-y: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow-md);
  padding: 4px;
}
.user-option {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 7px 9px;
  border-radius: var(--radius-sm);
  color: var(--text);
  font-size: 13px;
  text-align: left;
}
.user-option:hover { background: var(--surface-3); }
.user-avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  font-weight: 650;
  flex: none;
}
.user-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.user-role { font-size: 11px; color: var(--text-3); padding: 1px 7px; border-radius: var(--radius-pill); background: var(--neutral-soft); flex: none; }
.user-role.admin { color: var(--accent); background: var(--accent-soft); }
.user-drop-tip { margin: 0; padding: 10px 12px; font-size: 12.5px; color: var(--text-3); text-align: center; }
.scope-error {
  margin: 10px 0 0;
  font-size: 12.5px;
  color: var(--err);
}
.scope-readonly {
  display: flex;
  align-items: center;
  gap: 7px;
  max-width: 860px;
  margin: 0 auto 10px;
  padding: 7px 12px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  font-size: 12.5px;
  color: var(--text-3);
}
.scope-ro-label {
  font-weight: 600;
  color: var(--text-2);
  flex: none;
}
.scope-ro-value {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-2);
}
.scope-ro-tag {
  flex: none;
  font-size: 11px;
  color: var(--accent);
  background: var(--accent-soft);
  padding: 1px 8px;
  border-radius: var(--radius-pill);
  font-weight: 500;
}
@media (max-width: 768px) {
  .scope-options { grid-template-columns: 1fr; }
  .sidebar {
    position: fixed;
    left: 0;
    top: var(--nav-h);
    bottom: 0;
    z-index: 30;
    width: 0;
    box-shadow: var(--shadow-md);
  }
  .sidebar.open { width: min(280px, 84vw); }
  .chat-main { padding: 0 14px; }
  .user-bubble { max-width: 92%; }
  .suggestions { flex-direction: column; align-items: stretch; }
  .suggestion { justify-content: flex-start; }
  .login-gate { flex-direction: column; text-align: center; }
}
</style>
