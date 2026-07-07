import { defineStore } from 'pinia'
import { api } from '../api'
import { useAuthStore } from './auth'

export const useChatStore = defineStore('chat', {
  state: () => ({
    messages: [],
    conversationHistory: [],
    isTyping: false,
    isStreaming: false,
    inputText: '',
    historyEntries: [],
    historyLoaded: false,
    lastTraceId: null,
  }),

  actions: {
    // Fetches past session summaries once. Safe to call repeatedly — only the
    // first call hits the network, later calls are no-ops via historyLoaded.
    async loadHistory() {
      if (this.historyLoaded) return
      try {
        const data = await api.getHistory()
        this.historyEntries = Array.isArray(data) ? data : []
      } catch (_) {
        this.historyEntries = []
      }
      this.historyLoaded = true
    },

    async sendMessage(agentId) {
      const text = this.inputText.trim()
      if (!text || this.isStreaming) return

      // Safety net: ChatView loads history on mount, but if a message is sent
      // before that resolves, make sure it's loaded so the timeline above the
      // live conversation is never missing entries.
      if (!this.historyLoaded) await this.loadHistory()

      this.inputText = ''
      this.isTyping = true
      this.isStreaming = true

      this.messages.push({ role: 'user', content: text })

      let res
      try {
        res = await api.chat(text, agentId ?? 'finance', [...this.conversationHistory])
      } catch (_) {
        this.isTyping = false
        this.isStreaming = false
        this.messages.push({ role: 'assistant', content: 'Network error — try again.' })
        return
      }

      if (res.status === 401) {
        this.isTyping = false
        this.isStreaming = false
        this.messages.pop()
        useAuthStore().pinVerified = false
        return
      }

      // The trace id for this response is available on the Response object as
      // soon as the fetch resolves, before the stream body is consumed —
      // capture it now so feedback can reference it once the reply is done.
      this.lastTraceId = res.headers.get('X-Trace-Id')

      this.messages.push({ role: 'assistant', content: '' })
      const msgIdx = this.messages.length - 1
      let accumulated = ''

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      try {
        outer: while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const parts = buffer.split('\n\n')
          buffer = parts.pop() ?? ''

          for (const part of parts) {
            for (const line of part.split('\n')) {
              if (!line.startsWith('data: ')) continue
              const data = line.slice(6)

              if (data === '[DONE]') {
                this.conversationHistory.push({ role: 'user', content: text })
                this.conversationHistory.push({ role: 'assistant', content: accumulated })
                break outer
              }

              // Agent isn't configured (no model, or no key for its
              // provider) — the backend ends the stream immediately after
              // this marker. Flag the message so MessageBubble renders a
              // distinct "configure now" notice instead of a normal reply.
              if (data.startsWith('[ERROR:NOT_CONFIGURED]')) {
                this.messages[msgIdx].notConfigured = true
                this.messages[msgIdx].content = data.slice('[ERROR:NOT_CONFIGURED]'.length).trim()
                break outer
              }

              if (data.startsWith('[ERROR]')) {
                this.messages[msgIdx].content = data.slice(7).trim()
                break outer
              }

              if (this.isTyping) this.isTyping = false

              let chunk
              try { chunk = JSON.parse(data) } catch { chunk = data }

              accumulated += chunk
              this.messages[msgIdx].content = accumulated
            }
          }
        }
      } catch (_) {
        if (!accumulated) this.messages[msgIdx].content = 'Stream interrupted.'
      }

      this.isTyping = false
      this.isStreaming = false
    },

    // Submits a thumbs up/down (score 1 or 0) for the most recent assistant
    // reply, keyed by the trace id captured when that reply's stream started.
    async submitFeedback(score) {
      if (!this.lastTraceId) return false
      try {
        await api.feedback(this.lastTraceId, score)
        return true
      } catch (_) {
        return false
      }
    },

    clearChat() {
      this.messages = []
      this.conversationHistory = []
      this.inputText = ''
      this.isTyping = false
      this.isStreaming = false
    },
  },
})
