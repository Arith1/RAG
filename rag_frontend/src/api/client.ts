import { useAuthStore } from '../stores/auth'

/** FastAPI 错误体的 detail 通常是字符串；少数接口（如 readiness 503）返回对象，
 *  统一转成可读文案，避免 UI 弹出 [object Object]。 */
function detailMessage(data: unknown, status: number): string {
  const detail = (data as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object') {
    const d = detail as Record<string, unknown>
    if (d.status === 'not_ready' && d.checks && typeof d.checks === 'object') {
      const failed = Object.entries(d.checks as Record<string, unknown>)
        .filter(([, ok]) => ok !== true)
        .map(([name]) => name)
      if (failed.length > 0) return `服务尚未就绪：${failed.join('、')} 检查未通过`
    }
  }
  return `HTTP ${status}`
}

export interface SourceItem {
  index: number
  source: string | null
  score: number | null
  content: string
  /** 复杂问题被拆分成多问题检索时，该来源命中的子问题 */
  question?: string | null
}

export type SyncStatus = 'pending' | 'in_sync' | 'failed'

export interface DocItem {
  id: number
  file_name: string
  version: string
  source: string
  chunk_count: number
  sync_status: SyncStatus
  owner_id: number | null
  is_public: boolean
  /** 下载量（后端可能未提供，缺失时前端按 0 处理） */
  download_count?: number
  /** 最近更新时间（后端可能未提供，缺失时前端按 id 兜底倒序） */
  updated_at?: string
}

export interface QueueStats {
  enabled: boolean
  stream_len: number
  pending: number
  dead_letter: number
  inflight: number
  [k: string]: unknown
}

export interface QueuePendingItem {
  msg_id: string
  path: string
  file_name: string
  owner_id: number | null
  is_public: boolean
  retries: number
  enqueued_at: string
}

export interface QueueInflightItem {
  path: string
  file_name: string
}

export interface QueueDeadItem {
  msg_id: string
  path: string
  file_name: string
  owner_id: number | null
  is_public: boolean
  error: string
  origin: string
  dead_at: string
}

/** 批量上传接口逐文件结果（/api/documents/upload） */
export interface BatchUploadItem {
  file_name: string
  status: 'processing' | 'error'
  source?: string
  is_public?: boolean
  message: string
}

export interface BatchUploadResult {
  status: string
  results: BatchUploadItem[]
  accepted: number
  failed: number
  message: string
}

/** SSE 流式事件（与后端 /api/chat/stream 产出的 JSON 一一对应） */
export type ChatStreamEvent =
  | { type: 'meta'; session_id: string | null; intent: string; query: string; sources: SourceItem[] }
  | { type: 'token'; text: string }
  | { type: 'done'; answer: string }
  | { type: 'answer'; session_id: string | null; intent: string; query: string | null; answer: string; sources: SourceItem[] }
  | { type: 'error'; session_id?: string | null; message: string }

/** 带 JWT 的 fetch 封装：401 自动登出并回登录页。JSON 响应自动解析。 */
export async function api<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
  const auth = useAuthStore()
  const headers = new Headers(options.headers)
  if (auth.token) headers.set('Authorization', `Bearer ${auth.token}`)
  const res = await fetch(path, { ...options, headers })
  if (res.status === 401) {
    auth.logout()
    window.location.href = '/login'
    throw new Error('登录已过期')
  }
  if (!res.ok) {
    const data = (await res.json().catch(() => null)) as unknown
    throw new Error(detailMessage(data, res.status))
  }
  return (await res.json()) as T
}

function decodeFrame(frame: string): ChatStreamEvent | null {
  if (!frame.startsWith('data: ')) return null
  try {
    return JSON.parse(frame.slice(6)) as ChatStreamEvent
  } catch {
    return null
  }
}

