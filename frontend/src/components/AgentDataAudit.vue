<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import BudgetCard from './BudgetCard.vue'
import GoalCard from './dashboard/GoalCard.vue'

const props = defineProps({
  agentId: { type: String, required: true },
})

const data = ref(null)
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.getAuditData(props.agentId)
  } catch (err) {
    console.error('Could not load audit data:', err)
    error.value = 'Could not load audit data — check your connection and try again.'
  } finally {
    loading.value = false
  }
})

const profileEntries = computed(() => Object.entries(data.value?.profile ?? {}))
const summaries      = computed(() => data.value?.summaries ?? [])
const budgets        = computed(() => data.value?.budgets ?? [])
const goals          = computed(() => data.value?.goals ?? [])
const tips           = computed(() => data.value?.tips ?? [])
const transactions   = computed(() => data.value?.transactions?.items ?? [])
const transactionCount = computed(() => data.value?.transactions?.count ?? 0)

// Feedback-status weight/fill, matching BankConnection's no-accent pill
// idiom — urgency/emphasis reads through weight and fill density, not color.
const TIP_PILL = {
  pending:   { label: 'Pending',   class: 'border border-hairline border-dashed text-fog' },
  evaluated: { label: 'Evaluated', class: 'border border-hairline text-fog' },
  accepted:  { label: 'Accepted',  class: 'border border-ink text-ink font-semibold' },
  rejected:  { label: 'Rejected',  class: 'bg-ink text-paper font-semibold' },
}
function tipPill(status) {
  return TIP_PILL[status] ?? TIP_PILL.pending
}

