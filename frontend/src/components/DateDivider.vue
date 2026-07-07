<script setup>
import { computed } from 'vue'

const props = defineProps({
  label: { type: String, required: true },
})

// "Today" anchors the boundary between history and the live session, so it
// gets bold weight instead of the usual muted fog — no accent color in this
// system, emphasis comes from weight.
const isToday = computed(() => props.label === 'Today')
</script>

<template>
  <div
    class="flex items-center gap-3 my-4"
    :class="isToday ? 'text-ink font-semibold' : 'text-fog'"
  >
    <span class="flex-1 h-px bg-current opacity-30"></span>
    <span class="text-xs font-mono tracking-wide">{{ label }}</span>
    <span class="flex-1 h-px bg-current opacity-30"></span>
  </div>
</template>
