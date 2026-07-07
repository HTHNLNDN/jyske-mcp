import { markRaw } from 'vue'
import BudgetsWidget from '../components/dashboard/BudgetsWidget.vue'
import GoalsWidget from '../components/dashboard/GoalsWidget.vue'
import TipWidget from '../components/dashboard/TipWidget.vue'

// span is 1 | 2 (default 1) for a future full-width widget in the 2-column
// grid — all three current widgets are span: 1.
export const dashboardWidgets = [
  { id: 'budgets', component: markRaw(BudgetsWidget), span: 1 },
  { id: 'goals', component: markRaw(GoalsWidget), span: 1 },
  { id: 'tip', component: markRaw(TipWidget), span: 1 },
]
