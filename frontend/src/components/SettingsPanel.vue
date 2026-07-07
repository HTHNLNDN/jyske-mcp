<script setup>
import { ref, computed, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useAuthStore } from '../stores/auth'
import BankConnection from './BankConnection.vue'
import AgentDataAudit from './AgentDataAudit.vue'
import ApiKeysSettings from './ApiKeysSettings.vue'
import AgentModelSettings from './AgentModelSettings.vue'
import GrainEdge from './GrainEdge.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  // Optional deep-link, e.g. from the chat NOT_CONFIGURED notice's
  // "Configure now" action (see stores/ui.js) — jump straight to a given
  // agent's submenu section instead of landing on the agent list.
  initialAgentId: { type: String, default: null },
  initialSection: { type: String, default: null },
})

const emit = defineEmits(['close'])

const agentsStore = useAgentsStore()
const auth = useAuthStore()

// Two-level stack, entirely local panel state — not vue-router. null at
// each level means "show the level above". Reset to the top whenever the
// panel is closed, so reopening it always starts from the agent list
// rather than resuming wherever navigation was left off — unless an
// initialAgentId/initialSection deep-link was passed for this open.
const activeAgent = ref(null)     // agent object | null
const activeSection = ref(null)   // 'bank' | 'data' | 'model' | null
const activeGlobal = ref(null)    // 'keys' | null — non-agent-scoped sections

watch(() => props.open, (isOpen) => {
  if (!isOpen) {
    activeAgent.value = null
    activeSection.value = null
    activeGlobal.value = null
    return
  }
  const agent = props.initialAgentId
    ? agentsStore.agents.find((a) => a.id === props.initialAgentId)
    : null
  if (agent) {
    activeAgent.value = agent
    activeSection.value = props.initialSection ?? null
  } else {
    activeAgent.value = null
    activeSection.value = null
  }
})

const SECTIONS = {
  bank: 'Bank connection',
  model: 'Model',
  data: 'Your data',
}

const headerLabel = computed(() => {
  if (activeGlobal.value === 'keys') return 'API keys'
  if (!activeAgent.value) return 'Settings'
  if (!activeSection.value) return activeAgent.value.name
  return SECTIONS[activeSection.value]
})

function selectAgent(agent) {
  activeAgent.value = agent
  activeSection.value = null
}

function selectSection(section) {
  activeSection.value = section
}

function selectGlobal(key) {
  activeGlobal.value = key
}

// One step back: global section → section → agent submenu → agent list.
function goBack() {
  if (activeGlobal.value) {
    activeGlobal.value = null
    return
  }
  if (activeSection.value) {
    activeSection.value = null
  } else {
    activeAgent.value = null
  }
}
</script>