function formatValue(v) {
  if (v == null) return '—'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

// created_at columns are unix seconds (time.time()); tip_date/date columns
// are already plain ISO date strings — handle both without guessing wrong.
function formatEpoch(sec) {
  if (sec == null) return '—'
  const d = new Date(sec * 1000)
  if (Number.isNaN(d.getTime())) return String(sec)
  return d.toLocaleString('da-DK', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function formatDate(value) {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleDateString('da-DK', { year: 'numeric', month: 'short', day: 'numeric' })
}

function downloadJson() {
  if (!data.value) return
  const blob = new Blob([JSON.stringify(data.value, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${props.agentId}-audit-${new Date().toISOString().slice(0, 10)}.json`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <h2 class="text-sm font-condensed font-medium text-ink mb-3">Your data</h2>

  <div v-if="loading" class="text-xs font-mono text-fog/60 py-4">Loading audit data…</div>

  <div v-else-if="error" class="mb-4 text-xs text-ink border border-hairline px-3 py-2">{{ error }}</div>

  <template v-else-if="data">
    <button
      @click="downloadJson"
      class="w-full mb-4 text-sm font-medium bg-ink text-paper py-2.5 rounded transition-opacity hover:opacity-90"
    >Download JSON</button>

    <div class="border-t border-hairline">
      <!-- Profile -->
      <details class="group border-b border-hairline" open>
        <summary class="cursor-pointer select-none list-none py-3 flex items-center justify-between gap-2">
          <span class="flex items-center gap-2 text-sm text-ink">
            <svg class="w-3 h-3 flex-shrink-0 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>
            Profile
          </span>
          <span class="font-mono text-xs text-fog tabular-nums">{{ profileEntries.length }}</span>
        </summary>
        <div class="pb-4 pl-5">
          <p v-if="!profileEntries.length" class="text-xs font-mono text-fog/60">No profile data.</p>
          <ul v-else class="space-y-2">
            <li v-for="[key, value] in profileEntries" :key="key" class="text-xs">
              <span class="block font-mono tracking-[0.08em] uppercase text-fog mb-0.5">{{ key }}</span>
              <span class="text-ink break-words">{{ formatValue(value) }}</span>
            </li>
          </ul>
        </div>
      </details>

      <!-- Summaries -->
      <details class="group border-b border-hairline">
        <summary class="cursor-pointer select-none list-none py-3 flex items-center justify-between gap-2">
          <span class="flex items-center gap-2 text-sm text-ink">
            <svg class="w-3 h-3 flex-shrink-0 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>
            Summaries
          </span>
          <span class="font-mono text-xs text-fog tabular-nums">{{ summaries.length }}</span>
        </summary>
        <div class="pb-4 pl-5">
          <p v-if="!summaries.length" class="text-xs font-mono text-fog/60">No session summaries yet.</p>
          <ul v-else class="space-y-3">
            <li v-for="(row, i) in summaries" :key="i">
              <span class="block text-[11px] font-mono text-fog mb-0.5">{{ formatEpoch(row.created_at) }}</span>
              <span class="text-xs text-ink">{{ row.summary }}</span>
            </li>
          </ul>
        </div>
      </details>

      <!-- Budgets -->
      <details class="group border-b border-hairline">
        <summary class="cursor-pointer select-none list-none py-3 flex items-center justify-between gap-2">
          <span class="flex items-center gap-2 text-sm text-ink">
            <svg class="w-3 h-3 flex-shrink-0 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>
            Budgets
          </span>
          <span class="font-mono text-xs text-fog tabular-nums">{{ budgets.length }}</span>
        </summary>
        <div class="pb-4 pl-5 pr-px">
          <p v-if="!budgets.length" class="text-xs font-mono text-fog/60">No active budgets.</p>
          <BudgetCard v-else :budgets="budgets" />
        </div>
      </details>

      <!-- Goals -->
      <details class="group border-b border-hairline">
        <summary class="cursor-pointer select-none list-none py-3 flex items-center justify-between gap-2">
          <span class="flex items-center gap-2 text-sm text-ink">
            <svg class="w-3 h-3 flex-shrink-0 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>
            Goals
          </span>
          <span class="font-mono text-xs text-fog tabular-nums">{{ goals.length }}</span>
        </summary>
        <div class="pb-4 pl-5 pr-px">
          <p v-if="!goals.length" class="text-xs font-mono text-fog/60">No active goals.</p>
          <GoalCard v-else :goals="goals" />
        </div>
      </details>

      <!-- Tips -->
      <details class="group border-b border-hairline">
        <summary class="cursor-pointer select-none list-none py-3 flex items-center justify-between gap-2">
          <span class="flex items-center gap-2 text-sm text-ink">
            <svg class="w-3 h-3 flex-shrink-0 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>
            Tips
          </span>
          <span class="font-mono text-xs text-fog tabular-nums">{{ tips.length }}</span>
        </summary>
        <div class="pb-4 pl-5">
          <p v-if="!tips.length" class="text-xs font-mono text-fog/60">No tips recorded.</p>
          <ul v-else class="space-y-3">
            <li v-for="tip in tips" :key="tip.id" class="border-b border-hairline pb-3 last:border-0 last:pb-0">
              <div class="flex items-start justify-between gap-2 mb-1">
                <span class="text-[11px] font-mono text-fog">{{ tip.tip_date || formatEpoch(tip.created_at) }}<template v-if="tip.category_top"> · {{ tip.category_top }}</template></span>
                <span
                  class="flex-shrink-0 inline-flex items-center px-2 py-0.5 rounded text-[10px] font-mono tracking-[0.06em] uppercase"
                  :class="tipPill(tip.feedback_status).class"
                >{{ tipPill(tip.feedback_status).label }}</span>
              </div>
              <p class="text-xs text-ink">{{ tip.tip_text }}</p>
              <p v-if="tip.feedback_reason_text" class="mt-1 text-[11px] font-mono text-fog">"{{ tip.feedback_reason_text }}"</p>
            </li>
          </ul>
        </div>
      </details>

      <!-- Transactions -->
      <details class="group">
        <summary class="cursor-pointer select-none list-none py-3 flex items-center justify-between gap-2">
          <span class="flex items-center gap-2 text-sm text-ink">
            <svg class="w-3 h-3 flex-shrink-0 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>
            Transactions
          </span>
          <span class="font-mono text-xs text-fog tabular-nums">{{ transactionCount }}</span>
        </summary>
        <div class="pb-4">
          <p v-if="!transactions.length" class="pl-5 text-xs font-mono text-fog/60">No transactions recorded.</p>
          <!-- A literal wide table doesn't fit this panel's w-80 width without
               horizontal overflow/clipping — a stacked two-line row (like the
               Accounts list in BankConnection) reads far better at this
               width: description+amount on one line, date/category as a
               mono caption underneath. -->
          <ul v-else class="max-h-80 overflow-y-auto border-t border-hairline divide-y divide-hairline">
            <li v-for="tx in transactions" :key="tx.id" class="py-2 flex items-start justify-between gap-3">
              <div class="min-w-0">
                <p class="text-xs text-ink truncate">{{ tx.description || '—' }}</p>
                <p class="text-[11px] font-mono text-fog truncate">
                  {{ formatDate(tx.date) }} · {{ tx.category_top || 'Uncategorized' }}<template v-if="tx.category_leaf"> · {{ tx.category_leaf }}</template>
                </p>
              </div>
              <span
                class="flex-shrink-0 font-mono text-xs tabular-nums"
                :class="tx.direction === 'CRDT' ? 'text-ink font-semibold' : 'text-fog'"
              >{{ tx.direction === 'CRDT' ? '+' : '−' }}{{ Math.abs(tx.amount ?? 0).toLocaleString('da-DK') }} {{ tx.currency }}</span>
            </li>
          </ul>
        </div>
      </details>
    </div>
  </template>
  </template>
