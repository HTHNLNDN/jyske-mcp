<script setup>
import ProgressBar from './ProgressBar.vue'

// Presentational only — mirrors BudgetCard.vue's structure (stipple header,
// hairline border, mono captions, tabular-nums amounts).
defineProps({
  goals: { type: Array, required: true },
})

// No accent color for state — behind/overdue reads as bold/solid emphasis,
// ahead/on_track/complete (and the unparseable-deadline "unknown" case) stay
// muted/fog, matching BudgetCard's over/warning idiom and SettingsPanel's
// connection-status pill treatment.
function isUrgent(goal) {
  const status = goal.pace?.status
  return status === 'behind' || status === 'overdue'
}

function statusLabel(goal) {
  const status = goal.pace?.status
  if (!status || status === 'unknown') return null
  return status.replace('_', ' ')
}

function progressPercent(goal) {
  if (goal.pace?.pct_complete != null) return goal.pace.pct_complete
  if (goal.target_amount > 0) return (goal.current_amount / goal.target_amount) * 100
  return 0
}

function progressVariant(goal) {
  return isUrgent(goal) ? 'over' : 'ok'
}

// Concrete time-remaining caption per pace.status — see architect's spec for
// the exact wording per branch.
function timeCaption(goal) {
  const pace = goal.pace
  if (!pace || pace.status === 'unknown') {
    if (goal.deadline) return `On pace to finish ~${goal.deadline}`
    return 'No deadline set'
  }
  if (pace.status === 'complete') return 'Goal reached'
  if (pace.status === 'overdue') {
    const remaining = Math.max(0, (goal.target_amount ?? 0) - (goal.current_amount ?? 0))
    return `Deadline passed · ${remaining.toLocaleString('da-DK')} kr to go`
  }
  if (pace.days_remaining != null && pace.required_daily != null) {
    return `${pace.days_remaining} days left · need ${pace.required_daily.toLocaleString('da-DK')} kr/day`
  }
  if (pace.projected_completion_date) {
    return `On pace to finish ~${pace.projected_completion_date}`
  }
  return 'No deadline set'
}
</script>

<template>
  <div
    class="relative w-full bg-paper border border-hairline rounded overflow-hidden"
  >
    <div class="relative px-4 pt-3 pb-2.5 border-b border-hairline flex items-baseline gap-1.5">
      <span class="text-[11px] font-mono font-semibold tracking-[0.12em] uppercase text-ink">Goals</span>
    </div>
    <div class="relative px-4 py-3 space-y-4">
      <div v-for="goal in goals" :key="goal.id">
        <div class="flex justify-between items-baseline mb-1.5">
          <span class="text-sm text-ink">{{ goal.name }}</span>
          <span
            v-if="statusLabel(goal)"
            class="text-xs font-mono tabular-nums uppercase tracking-[0.06em]"
            :class="isUrgent(goal) ? 'text-ink font-semibold' : 'text-fog'"
          >{{ statusLabel(goal) }}</span>
        </div>
        <ProgressBar :percent="progressPercent(goal)" :variant="progressVariant(goal)" />
        <div class="mt-1 flex justify-between items-baseline text-xs font-mono">
          <span class="text-fog/70">{{ timeCaption(goal) }}</span>
          <span class="text-fog/70 tabular-nums flex-shrink-0 pl-2">
            {{ goal.current_amount.toLocaleString('da-DK') }} / {{ goal.target_amount.toLocaleString('da-DK') }} DKK
          </span>
        </div>
      </div>
    </div>
  </div>
</template>