<template>
  <!-- Backdrop -->
  <Transition
    enter-active-class="transition-opacity ease-out duration-200"
    enter-from-class="opacity-0"
    enter-to-class="opacity-100"
    leave-active-class="transition-opacity ease-in duration-150"
    leave-from-class="opacity-100"
    leave-to-class="opacity-0"
  >
    <div
      v-if="open"
      @click="emit('close')"
      class="fixed inset-0 z-30 bg-ink/10"
    ></div>
  </Transition>

  <!-- Grain strip along the panel's slide edge — mounted unconditionally so
       its own Transition (inside GrainEdge) always controls its enter/leave,
       independent of the backdrop's mount/unmount (see GrainEdge.vue).
       inset must match the panel's own w-80 max-w-[88vw] below — keep in sync. -->
  <GrainEdge side="right" inset="min(20rem, 88vw)" :open="open" />

  <!-- Panel -->
  <Transition
    enter-active-class="transition-transform ease-out duration-200"
    enter-from-class="translate-x-full"
    enter-to-class="translate-x-0"
    leave-active-class="transition-transform ease-in duration-150"
    leave-from-class="translate-x-0"
    leave-to-class="translate-x-full"
  >
    <div
      v-if="open"
      class="fixed right-0 top-0 h-full w-80 max-w-[88vw] z-40 bg-paper border-l border-hairline flex flex-col overflow-y-auto"
    >
      <div class="px-5 pt-14 pb-4 flex items-center justify-between gap-2">
        <div class="flex items-center gap-1 min-w-0">
          <button
            v-if="activeAgent || activeGlobal"
            @click="goBack"
            class="w-8 h-8 -ml-2 flex-shrink-0 flex items-center justify-center text-fog hover:text-ink transition-colors"
            aria-label="Back"
          >
            <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="1.5"
                 stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
              <path d="M15 18l-6-6 6-6"/>
            </svg>
          </button>
          <p class="text-[11px] font-mono tracking-[0.2em] uppercase text-fog truncate">{{ headerLabel }}</p>
        </div>
        <button
          @click="emit('close')"
          class="w-8 h-8 -mr-2 flex-shrink-0 flex items-center justify-center text-fog hover:text-ink transition-colors"
          aria-label="Close settings"
        >
          <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="1.5"
               stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
            <path d="M18 6 6 18M6 6l12 12"/>
          </svg>
        </button>
      </div>

      <!-- Global section content (no agent scope) -->
      <div v-if="activeGlobal === 'keys'" class="px-5 pb-8">
        <ApiKeysSettings />
      </div>

      <!-- Level 1: "API keys" row + the agent list, one flat list -->
      <nav v-else-if="!activeAgent" class="flex-1 pb-2">
        <button
          @click="selectGlobal('keys')"
          class="w-full flex items-center justify-between px-5 py-3.5 text-left transition-colors hover:bg-paperdim border-b border-hairline"
        >
          <span class="text-sm text-ink">API keys</span>
          <svg class="w-3.5 h-3.5 text-fog flex-shrink-0" fill="none" stroke="currentColor" stroke-width="1.5"
               stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
            <path d="M9 18l6-6-6-6"/>
          </svg>
        </button>
        <button
          v-for="agent in agentsStore.agents"
          :key="agent.id"
          @click="selectAgent(agent)"
          class="w-full flex items-center justify-between px-5 py-3.5 text-left transition-colors hover:bg-paperdim border-b border-hairline"
        >
          <span class="text-sm text-ink">{{ agent.name }}</span>
          <svg class="w-3.5 h-3.5 text-fog flex-shrink-0" fill="none" stroke="currentColor" stroke-width="1.5"
               stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
            <path d="M9 18l6-6-6-6"/>
          </svg>
        </button>
      </nav>

      <!-- Level 2: per-agent submenu -->
      <nav v-else-if="!activeSection" class="pb-2">
        <button
          v-for="(label, key) in SECTIONS"
          :key="key"
          @click="selectSection(key)"
          class="w-full flex items-center justify-between px-5 py-3.5 text-left transition-colors hover:bg-paperdim border-b border-hairline"
        >
          <span class="text-sm text-ink">{{ label }}</span>
          <svg class="w-3.5 h-3.5 text-fog flex-shrink-0" fill="none" stroke="currentColor" stroke-width="1.5"
               stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
            <path d="M9 18l6-6-6-6"/>
          </svg>
        </button>
      </nav>

      <!-- Level 3: agent section content -->
      <div v-else class="px-5 pb-8">
        <BankConnection v-if="activeSection === 'bank'" />
        <AgentModelSettings v-else-if="activeSection === 'model'" :agent-id="activeAgent.id" />
        <AgentDataAudit v-else-if="activeSection === 'data'" :agent-id="activeAgent.id" />
      </div>

      <!-- Level 1 footer: sign out — mirrors the position it previously
           held in BurgerMenu.vue's footer, now scoped here instead. -->
      <div v-if="!activeAgent && !activeGlobal" class="px-5 py-6 border-t border-hairline">
        <button
          @click="auth.logout()"
          class="text-xs text-fog hover:text-ink transition-colors"
        >Sign out</button>
      </div>
    </div>
  </Transition>
</template>
