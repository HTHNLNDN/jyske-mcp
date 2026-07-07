import { defineStore } from 'pinia'
import { api } from '../api'
import { useChatStore } from './chat'

export const useAgentsStore = defineStore('agents', {
  state: () => ({
    agents: [],
    activeAgent: null,
  }),

  getters: {
    // /agents already returns `configured` per agent (model set + that
    // model's provider has a key) — this just spares callers (ChatView's
    // NOT_CONFIGURED handling, AgentModelSettings' onboarding hint) from
    // reaching into activeAgent directly.
    isActiveAgentConfigured: (state) => !!state.activeAgent?.configured,
  },

  actions: {
    // Fetches the agent list. Doubles as the session check — /agents is a
    // protected route, so a non-ok response means the session is invalid.
    // Returns whether the request was authorized.
    async loadAgents() {
      const res = await api.getAgents()
      if (res.ok) {
        this.agents = await res.data
        this.activeAgent = this.agents[0] ?? null
      }
      return res.ok
    },

    setActiveAgent(agent) {
      const changed = this.activeAgent?.id !== agent?.id
      this.activeAgent = agent
      if (changed) useChatStore().clearChat()
    },

    reset() {
      this.agents = []
      this.activeAgent = null
    },
  },
})
