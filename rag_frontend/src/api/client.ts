import { useAuthStore } from '../stores/auth'

export interface SourceItem {
  index: number
  source: string | null
  score: number | null
  content: string
}

/** SSE 流式事件（与后端 chat_stream 产出的 JSON 一一对应） */
export type ChatStreamEvent =
  | { type: 'meta'; intent: string; query: string; sources: SourceItem[] }
  | { type: 'token'; text: string }
  | { type: 'done'; answer: string }
  | { type: 'answer'; intent: string; query: string | null; answer: string; sources: SourceItem[] }
  | { type: 'error'; message: string }

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
    const data = (await res.json().catch(() => ({}))) as { detail?: string }
    throw new Error(data.detail ?? `HTTP ${res.status}`)
  }
  return (await res.json()) as T
}

/** SSE 流式问答：逐事件回调 onEvent（fetch ReadableStream 解析，非 EventSource——POST 不支持）。 */
export async function streamChat(
  body: { content: string; session_id: string | null },
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
  if (!res.ok) throw new Error(`HTTP ${res.status}`)

  const reader = res.body!.getReader()
  const decoder = new TextDecoder('utf-8') // stream:true 处理跨帧半截字符
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, idx).trim()
      buffer = buffer.slice(idx + 2)
      if (frame.startsWith('data: ')) {
        onEvent(JSON.parse(frame.slice(6)) as ChatStreamEvent)
      }
    }
  }
}
