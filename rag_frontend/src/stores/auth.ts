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
    async fetchMe() {
      this.user = await api<UserInfo>('/api/auth/me')
    },
    logout() {
      this.token = ''
      this.user = null
      localStorage.removeItem(TOKEN_KEY)
    },
  },
})
