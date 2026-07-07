import { createRouter, createWebHashHistory } from 'vue-router'
import DashboardView from '../views/DashboardView.vue'
import ChatView from '../views/ChatView.vue'

// Hash-based routing specifically — load-bearing, not a style choice: the
// backend's AuthMiddleware only exempts exact path "/" from the PIN check
// (app.py's _is_exempt), so a clean-URL deep-link/refresh to /dashboard or
// /chat would 401 before Vue Router ever runs. Hash routing keeps every
// request the server sees as literally "/", so no backend change is needed.
export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: DashboardView },
    { path: '/chat', name: 'chat', component: ChatView },
  ],
})
