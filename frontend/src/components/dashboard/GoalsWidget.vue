<script setup>
import { onMounted, watch } from 'vue'
import { useGoalsStore } from '../../stores/goals'
import GoalCard from './GoalCard.vue'

// Mirrors BudgetsWidget.vue's shape — self-fetching container, reports back
// whether it ended up with real data once loaded.
const emit = defineEmits(['loaded'])

const goals = useGoalsStore()

onMounted(() => goals.load())

watch(() => goals.loaded, (loaded) => {
  if (loaded) emit('loaded', goals.goals.length > 0)
}, { immediate: true })
</script>

<template>
  <!-- Loading state -->
  <div
    v-if="!goals.loaded"
    class="relative w-full bg-paper border border-hairline rounded overflow-hidden"
  >
    <div class="relative px-4 pt-3 pb-2.5 border-b border-hairline">
      <span class="text-[11px] font-mono font-semibold tracking-[0.12em] uppercase text-ink">Goals</span>
    </div>
    <div class="relative px-4 py-4">
      <div class="h-2 w-2/3 bg-paperdim rounded"></div>
    </div>
  </div>

  <!-- Empty state -->
  <div
    v-else-if="goals.goals.length === 0"
    class="relative w-full bg-paper border border-hairline rounded overflow-hidden"
  >
    <div class="relative px-4 pt-3 pb-2.5 border-b border-hairline">
      <span class="text-[11px] font-mono font-semibold tracking-[0.12em] uppercase text-ink">Goals</span>
    </div>
    <div class="relative px-4 py-4">
      <p class="text-sm text-fog/70">No goals set yet</p>
    </div>
  </div>

  <!-- Data state -->
  <GoalCard v-else :goals="goals.goals" />
</template>
