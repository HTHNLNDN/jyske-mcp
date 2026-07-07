<script setup>
import { ref, computed } from 'vue'
import { dashboardWidgets } from '../dashboard/widgets'
import CardHalo from '../components/CardHalo.vue'

// Empty-dashboard tracking: the shell deliberately has no visibility into
// each widget's internal data state (dumb shell, self-fetching widgets), so
// each container widget reports whether it ended up with real data — via a
// single `@loaded` event carrying a boolean — once its own load() resolves.
// `reports` maps widget id -> hasData. Once every registered widget has
// reported in, and every one of them reported false, the grid is swapped for
// one consolidated fallback message instead of a page of individually-empty
// cards.
const reports = ref({})

function handleLoaded(id, hasData) {
  reports.value = { ...reports.value, [id]: hasData }
}

const allReported = computed(() =>
  dashboardWidgets.every(w => w.id in reports.value)
)

const allEmpty = computed(() =>
  allReported.value && dashboardWidgets.every(w => reports.value[w.id] === false)
)
</script>

<template>
  <div class="flex-1 overflow-y-auto px-4 py-4">
    <div
      v-if="allEmpty"
      class="h-full flex items-center justify-center px-6"
    >
      <p class="text-fog/60 text-sm text-center leading-relaxed">
        No widgets found — start using the app, and the page will self-populate.
      </p>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 auto-rows-min" v-else>
      <div v-for="w in dashboardWidgets" :key="w.id" :class="w.span === 2 ? 'col-span-2' : 'col-span-1'">
        <CardHalo>
          <component :is="w.component" @loaded="handleLoaded(w.id, $event)" />
        </CardHalo>
      </div>
    </div>
  </div>
</template>
