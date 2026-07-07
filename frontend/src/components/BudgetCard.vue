<script setup>
import { ref, reactive } from 'vue'
import ProgressBar from './dashboard/ProgressBar.vue'
import { useBudgetsStore } from '../stores/budgets'

defineProps({
  budgets: { type: Array, required: true },
})

const store = useBudgetsStore()

// Single-open accordion at the top level — keeps the card compact, only one
// category's breakdown visible at a time.
const openCat = ref(null)
// Multi-open at the mid-category level — sub-rows are small, no need to
// collapse siblings when opening another.
const openMid = reactive({})

// Recategorize picker — keyed by transaction id, mirrors the openMid keying
// pattern above. `pickers[tx.id]` holds the per-row draft selection/status so
// several rows can have their picker open at once without clobbering state.
const openFlag = reactive({})
const pickers = reactive({})

function midKey(category, entry) {
  return `${category}::${entry.uncategorized ? '__uncat__' : entry.category_mid}`
}

function toggleFlag(tx) {
  const willOpen = !openFlag[tx.id]
  openFlag[tx.id] = willOpen
  if (willOpen) {
    store.loadCategoryTree()
    pickers[tx.id] = { top: '', mid: '', saving: false, error: '', done: false }
  }
}

function onPickerTopChange(tx) {
  // Changing the top-level category invalidates whatever sub-category was
  // picked for the previous top — the mid <select> options are recomputed
  // from store.categoryTree[selectedTop].
  pickers[tx.id].mid = ''
}

async function savePicker(tx) {
  const picker = pickers[tx.id]
  if (!picker || !picker.top || !picker.mid || picker.saving) return
  picker.saving = true
  picker.error = ''
  const res = await store.recategorize({
    transactionId: tx.id,
    categoryTop: picker.top,
    categoryMid: picker.mid,
  })
  picker.saving = false
  if (!res.ok) {
    picker.error = res.data?.detail || 'Could not save — check your connection and try again.'
    return
  }
  openFlag[tx.id] = false
  // If the currently open top-level category was on either side of the
  // move, reload its breakdown so the accordion visually reflects the
  // change (e.g. the transaction disappearing from its old sub-category).
  if (openCat.value && (openCat.value === res.data.old_category_top || openCat.value === res.data.new_category_top)) {
    store.loadBreakdown(openCat.value)
  }
}

function toggleCat(row) {
  if (row.category_mid !== null) return // scope guard: mid-category rows never expand
  const isOpen = openCat.value === row.category
  openCat.value = isOpen ? null : row.category
  if (!isOpen) store.loadBreakdown(row.category)
}

function toggleMid(category, entry) {
  const key = midKey(category, entry)
  const willOpen = !openMid[key]
  openMid[key] = willOpen
  if (willOpen) store.loadLineItems(category, entry.category_mid, entry.uncategorized)
}

function currentMonth() {
  return new Date().toLocaleString('en-US', { month: 'long' })
}
</script>

