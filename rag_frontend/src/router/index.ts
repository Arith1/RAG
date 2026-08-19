import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('../views/LoginView.vue') },
    { path: '/', name: 'chat', component: () => import('../views/ChatView.vue') },
    { path: '/documents', name: 'documents', component: () => import('../views/DocumentsView.vue') },
  ],
})

// 路由守卫：未登录一律回登录页
router.beforeEach(() => {
  const auth = useAuthStore()
  if (!auth.token) {
    return { path: '/login' }
  }
})

export default router
