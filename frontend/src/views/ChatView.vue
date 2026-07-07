<script setup>
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useChatStore } from '../stores/chat'
import { useTipStore } from '../stores/tip'
import SessionSummaryCard from '../components/SessionSummaryCard.vue'
import DateDivider from '../components/DateDivider.vue'
import MessageBubble from '../components/MessageBubble.vue'
import TypingIndicator from '../components/TypingIndicator.vue'
import InputBar from '../components/InputBar.vue'
import TipCard from '../components/TipCard.vue'

const chat = useChatStore()
const tip = useTipStore()
const msgContainer = ref(null)

function scrollToBottom() {
  const el = msgContainer.value
  if (el) el.scrollTop = el.scrollHeight
}

// Past summaries grouped by day so each distinct date gets one divider, even
// if more than one session happened that day. historyEntries already arrives
// oldest-first from the backend, so groups come out in that order too.
const historyGroups = computed(() => {
  const groups = []
  let current = null
  for (const entry of chat.historyEntries) {
    if (!current || current.period !== entry.period) {
      current = { period: entry.period, date: entry.date, entries: [] }
      groups.push(current)
    }
    current.entries.push(entry)
  }
  return groups
})

// Index of the most recent assistant message — only that bubble gets the
// feedback (thumbs up/down) controls, never earlier ones in the timeline.
const lastAssistantIndex = computed(() => {
  for (let i = chat.messages.length - 1; i >= 0; i--) {
    if (chat.messages[i].role === 'assistant') return i
  }
  return -1
})

// Keep pinned to the bottom as messages stream in or the typing dots appear —
// history sits above the fold by default, today's conversation stays in view.
watch(() => chat.messages, () => nextTick(scrollToBottom), { deep: true })
watch(() => chat.isTyping, () => nextTick(scrollToBottom))

onMounted(async () => {
  await chat.loadHistory()
  await nextTick()
  scrollToBottom()
})
</script>

<template>
  <!-- Conversation area: past sessions above the fold, today's live chat at the bottom -->
  <main ref="msgContainer" class="flex-1 overflow-y-auto overscroll-y-contain">
    <p v-if="historyGroups.length > 0" class="pt-3 text-center text-[11px] font-mono text-fog/50">
      scroll up for history
    </p>

    <div v-if="historyGroups.length > 0" class="px-4 pt-2 pb-2 space-y-3">
      <template v-for="group in historyGroups" :key="group.period">
        <DateDivider :label="group.date" />
        <SessionSummaryCard
          v-for="(entry, i) in group.entries"
          :key="`${group.period}-${i}`"
          :date="entry.date"
          :summary="entry.summary"
        />
      </template>

      <DateDivider label="Today" />

      <!-- Financial tip of the day — a standalone widget, fetched once per
           session (see stores/tip.js). Sits at the top of today's section,
           right after the divider, so it's in view on load without
           scrolling up past the whole history — the view is pinned to the
           bottom by default and this is the last thing before the live chat. -->
      <TipCard v-if="tip.todayTip" :tip="tip.todayTip" />
    </div>

    <!-- No history yet (first day) — nothing to sit "below", so the tip
         still gets its own spot just above the live chat. -->
    <div v-else-if="tip.todayTip" class="px-4 pt-3">
      <TipCard :tip="tip.todayTip" />
    </div>

    <!-- Empty state -->
    <div
      v-if="chat.messages.length === 0 && !chat.isTyping"
      class="h-full flex items-center justify-center px-6"
    >
      <p class="text-fog/60 text-sm text-center leading-relaxed">
        Ask me anything about your finances
      </p>
    </div>

    <!-- Messages + typing indicator -->
    <div v-else class="px-4 pt-4 pb-2 space-y-3">
      <MessageBubble
        v-for="(msg, idx) in chat.messages"
        :key="idx"
        :message="msg"
        :is-latest="idx === lastAssistantIndex"
      />
      <TypingIndicator v-if="chat.isTyping" />
    </div>
  </main>

  <InputBar />
</template>
