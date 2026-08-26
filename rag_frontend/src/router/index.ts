import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('../views/LoginView.vue') },
    { path: '/', name: 'chat', component: () => import('../views/ChatView.vue') },
    { path: '/documents', name: 'documents', component: () => import('../views/DocumentsView.vue') },
    { path: '/knowledge', name: 'knowledge', component: () => import('../views/KnowledgeBaseView.vue') },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

// 已登录用户访问登录页时直接回到问答首页
router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.name === 'login' && auth.isLoggedIn) {
    return { name: 'chat' }
  }
  return true
})

export default router
