<script setup>
import { onMounted, watch } from 'vue'
import { useBudgetsStore } from '../../stores/budgets'
import BudgetCard from '../BudgetCard.vue'

// Self-fetching container widget — reads its own store, owns its own
// loading/empty state, and reports back to the dashboard shell whether it
// ended up with real data once loaded (the shell has zero visibility into
// widget-internal state otherwise).
const emit = defineEmits(['loaded'])

const budgets = useBudgetsStore()

onMounted(() => budgets.load())

watch(() => budgets.loaded, (loaded) => {
  if (loaded) emit('loaded', budgets.budgets.length > 0)
}, { immediate: true })
</script>

<template>
  <!-- Loading state -->
  <div
    v-if="!budgets.loaded"
    class="relative w-full bg-paper border border-hairline rounded overflow-hidden"
  >
    <div class="relative px-4 pt-3 pb-2.5 border-b border-hairline">
      <span class="text-[11px] font-mono font-semibold tracking-[0.12em] uppercase text-ink">Budget Status</span>
    </div>
    <div class="relative px-4 py-4">
      <div class="h-2 w-2/3 bg-paperdim rounded"></div>
    </div>
  </div>

  <!-- Empty state -->
  <div
    v-else-if="budgets.budgets.length === 0"
    class="relative w-full bg-paper border border-hairline rounded overflow-hidden"
  >
    <div class="relative px-4 pt-3 pb-2.5 border-b border-hairline">
      <span class="text-[11px] font-mono font-semibold tracking-[0.12em] uppercase text-ink">Budget Status</span>
    </div>
    <div class="relative px-4 py-4">
      <p class="text-sm text-fog/70">No budgets set yet</p>
    </div>
  </div>

  <!-- Data state -->
  <BudgetCard v-else :budgets="budgets.budgets" />
</template>
