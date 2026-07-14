<script setup>
import { onMounted, watch, ref } from 'vue'
import { useBudgetsStore } from '../../stores/budgets'
import BudgetCard from '../BudgetCard.vue'
import BudgetForm from '../BudgetForm.vue'

// Self-fetching container widget — reads its own store, owns its own
// loading/empty state, and reports back to the dashboard shell whether it
// ended up with real data once loaded (the shell has zero visibility into
// widget-internal state otherwise).
const emit = defineEmits(['loaded'])

const budgets = useBudgetsStore()

// Toggles the inline "new budget" form — present above every state (loading,
// empty, data) so the empty state gets a call to action instead of being a
// dead end, and existing budgets stay one click away from adding another.
const showForm = ref(false)

onMounted(() => budgets.load())

watch(() => budgets.loaded, (loaded) => {
  if (loaded) emit('loaded', budgets.budgets.length > 0)
}, { immediate: true })

function onSaved() {
  showForm.value = false
  // budgets.createBudget() already reloads status internally — nothing else
  // to do here beyond closing the form.
}
</script>

<template>
  <!-- Single wrapping root (rather than the old top-level v-if/else-if/else
       fragment) so CardHalo's stipple halo continues to frame exactly one
       block, now inclusive of the new-budget toggle/form. -->
  <div>
    <div class="flex justify-end mb-2">
      <button
        type="button"
        class="text-[11px] font-mono font-semibold transition-opacity"
        :class="showForm ? 'text-fog hover:text-ink' : 'text-ink hover:opacity-70'"
        @click="showForm = !showForm"
      >{{ showForm ? 'Cancel' : '+ New budget' }}</button>
    </div>

    <div
      v-if="showForm"
      class="relative w-full bg-paper border border-hairline rounded overflow-hidden mb-3"
    >
      <BudgetForm @saved="onSaved" @cancelled="showForm = false" />
    </div>

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
  </div>
</template>
