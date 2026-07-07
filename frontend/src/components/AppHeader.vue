<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAgentsStore } from '../stores/agents'

const agents = useAgentsStore()
const route = useRoute()

// The dashboard route is deliberately agent-agnostic, so it always reads
// "Home" regardless of which agent is active; every other route (currently
// just /chat) shows the active agent's name.
const title = computed(() =>
  route.name === 'dashboard' ? 'Home' : (agents.activeAgent?.name ?? 'Finance')
)

defineProps({
  // True when the Enable Banking connection needs the user's attention
  // (expiring soon or already expired) — surfaced as a small dot on the gear.
  needsAttention: { type: Boolean, default: false },
})

const emit = defineEmits(['toggle-menu', 'open-settings'])
</script>

<template>
  <!-- Header — 64px content + safe area for notch/Dynamic Island -->
  <header
    class="flex-shrink-0 flex items-center px-4 bg-paper border-b border-hairline pt-[env(safe-area-inset-top)]"
    style="min-height: calc(4rem + env(safe-area-inset-top));"
  >
    <button
      @click="emit('toggle-menu')"
      class="w-11 h-11 flex items-center justify-center text-fog hover:text-ink transition-colors"
      aria-label="Open menu"
    >
      <div class="flex flex-col gap-[5px]">
        <span class="block h-px w-[18px] bg-current rounded-full"></span>
        <span class="block h-px w-[18px] bg-current rounded-full"></span>
        <span class="block h-px w-[12px] bg-current rounded-full"></span>
      </div>
    </button>

    <span class="flex-1 text-center text-sm font-condensed font-medium text-ink">{{ title }}</span>

    <button
      @click="emit('open-settings')"
      class="relative w-11 h-11 flex items-center justify-center text-fog hover:text-ink transition-colors"
      aria-label="Settings"
    >
      <svg class="w-[18px] h-[18px]" fill="none" stroke="currentColor"
           stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
           viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="3"/>
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
      </svg>
      <!-- Attention badge — filled dot, no accent color: weight/fill carries the
           nudge rather than a warning hue, consistent with the rest of the app. -->
      <span
        v-if="needsAttention"
        class="absolute top-[9px] right-[9px] w-[7px] h-[7px] rounded-full bg-ink ring-2 ring-paper"
        aria-label="Connection needs attention"
      ></span>
    </button>
  </header>
</template>
