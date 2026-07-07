import { defineStore } from 'pinia'
import { api } from '../api'
import { useAgentsStore } from './agents'
import { useChatStore } from './chat'
import { useTipStore } from './tip'
import { useBudgetsStore } from './budgets'
import { useGoalsStore } from './goals'

// Non-reactive handle for the lockout countdown timer. Kept out of state so it
// doesn't appear in the store's reactive surface (matches the original Alpine app).
let countdownInterval = null

export const useAuthStore = defineStore('auth', {
  state: () => ({
    pinVerified: false,
    pin: '',
    pinShake: false,
    pinError: '',
    pinFailures: 0,
    lockedUntil: 0,
    countdown: 0,
  }),

  actions: {
    // On load: try the protected /agents route. A 200 means the session cookie
    // is still valid, so we skip the PIN screen. Any other response leaves the
    // user on the PIN screen.
    async checkSession() {
      const agentsStore = useAgentsStore()
      const ok = await agentsStore.loadAgents()
      this.pinVerified = ok
      return ok
    },

    async submitPin(pin) {
      if (this.lockedUntil > Date.now()) return

      const res = await api.login(pin)

      if (res.ok) {
        this.pin = ''
        this.pinFailures = 0
        this.pinError = ''
        this.pinVerified = true
        await useAgentsStore().loadAgents()
        return
      }

      this.pin = ''

      if (res.status === 429) {
        // Server-side lockout (safety net if client counter drifted)
        if (!this.lockedUntil) {
          this.lockedUntil = Date.now() + 60000
          this.countdown = 60
          this.startCountdown()
        }
        return
      }

      // 401 — wrong PIN
      this.pinFailures++
      if (this.pinFailures >= 3) {
        this.lockedUntil = Date.now() + 60000
        this.countdown = 60
        this.pinFailures = 0
        this.pinError = ''
        this.startCountdown()
      } else {
        const left = 3 - this.pinFailures
        this.pinError = `Wrong PIN — ${left} attempt${left === 1 ? '' : 's'} left`
        this.pinShake = true
        setTimeout(() => { this.pinShake = false }, 500)
      }
    },

    startCountdown() {
      if (countdownInterval) clearInterval(countdownInterval)
      countdownInterval = setInterval(() => {
        this.countdown = Math.max(0, Math.ceil((this.lockedUntil - Date.now()) / 1000))
        if (this.countdown === 0) {
          clearInterval(countdownInterval)
          countdownInterval = null
          this.lockedUntil = 0
        }
      }, 1000)
    },

    async logout() {
      await api.logout()
      this.pinVerified = false
      this.pin = ''
      this.pinFailures = 0
      this.lockedUntil = 0
      this.countdown = 0
      this.pinError = ''
      this.pinShake = false
      if (countdownInterval) {
        clearInterval(countdownInterval)
        countdownInterval = null
      }
      useChatStore().clearChat()
      useAgentsStore().reset()
      useTipStore().reset()
      useBudgetsStore().reset()
      useGoalsStore().reset()
    },
  },
})