/** SSE 流式问答：逐事件回调 onEvent（fetch ReadableStream 解析，非 EventSource——POST 不支持）。 */
export async function streamChat(
  body: {
    content: string
    session_id: string | null
    /** 会话检索范围（新会话首问生效；已存在会话后端以库中为准） */
    retrieve_own_private?: boolean
    retrieve_own_public?: boolean
    retrieve_kb_public?: boolean
    retrieve_owner_ids?: number[]
  },
  onEvent: (evt: ChatStreamEvent) => void,
): Promise<void> {
  const auth = useAuthStore()
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${auth.token}`,
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const data = (await res.json().catch(() => null)) as unknown
    throw new Error(detailMessage(data, res.status))
  }

  const reader = res.body!.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, idx).trim()
      buffer = buffer.slice(idx + 2)
      if (frame.startsWith('data:')) {
        const evt = decodeFrame(frame)
        if (evt) onEvent(evt)
      }
    }
  }
}


/** 历史会话元信息（MySQL chat_sessions，侧边栏列表） */
export interface ChatSessionInfo {
  session_id: string
  title: string
  message_count: number
  last_message_at: string | null
  /** 最后一条用户消息摘要（过长已截断） */
  last_message_preview: string
  created_at: string | null
  updated_at: string | null
  /** 会话检索范围：是否检索自己的私有文档 */
  retrieve_own_private: boolean
  /** 会话检索范围：是否检索自己的公开文档 */
  retrieve_own_public: boolean
  /** 会话检索范围：是否检索知识库里的公开文档 */
  retrieve_kb_public: boolean
  /** 会话检索范围：指定用户的公开文档（与 retrieve_kb_public 互斥） */
  retrieve_owner_ids: number[]
}

/** 会话内一条消息（完整消息从 Postgres checkpoint 加载） */
export interface ChatMessageItem {
  role: 'user' | 'assistant'
  content: string
  sources?: SourceItem[]
  intent?: string | null
  created_at?: string | null
}

export interface ChatSessionDetail {
  session_id: string
  title: string
  messages: ChatMessageItem[]
  retrieve_own_private: boolean
  retrieve_own_public: boolean
  retrieve_kb_public: boolean
  retrieve_owner_ids: number[]
  retrieve_owner_names: string[]
}

/** 历史会话列表（按最近活跃时间倒序） */
export async function listChatSessions(): Promise<ChatSessionInfo[]> {
  return api<ChatSessionInfo[]>('/api/chat/sessions')
}

/** 点进会话后加载完整消息 */
export async function getChatSession(sessionId: string): Promise<ChatSessionDetail> {
  return api<ChatSessionDetail>(`/api/chat/sessions/${encodeURIComponent(sessionId)}`)
}

export async function renameChatSession(sessionId: string, title: string): Promise<ChatSessionInfo> {
  return api<ChatSessionInfo>(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
}

export async function deleteChatSession(sessionId: string): Promise<{ status: string; session_id: string }> {
  return api<{ status: string; session_id: string }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  })
}

/** 个人详情（GET /api/users/{user_id}/profile） */
export interface ProfileInfo {
  id: number
  username: string
  role: 'admin' | 'user'
  created_at: string | null
  is_self: boolean
}

/** 查看任意用户个人详情（他人仅公开字段，is_self 区分是否本人） */
export async function getUserProfile(userId: number | string): Promise<ProfileInfo> {
  return api<ProfileInfo>(`/api/users/${encodeURIComponent(userId)}/profile`)
}

/** 修改密码（成功后需重新登录） */
export async function changePassword(oldPassword: string, newPassword: string): Promise<{ message: string }> {
  return api<{ message: string }>('/api/auth/change-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  })
}

/** 提交账号删除请求（进入删除队列，删除完成后无法登录） */
export async function deleteAccount(): Promise<{ status: string; queued: boolean; message: string }> {
  return api<{ status: string; queued: boolean; message: string }>('/api/auth/delete-account', {
    method: 'POST',
  })
}

/** 用户搜索结果（/api/users/search，供问答页「指定用户的公开文档」多选器） */
export interface UserSearchItem {
  id: number
  username: string
  role: 'admin' | 'user'
}

/** 按用户名关键字搜索 active 用户（排除自己），最多 limit 条 */
export async function searchUsers(q: string, limit = 20): Promise<UserSearchItem[]> {
  return api<UserSearchItem[]>(`/api/users/search?q=${encodeURIComponent(q)}&limit=${limit}`)
}
