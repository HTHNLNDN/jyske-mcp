import { defineStore } from 'pinia'
import { api } from '../api'

// Its own store, not folded into a combined dashboard.js — mirrors stores/tip.js:
// each widget's data stays independently owned so a widget stays portable/decoupled.
export const useGoalsStore = defineStore('goals', {
  state: () => ({
    goals: [],
    loaded: false,
  }),

  actions: {
    // Fetches goals once. Safe to call repeatedly — only the first call hits
    // the network, later calls are no-ops via `loaded`.
    async load() {
      if (this.loaded) return
      try {
        const data = await api.getGoals()
        this.goals = data?.goals ?? []
      } catch (_) {
        this.goals = []
      }
      this.loaded = true
    },

    reset() {
      this.goals = []
      this.loaded = false
    },
  },
})
