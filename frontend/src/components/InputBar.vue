<script setup>
import { useChatStore } from '../stores/chat'
import { useAgentsStore } from '../stores/agents'

const chat = useChatStore()
const agents = useAgentsStore()

function send() {
  chat.sendMessage(agents.activeAgent?.id)
}
</script>

<template>
  <!-- Input bar — sticky, safe area -->
  <div
    class="flex-shrink-0 bg-paper border-t border-hairline px-3 pt-3"
    style="padding-bottom: max(12px, env(safe-area-inset-bottom));"
  >
    <div class="flex items-end gap-2">
      <textarea
        rows="1"
        placeholder="Message…"
        v-model="chat.inputText"
        @keydown.enter.exact.prevent="send"
        :disabled="chat.isStreaming"
        class="flex-1 resize-none bg-paperdim text-ink placeholder-fog/50 rounded px-4 py-3 text-base leading-5 outline-none border border-hairline focus:border-ink/40 transition-colors max-h-32 overflow-y-auto disabled:opacity-50 appearance-none"
        style="field-sizing: content; font-size: 16px; background-color: #ece9e0; color: #0a0a0a; -webkit-appearance: none;"
      ></textarea>
      <button
        @click="send"
        :disabled="!chat.inputText.trim() || chat.isStreaming"
        :class="chat.inputText.trim() && !chat.isStreaming ? 'opacity-100' : 'opacity-35'"
        class="flex-shrink-0 mb-0.5 w-9 h-9 rounded-full bg-ink flex items-center justify-center transition-opacity"
        aria-label="Send"
      >
        <svg class="w-4 h-4 text-paper" viewBox="0 0 24 24" fill="currentColor">
          <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
        </svg>
      </button>
    </div>
  </div>
</template>
