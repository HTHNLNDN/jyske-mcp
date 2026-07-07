import { defineStore } from 'pinia'
import { api } from '../api'

// Deliberately its own store, not folded into chat.js: the tip card is meant
// to be a portable widget (today it sits above the chat timeline, later it
// may live in a dashboard that has nothing to do with chat), so its state
// shouldn't be coupled to the message-loop/history state that only makes
// sense inside a conversation.
export const useTipStore = defineStore('tip', {
  state: () => ({
    todayTip: null,
    tipLoaded: false,
  }),

  actions: {
    // Fetches today's tip once. Safe to call repeatedly — only the first
    // call hits the network, later calls are no-ops via tipLoaded.
    async loadTodayTip() {
      if (this.tipLoaded) return
      try {
        const data = await api.getTodayTip()
        this.todayTip = data?.tip ?? null
      } catch (_) {
        this.todayTip = null
      }
      this.tipLoaded = true
    },

    reset() {
      this.todayTip = null
      this.tipLoaded = false
    },
  },
})
