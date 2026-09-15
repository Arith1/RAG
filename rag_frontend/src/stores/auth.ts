import { defineStore } from 'pinia'
import { api } from '../api/client'

export interface UserInfo {
  id: number
  username: string
  role: 'admin' | 'user'
}

const TOKEN_KEY = 'rag_token'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) ?? '',
    user: null as UserInfo | null,
  }),
  getters: {
    isLoggedIn: (s) => !!s.token,
    isAdmin: (s) => s.user?.role === 'admin',
  },
  actions: {
    async login(username: string, password: string) {
      const form = new URLSearchParams({ username, password })
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: form,
      })
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { detail?: string }
        throw new Error(data.detail ?? (res.status === 401 ? '用户名或密码错误' : '登录失败'))
      }
      const data = (await res.json()) as { access_token: string; role: string }
      this.token = data.access_token
      localStorage.setItem(TOKEN_KEY, data.access_token)
      await this.fetchMe()
    },
    async register(username: string, password: string) {
      const res = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { detail?: string }
        throw new Error(data.detail ?? '注册失败')
      }
      // 注册成功后自动登录并进入问答首页
      await this.login(username, password)
    },
    async fetchMe() {
      this.user = await api<UserInfo>('/api/auth/me')
    },
    async logout(revoke = true) {
      const token = this.token
      try {
        if (token && revoke) {
          // keepalive + 短超时：优先让服务端吊销 token，同时避免退出按钮长时间卡住。
          await fetch('/api/auth/logout', {
            method: 'POST',
            headers: { Authorization: `Bearer ${token}` },
            keepalive: true,
            signal: AbortSignal.timeout(3000),
          })
        }
      } catch {
        // 网络异常仍执行本地退出；未成功吊销时 token 最迟在自身过期时间失效。
      } finally {
        this.token = ''
        this.user = null
        localStorage.removeItem(TOKEN_KEY)
      }
    },
  },
})
