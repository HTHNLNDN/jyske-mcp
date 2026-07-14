<script setup>
import { reactive, computed, onMounted } from 'vue'
import { useBudgetsStore } from '../stores/budgets'

// Creates a budget — and, since POST /budgets upserts on
// (category_top, category_mid, period), implicitly edits one too if a
// matching row already exists. Mirrors BudgetCard.vue's recategorize
// picker for select/select/save-cancel shape, but here the mid-category is
// optional (a budget scoped to a whole top-level category is the common
// case) so it gets a blank "whole category" option instead of being
// disabled-until-required like the recategorize picker's mid select.
const emit = defineEmits(['saved', 'cancelled'])

const store = useBudgetsStore()

const form = reactive({
  categoryTop: '',
  categoryMid: '',
  limitAmount: '',
  period: 'monthly',
  saving: false,
  error: '',
})

onMounted(() => store.loadCategoryTree())

function onTopChange() {
  // Same invalidation rule as BudgetCard's recategorize picker: switching
  // the top-level category invalidates whatever mid was picked before.
  form.categoryMid = ''
}

const canSave = computed(() => !form.saving && !!form.categoryTop && Number(form.limitAmount) > 0)

async function save() {
  if (!canSave.value) return
  form.saving = true
  form.error = ''
  const res = await store.createBudget({
    categoryTop: form.categoryTop,
    categoryMid: form.categoryMid || null,
    limitAmount: Number(form.limitAmount),
    period: form.period,
  })
  form.saving = false
  if (!res.ok) {
    form.error = res.data?.detail || 'Could not save — check your connection and try again.'
    return
  }
  emit('saved')
}

function cancel() {
  emit('cancelled')
}
</script>

<template>
  <div class="relative px-4 pt-3 pb-2.5 border-b border-hairline">
    <span class="text-[11px] font-mono font-semibold tracking-[0.12em] uppercase text-ink">New Budget</span>
  </div>
  <div class="relative px-4 py-3 space-y-2.5">
    <p v-if="store.categoryTreeLoading" class="text-xs text-fog/70">Loading categories…</p>
    <template v-else>
      <div class="flex gap-1.5">
        <select
          v-model="form.categoryTop"
          class="min-w-0 flex-1 text-[11px] font-mono bg-paperdim border border-hairline rounded px-1.5 py-1 text-ink"
          @change="onTopChange"
        >
          <option value="" disabled>Category…</option>
          <option v-for="top in Object.keys(store.categoryTree ?? {})" :key="top" :value="top">{{ top }}</option>
        </select>
        <select
          v-model="form.categoryMid"
          class="min-w-0 flex-1 text-[11px] font-mono bg-paperdim border border-hairline rounded px-1.5 py-1 text-ink disabled:opacity-50"
          :disabled="!form.categoryTop"
        >
          <option value="">{{ form.categoryTop ? `— all of ${form.categoryTop} —` : 'Sub-category…' }}</option>
          <option
            v-for="mid in (store.categoryTree?.[form.categoryTop] ?? [])"
            :key="mid"
            :value="mid"
          >{{ mid }}</option>
        </select>
      </div>

      <div class="flex gap-1.5">
        <input
          v-model="form.limitAmount"
          type="number"
          min="1"
          step="1"
          placeholder="Limit (DKK)"
          class="min-w-0 flex-1 text-[11px] font-mono bg-paperdim border border-hairline rounded px-1.5 py-1 text-ink tabular-nums"
        />
        <select
          v-model="form.period"
          class="min-w-0 flex-1 text-[11px] font-mono bg-paperdim border border-hairline rounded px-1.5 py-1 text-ink"
        >
          <option value="monthly">Monthly</option>
          <option value="weekly">Weekly</option>
        </select>
      </div>

      <div class="flex items-center gap-2">
        <button
          type="button"
          class="text-[11px] font-mono font-semibold bg-ink text-paper px-2.5 py-1 rounded transition-opacity disabled:opacity-40"
          :disabled="!canSave"
          @click="save"
        >{{ form.saving ? 'Saving…' : 'Save' }}</button>
        <button
          type="button"
          class="text-[11px] font-mono text-fog hover:text-ink transition-colors"
          @click="cancel"
        >Cancel</button>
      </div>
      <p v-if="form.error" class="text-[11px] text-ink font-semibold">{{ form.error }}</p>
    </template>
  </div>
</template>
