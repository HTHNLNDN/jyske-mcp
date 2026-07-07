<script setup>
import { ref, watch, onMounted } from 'vue'
import { useAuthStore } from './stores/auth'
import { useTipStore } from './stores/tip'
import { useUiStore } from './stores/ui'
import { api } from './api'
import PinScreen from './components/PinScreen.vue'
import AppHeader from './components/AppHeader.vue'
import BurgerMenu from './components/BurgerMenu.vue'
import SettingsPanel from './components/SettingsPanel.vue'

const auth = useAuthStore()
const tip = useTipStore()
const ui = useUiStore()

const menuOpen = ref(false)
const settingsOpen = ref(false)
// Deep-link target for the settings panel — null/null lands on the plain
// agent list, matching the gear icon's default behaviour.
const settingsAgentId = ref(null)
const settingsSection = ref(null)

function openSettings() {
  settingsAgentId.value = null
  settingsSection.value = null
  settingsOpen.value = true
}

// Consumed by MessageBubble's "Configure now" action (via stores/ui.js) so
// it can jump straight into a given agent's Model section without
// threading props/emits up through ChatView.
watch(() => ui.settingsRequest, (req) => {
  if (!req) return
  settingsAgentId.value = req.agentId
  settingsSection.value = req.section
  settingsOpen.value = true
  ui.settingsRequest = null
})

// True when the Enable Banking connection is expired or about to be —
// drives the attention dot on the settings gear from first load.
const needsAttention = ref(false)

// A brief inline notice for the Enable Banking redirect-back (?consent=…).
// { type: 'success' | 'error', message }
const notice = ref(null)

// On load, probe the session — a valid cookie skips the PIN screen.
onMounted(() => auth.checkSession())

// Once past the PIN screen: consume the consent redirect-back query params
// (if any) and pull the current connection status for the header badge. On
// logout, snap the shell back to its default state.
watch(() => auth.pinVerified, (verified) => {
  if (verified) {
    handleConsentRedirect()
    refreshAttentionBadge()
    tip.loadTodayTip()
  } else {
    menuOpen.value = false
    settingsOpen.value = false
  }
}, { immediate: true })

function handleConsentRedirect() {
  const params = new URLSearchParams(window.location.search)
  const consent = params.get('consent')
  if (!consent) return

  if (consent === 'success') {
    notice.value = { type: 'success', message: 'Bank connection updated.' }
    openSettings()
  } else if (consent === 'error') {
    const reason = params.get('reason')
    notice.value = { type: 'error', message: reason ? `Reconnection failed — ${reason}` : 'Reconnection failed.' }
  }

  // Strip the query params so a refresh doesn't re-trigger the notice.
  history.replaceState(null, '', window.location.pathname)
  setTimeout(() => { notice.value = null }, 6000)
}

async function refreshAttentionBadge() {
  try {
    const status = await api.getConsentStatus()
    needsAttention.value = status?.status === 'expired' || status?.status === 'expiring_soon'
  } catch (_) {
    needsAttention.value = false
  }
}
</script>

<template>
  <PinScreen v-if="!auth.pinVerified" />

  <div v-else class="flex flex-col h-full">
    <AppHeader
      :needs-attention="needsAttention"
      @toggle-menu="menuOpen = true"
      @open-settings="openSettings"
    />

    <!-- Consent redirect-back notice — error reads through a filled ink bar,
         success stays subdued, consistent with the no-accent-color system. -->
    <div
      v-if="notice"
      class="flex-shrink-0 border-b border-hairline px-4 py-2 text-xs font-mono"
      :class="notice.type === 'error' ? 'bg-ink text-paper font-semibold' : 'text-fog'"
    >{{ notice.message }}</div>

    <BurgerMenu
      :open="menuOpen"
      @close="menuOpen = false"
    />

    <SettingsPanel
      :open="settingsOpen"
      :initial-agent-id="settingsAgentId"
      :initial-section="settingsSection"
      @close="settingsOpen = false"
    />

    <router-view />
  </div>
</template>
