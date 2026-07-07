<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { useAgentsStore } from '../stores/agents'

const agentsStore = useAgentsStore()

const loading = ref(false)
const providers = ref([])

// Keyed by provider id — plain reactive objects (not one ref per provider)
// since the provider list itself is dynamic.
const keyInputs = ref({})
const keySaving = ref({})
const keyClearing = ref({})
const keyError = ref({})

async function load() {
  loading.value = true
  try {
    providers.value = await api.getProviders()
  } catch (err) {
    console.error('Could not load provider keys:', err)
    providers.value = []
  } finally {
    loading.value = false
  }
}

// This component is only mounted once "API keys" is selected
// (SettingsPanel v-if's it in/out), matching BankConnection's
// always-fresh-when-shown pattern.
onMounted(load)

async function refreshProviders() {
  try {
    providers.value = await api.getProviders()
  } catch (_) {
    // Keep the previous list rather than blanking it on a transient failure.
  }
}

async function saveKey(provider) {
  if (keySaving.value[provider]) return
  const value = (keyInputs.value[provider] || '').trim()
  if (!value) return

  keySaving.value = { ...keySaving.value, [provider]: true }
  keyError.value = { ...keyError.value, [provider]: '' }
  try {
    const res = await api.setProviderKey(provider, value)
    if (!res.ok) {
      keyError.value = { ...keyError.value, [provider]: res.data?.detail || 'Could not save key.' }
      return
    }
    keyInputs.value = { ...keyInputs.value, [provider]: '' }
    await refreshProviders()
    await agentsStore.loadAgents()
  } catch (_) {
    keyError.value = { ...keyError.value, [provider]: 'Could not reach the server — check your connection and try again.' }
  } finally {
    keySaving.value = { ...keySaving.value, [provider]: false }
  }
}

async function clearKey(provider) {
  if (keyClearing.value[provider]) return
  keyClearing.value = { ...keyClearing.value, [provider]: true }
  keyError.value = { ...keyError.value, [provider]: '' }
  try {
    await api.deleteProviderKey(provider)
    await refreshProviders()
    await agentsStore.loadAgents()
  } catch (_) {
    keyError.value = { ...keyError.value, [provider]: 'Could not reach the server — check your connection and try again.' }
  } finally {
    keyClearing.value = { ...keyClearing.value, [provider]: false }
  }
}
</script>

<template>
  <h2 class="text-sm font-condensed font-medium text-ink mb-3">API keys</h2>

  <div v-if="loading" class="text-xs font-mono text-fog/60 py-4">Loading…</div>

  <template v-else>
    <p v-if="!providers.length" class="text-xs font-mono text-fog/60 py-2">No providers available.</p>

    <div
      v-for="p in providers"
      :key="p.provider"
      class="mb-4 pb-4 border-b border-hairline last:border-b-0 last:mb-0 last:pb-0"
    >
      <div class="flex items-center gap-2 mb-2">
        <span class="text-sm text-ink">{{ p.label }}</span>
        <span
          v-if="p.has_key"
          class="inline-flex items-center px-2.5 py-0.5 rounded text-[11px] font-mono tracking-[0.06em] uppercase border border-hairline text-fog"
        >Key set</span>
      </div>

      <div class="flex items-center gap-2">
        <input
          type="password"
          v-model="keyInputs[p.provider]"
          :disabled="keySaving[p.provider]"
          placeholder="Enter API key"
          autocomplete="off"
          class="flex-1 min-w-0 text-sm font-mono bg-paperdim border border-hairline rounded px-2.5 py-1.5 disabled:opacity-50"
        />
        <button
          @click="saveKey(p.provider)"
          :disabled="keySaving[p.provider] || !(keyInputs[p.provider] || '').trim()"
          class="flex-shrink-0 text-sm font-medium bg-ink text-paper py-1.5 px-4 rounded transition-opacity disabled:opacity-50"
        >{{ keySaving[p.provider] ? 'Saving…' : 'Save' }}</button>
      </div>

      <button
        v-if="p.has_key"
        @click="clearKey(p.provider)"
        :disabled="keyClearing[p.provider]"
        class="mt-2 text-xs font-mono text-fog hover:text-ink transition-colors disabled:opacity-50"
      >{{ keyClearing[p.provider] ? 'Clearing…' : 'Clear' }}</button>

      <p v-if="keyError[p.provider]" class="mt-2 text-xs text-ink font-semibold">{{ keyError[p.provider] }}</p>
    </div>
  </template>
</template>