<template>
  <div
    class="relative w-full bg-paper border border-hairline rounded overflow-hidden"
  >
    <div class="relative px-4 pt-3 pb-2.5 border-b border-hairline flex items-baseline gap-1.5">
      <span class="text-[11px] font-mono font-semibold tracking-[0.12em] uppercase text-ink">Budget Status</span>
      <span class="text-[11px] font-mono text-fog">· {{ currentMonth() }}</span>
    </div>
    <div class="relative px-4 py-3 space-y-4">
      <div v-for="(row, ri) in budgets" :key="ri">
        <button
          type="button"
          class="w-full text-left"
          :class="row.category_mid === null ? 'cursor-pointer' : 'cursor-default'"
          @click="toggleCat(row)"
        >
          <div class="flex justify-between items-baseline mb-1.5 gap-2">
            <span class="flex items-center gap-1 text-sm text-ink min-w-0">
              <span
                v-if="row.category_mid === null"
                class="font-mono text-fog transition-transform duration-150 flex-shrink-0"
                :class="openCat === row.category ? 'rotate-90' : ''"
              >&rsaquo;</span>
              <span class="truncate">{{ row.category }}</span>
            </span>
            <span
              class="flex-shrink-0 text-xs font-mono tabular-nums"
              :class="row.status === 'over' ? 'text-ink font-semibold' : row.status === 'warning' ? 'text-ink' : 'text-fog'"
            >{{ row.status === 'over' ? 'OVER' : row.percent + '%' }}</span>
          </div>
          <ProgressBar
            :percent="row.percent"
            :variant="row.status === 'over' ? 'over' : row.status === 'warning' ? 'warning' : 'ok'"
          />
          <div class="mt-1 text-right text-xs font-mono text-fog/70 tabular-nums">
            {{ row.spent.toLocaleString('da-DK') }} / {{ row.limit.toLocaleString('da-DK') }} DKK
          </div>
        </button>

        <!-- Mid-category breakdown -->
        <div
          v-if="row.category_mid === null && openCat === row.category"
          class="mt-2 pl-3 border-l border-hairline space-y-2"
        >
          <p v-if="store.breakdowns[row.category]?.loading" class="text-xs text-fog/70">Loading…</p>
          <template v-else-if="store.breakdowns[row.category]?.data">
            <div v-for="entry in store.breakdowns[row.category].data.breakdown" :key="midKey(row.category, entry)">
              <button
                type="button"
                class="w-full text-left flex justify-between items-baseline gap-2 py-0.5"
                @click="toggleMid(row.category, entry)"
              >
                <span class="flex items-center gap-1 text-xs min-w-0" :class="entry.uncategorized ? 'text-fog italic' : 'text-ink'">
                  <span
                    class="font-mono text-fog transition-transform duration-150 flex-shrink-0"
                    :class="openMid[midKey(row.category, entry)] ? 'rotate-90' : ''"
                  >&rsaquo;</span>
                  <span class="truncate">{{ entry.label }}</span>
                </span>
                <span class="flex-shrink-0 font-mono text-xs tabular-nums text-fog">
                  {{ entry.spent.toLocaleString('da-DK') }} DKK · {{ entry.count }}
                </span>
              </button>

              <!-- Line items -->
              <div
                v-if="openMid[midKey(row.category, entry)]"
                class="mt-1 pl-3 border-l border-hairline space-y-1"
              >
                <p v-if="store.lineItems[midKey(row.category, entry)]?.loading" class="text-xs text-fog/70">Loading…</p>
                <template v-else-if="store.lineItems[midKey(row.category, entry)]?.data">
                  <p v-if="!store.lineItems[midKey(row.category, entry)].data.items.length" class="text-xs text-fog/70">
                    No transactions.
                  </p>
                  <div v-for="tx in store.lineItems[midKey(row.category, entry)].data.items" :key="tx.id">
                    <div class="flex justify-between items-baseline gap-2">
                      <span class="text-[11px] font-mono text-fog truncate">{{ tx.date }}</span>
                      <span class="text-xs text-ink truncate flex-1 min-w-0">{{ tx.description }}</span>
                      <span class="flex-shrink-0 font-mono text-xs tabular-nums text-ink">
                        {{ tx.amount.toLocaleString('da-DK') }} {{ tx.currency }}
                      </span>
                      <button
                        type="button"
                        class="flex-shrink-0 w-4 h-4 flex items-center justify-center text-fog hover:text-ink transition-colors"
                        :aria-label="`Recategorize ${tx.description}`"
                        @click="toggleFlag(tx)"
                      >
                        <svg class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="1.5"
                             stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
                          <path d="M5 3v18M5 3h11l-2.5 4L16 11H5" />
                        </svg>
                      </button>
                    </div>

                    <!-- Recategorize picker -->
                    <div v-if="openFlag[tx.id]" class="mt-1 mb-1.5 pl-2 border-l border-hairline space-y-1.5">
                      <p v-if="store.categoryTreeLoading" class="text-[11px] text-fog/70">Loading categories…</p>
                      <template v-else>
                        <div class="flex gap-1.5">
                          <select
                            v-model="pickers[tx.id].top"
                            class="min-w-0 flex-1 text-[11px] font-mono bg-paperdim border border-hairline rounded px-1.5 py-1 text-ink"
                            @change="onPickerTopChange(tx)"
                          >
                            <option value="" disabled>Category…</option>
                            <option v-for="top in Object.keys(store.categoryTree ?? {})" :key="top" :value="top">{{ top }}</option>
                          </select>
                          <select
                            v-model="pickers[tx.id].mid"
                            class="min-w-0 flex-1 text-[11px] font-mono bg-paperdim border border-hairline rounded px-1.5 py-1 text-ink disabled:opacity-50"
                            :disabled="!pickers[tx.id].top"
                          >
                            <option value="" disabled>Sub-category…</option>
                            <option
                              v-for="mid in (store.categoryTree?.[pickers[tx.id].top] ?? [])"
                              :key="mid"
                              :value="mid"
                            >{{ mid }}</option>
                          </select>
                        </div>
                        <div class="flex items-center gap-2">
                          <button
                            type="button"
                            class="text-[11px] font-mono font-semibold bg-ink text-paper px-2.5 py-1 rounded transition-opacity disabled:opacity-40"
                            :disabled="!pickers[tx.id].top || !pickers[tx.id].mid || pickers[tx.id].saving"
                            @click="savePicker(tx)"
                          >{{ pickers[tx.id].saving ? 'Saving…' : 'Save' }}</button>
                          <button
                            type="button"
                            class="text-[11px] font-mono text-fog hover:text-ink transition-colors"
                            @click="openFlag[tx.id] = false"
                          >Cancel</button>
                        </div>
                        <p v-if="pickers[tx.id].error" class="text-[11px] text-ink font-semibold">{{ pickers[tx.id].error }}</p>
                      </template>
                    </div>
                  </div>
                </template>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>
