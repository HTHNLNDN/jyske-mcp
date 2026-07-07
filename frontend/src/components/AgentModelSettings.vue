<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { useAgentsStore } from '../stores/agents'

const props = defineProps({
  agentId: { type: String, required: true },
})

const agentsStore = useAgentsStore()

const loading = ref(false)
const providers = ref([])

const selectedModel = ref('')
const modelSaving = ref(false)
const modelError = ref('')

const currentAgent = computed(() => agentsStore.agents.find(a => a.id === props.agentId) ?? null)

// The provider that owns the currently selected model — drives the
// stale-selection warning without hardcoding provider ids.
const selectedProvider = computed(() => {
  for (const p of providers.value) {
    if (p.models.some((m) => m.id === selectedModel.value)) return p
  }
  return null
})

// Only offer models from providers the user actually has a key for — but
// don't drop the currently-selected model's provider from the list just
// because its key was since removed, so the select doesn't silently blank
// out from under an already-saved choice (see selectedProvider warning below).
const visibleProviders = computed(() =>
  providers.value.filter(
    (p) => p.has_key || p.models.some((m) => m.id === selectedModel.value)
  )
)

async function load() {
  loading.value = true
  try {
    providers.value = await api.getProviders()
    if (!agentsStore.agents.length) await agentsStore.loadAgents()
    selectedModel.value = currentAgent.value?.model ?? ''
  } catch (err) {
    console.error('Could not load model settings:', err)
    providers.value = []
  } finally {
    loading.value = false
  }
}

// This component is only mounted once "Model" is selected
// (SettingsPanel v-if's it in/out), matching BankConnection's
// always-fresh-when-shown pattern.
onMounted(load)

async function onModelChange() {
  if (modelSaving.value) return
  modelSaving.value = true
  modelError.value = ''
  const previous = currentAgent.value?.model ?? ''
  try {
    const res = await api.setAgentModel(props.agentId, selectedModel.value)
    if (!res.ok) {
      modelError.value = res.data?.detail || 'Could not update model.'
      selectedModel.value = previous
      return
    }
    await agentsStore.loadAgents()
  } catch (_) {
    modelError.value = 'Could not reach the server — check your connection and try again.'
    selectedModel.value = previous
  } finally {
    modelSaving.value = false
  }
}
</script>

<template>
  <h2 class="text-sm font-condensed font-medium text-ink mb-3">Model</h2>

  <div v-if="loading" class="text-xs font-mono text-fog/60 py-4">Loading…</div>

  <template v-else>
    <div class="mb-2">
      <label class="block text-[11px] font-mono tracking-[0.1em] uppercase text-fog mb-1.5">Model</label>
      <select
        v-model="selectedModel"
        @change="onModelChange"
        :disabled="modelSaving"
        class="w-full text-sm bg-paperdim border border-hairline rounded px-2.5 py-1.5 disabled:opacity-50"
      >
        <option value="" disabled>Choose a model…</option>
        <optgroup v-for="p in visibleProviders" :key="p.provider" :label="p.label">
          <option v-for="m in p.models" :key="m.id" :value="m.id">{{ m.label }}</option>
        </optgroup>
      </select>
      <p v-if="modelError" class="mt-2 text-xs text-ink font-semibold">{{ modelError }}</p>
      <p v-if="selectedModel && selectedProvider && !selectedProvider.has_key"
         class="mt-2 text-xs text-ink font-semibold">
        This model's provider key was removed. Re-add it under API keys, or pick another model.
      </p>
    </div>

    <p v-if="visibleProviders.length === 0" class="mb-5 text-xs text-fog leading-relaxed">
      Add an API key under Settings › API keys, then choose a model here.
    </p>
    <p v-else-if="!selectedModel" class="mb-5 text-xs text-fog leading-relaxed">
      Pick a model to start chatting.
    </p>
  </template>
</template>
