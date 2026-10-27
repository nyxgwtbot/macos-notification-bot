import { createRouter, createWebHistory } from '@ionic/vue-router';
import { RouteRecordRaw } from 'vue-router';
import DashboardView from '../views/DashboardView.vue';
import FinanceView from '../views/FinanceView.vue';
import ConfigView from '../views/ConfigView.vue';
import LogsView from '../views/LogsView.vue';
import ChatView from '../views/ChatView.vue';

const routes: Array<RouteRecordRaw> = [
  {
    path: '/',
    redirect: '/dashboard'
  },
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: DashboardView
  },
  {
    path: '/chat',
    name: 'Chat',
    component: ChatView
  },
  {
    path: '/finance',
    name: 'Finance',
    component: FinanceView
  },
  {
    path: '/config',
    name: 'Config',
    component: ConfigView
  },
  {
    path: '/logs',
    name: 'Logs',
    component: LogsView
  }
];

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes
});

export default router;