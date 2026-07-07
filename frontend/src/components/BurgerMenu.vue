<script setup>
import { useRouter } from 'vue-router'
import { useAgentsStore } from '../stores/agents'
import { useChatStore } from '../stores/chat'
import GrainEdge from './GrainEdge.vue'

defineProps({
  open: { type: Boolean, default: false },
})

const emit = defineEmits(['close'])

const router = useRouter()
const agents = useAgentsStore()
const chat = useChatStore()

function selectAgent(agent) {
  if (chat.isStreaming) return
  agents.setActiveAgent(agent)
  router.push('/chat')
  emit('close')
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
       inset must match the panel's own w-72 below — keep in sync. -->
  <GrainEdge side="left" inset="18rem" :open="open" />

  <!-- Panel -->
  <Transition
    enter-active-class="transition-transform ease-out duration-200"
    enter-from-class="-translate-x-full"
    enter-to-class="translate-x-0"
    leave-active-class="transition-transform ease-in duration-150"
    leave-from-class="translate-x-0"
    leave-to-class="-translate-x-full"
  >
    <div
      v-if="open"
      class="fixed left-0 top-0 h-full w-72 z-40 bg-paper border-r border-hairline flex flex-col overflow-y-auto"
    >
      <div class="px-5 pt-14 pb-4">
        <p class="text-[11px] font-mono tracking-[0.2em] uppercase text-fog">Navigate</p>
      </div>

      <nav class="pb-2 border-b border-hairline">
        <router-link
          to="/"
          @click="emit('close')"
          class="w-full flex items-center gap-3 px-5 py-3.5 text-left transition-colors hover:bg-paperdim text-fog"
          active-class="text-ink font-semibold"
        >
          <span class="text-sm">Home</span>
        </router-link>
      </nav>

      <div class="px-5 pt-6 pb-4">
        <p class="text-[11px] font-mono tracking-[0.2em] uppercase text-fog">Agents</p>
      </div>

      <nav class="flex-1 pb-2">
        <button
          v-for="agent in agents.agents"
          :key="agent.id"
          @click="selectAgent(agent)"
          class="w-full flex items-center gap-3 px-5 py-3.5 text-left transition-colors hover:bg-paperdim"
          :class="agents.activeAgent?.id === agent.id ? 'text-ink font-semibold' : 'text-fog'"
        >
          <!-- Active state reads as a filled dot; inactive is outline-only —
               weight/fill stands in for the old color-cycling accent dot. -->
          <span
            class="flex-shrink-0 w-2 h-2 rounded-full transition-colors"
            :class="agents.activeAgent?.id === agent.id
              ? 'bg-ink'
              : 'bg-transparent border border-hairline'"
          ></span>
          <span class="text-sm">{{ agent.name }}</span>
        </button>
      </nav>
    </div>
  </Transition>
</template>
