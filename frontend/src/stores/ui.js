import { defineStore } from 'pinia'

// Minimal cross-component signal for "open Settings, optionally deep-linked
// to a specific agent + section". Exists because the NOT_CONFIGURED chat
// notice's "Configure now" action (rendered deep inside MessageBubble) needs
// to reach App.vue's settings panel state without threading props/emits
// through ChatView -> App.vue. App.vue watches settingsRequest and clears it
// once consumed, so re-requesting the same target still fires the watcher.
export const useUiStore = defineStore('ui', {
  state: () => ({
    settingsRequest: null, // { agentId, section, token } | null
  }),

  actions: {
    openSettings(agentId = null, section = null) {
      this.settingsRequest = { agentId, section, token: Date.now() }
    },
  },
})
