<script setup>
import { computed, ref } from 'vue'
import BudgetCard from './BudgetCard.vue'
import { useChatStore } from '../stores/chat'
import { useAgentsStore } from '../stores/agents'
import { useUiStore } from '../stores/ui'
import { renderMarkdown } from '../lib/markdown'

const props = defineProps({
  message: { type: Object, required: true },
  // True only for the most recent assistant message — feedback controls
  // (thumbs up/down) are only ever shown there, never on historical replies.
  isLatest: { type: Boolean, default: false },
})

const chat = useChatStore()
const agentsStore = useAgentsStore()
const uiStore = useUiStore()

// Set by stores/chat.js when the stream ends with the [ERROR:NOT_CONFIGURED]
// marker (no model or no provider key set for the active agent) — rendered
// as a distinct inline notice rather than a normal assistant reply.
const isNotConfigured = computed(() => props.message.role === 'assistant' && props.message.notConfigured === true)

function configureNow() {
  uiStore.openSettings(agentsStore.activeAgent?.id ?? null, 'model')
}

// null = no vote yet, otherwise the submitted score (1 or 0). Once set, the
// buttons are replaced by a checkmark for the rest of the session — no undo.
const votedScore = ref(null)
const submitting = ref(false)

// Wait until the reply has actually finished streaming (and has content)
// before offering feedback — an in-progress, still-empty bubble isn't a
// response worth rating yet.
const showFeedback = computed(() =>
  props.isLatest &&
  props.message.role === 'assistant' &&
  !chat.isStreaming &&
  !!props.message.content &&
  !isNotConfigured.value &&
  votedScore.value === null
)

async function vote(score) {
  if (submitting.value || votedScore.value !== null) return
  submitting.value = true
  const ok = await chat.submitFeedback(score)
  submitting.value = false
  if (ok) votedScore.value = score
}

// An assistant message whose content is a budget JSON array renders as a card.
// The model is told to return raw JSON, but tolerate it wrapping the array in
// markdown fences or a sentence of prose — clean those off before parsing, and
// fall back to plain text if what's left still isn't a valid budget array.
const budget = computed(() => {
  if (props.message.role !== 'assistant') return null
  if (typeof props.message.content !== 'string') return null

  // Strip markdown code fences (```json … ``` or ``` … ```).
  let cleaned = props.message.content.replace(/```(?:json)?/gi, '').trim()

  // Drop any leading prose before the first '['.
  const start = cleaned.indexOf('[')
  if (start === -1) return null
  cleaned = cleaned.slice(start)

  // Only a JSON array is a budget payload.
  if (!cleaned.startsWith('[')) return null

  try {
    const data = JSON.parse(cleaned)
    if (Array.isArray(data) && data.length > 0 && 'category' in data[0] && 'spent' in data[0]) {
      return data
    }
  } catch (_) {}
  return null
})

// Safe to render as markdown once the message is no longer actively being
// streamed into. `isStreaming` is a single global flag on the store, not
// per-message, so it alone isn't enough — gate on isLatest too, otherwise an
// older, already-finished assistant message would wrongly stay stuck in the
// raw whitespace-pre-wrap fallback whenever a *later* message happens to be
// streaming.
const canRenderMarkdown = computed(() =>
  props.message.role === 'assistant' &&
  !budget.value &&
  !(props.isLatest && chat.isStreaming)
)

const renderedHtml = computed(() => {
  if (!canRenderMarkdown.value) return ''
  if (typeof props.message.content !== 'string') return ''
  return renderMarkdown(props.message.content)
})
</script>

<template>
  <div :class="message.role === 'user' ? 'flex justify-end' : 'flex flex-col items-start'">
    <!-- Budget card — assistant message that parses as a budget JSON array -->
    <BudgetCard v-if="budget" :budgets="budget" :editable="false" />

    <!-- Agent not configured (no model / no provider key) — a distinct
         inline notice, not a normal reply bubble, with a way to fix it. -->
    <div
      v-else-if="isNotConfigured"
      class="max-w-[85%] text-xs leading-relaxed border border-hairline rounded px-4 py-3"
    >
      <p class="text-ink mb-2">{{ message.content }}</p>
      <button
        @click="configureNow"
        class="text-[11px] font-mono tracking-[0.08em] uppercase text-ink underline underline-offset-2 hover:no-underline"
      >Configure now</button>
    </div>

    <!-- Rendered markdown — non-budget assistant message, done streaming -->
    <div
      v-else-if="canRenderMarkdown"
      class="chat-markdown max-w-[80%] text-sm leading-relaxed rounded px-4 py-2.5 bg-paper border border-hairline text-ink"
      v-html="renderedHtml"
    ></div>

    <!-- Plain text — user messages, and non-budget assistant messages still streaming -->
    <div
      v-else
      class="max-w-[80%] text-sm leading-relaxed rounded px-4 py-2.5 whitespace-pre-wrap"
      :class="message.role === 'user'
        ? 'bg-ink text-paper font-[450]'
        : 'bg-paper border border-hairline text-ink'"
    >{{ message.content }}</div>

    <!-- Feedback controls — most recent assistant reply only -->
    <div v-if="showFeedback" class="flex items-center gap-3 mt-1.5 px-1">
      <button
        @click="vote(1)"
        :disabled="submitting"
        class="text-fog hover:text-ink transition-colors disabled:opacity-50"
        aria-label="Good response"
      >
        <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="1.5"
             stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
          <path d="M7 11v10H4a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1h3zm0 0 5.5-8a2 2 0 0 1 2.7-.7 2 2 0 0 1 .9 2.2L14.5 9H19a2 2 0 0 1 2 2.3l-1.4 8A2 2 0 0 1 17.6 21H10a3 3 0 0 1-3-3v-7z"/>
        </svg>
      </button>
      <button
        @click="vote(0)"
        :disabled="submitting"
        class="text-fog hover:text-ink transition-colors disabled:opacity-50"
        aria-label="Bad response"
      >
        <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="1.5"
             stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
          <path d="M17 13V3h3a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-3zm0 0-5.5 8a2 2 0 0 1-2.7.7 2 2 0 0 1-.9-2.2l1.6-6.5H5a2 2 0 0 1-2-2.3l1.4-8A2 2 0 0 1 6.4 3H14a3 3 0 0 1 3 3v7z"/>
        </svg>
      </button>
    </div>
    <div v-else-if="isLatest && message.role === 'assistant' && votedScore !== null" class="mt-1.5 px-1">
      <svg class="w-4 h-4 text-ink" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
        <path d="M20 6 9 17l-5-5"/>
      </svg>
    </div>
  </div>
</template>

<style scoped>
/* Rendered markdown inside an assistant bubble — extends the app's flat,
   hairline-only surface treatment rather than falling back to generic
   `prose`-style defaults (no shadows, no rounded-2xl, no color accents). */
.chat-markdown :deep(p) {
  margin: 0 0 0.6em;
}
.chat-markdown :deep(p:last-child) {
  margin-bottom: 0;
}
.chat-markdown :deep(ul),
.chat-markdown :deep(ol) {
  margin: 0 0 0.6em;
  padding-left: 1.25em;
}
.chat-markdown :deep(ul) {
  list-style: disc;
}
.chat-markdown :deep(ol) {
  list-style: decimal;
}
.chat-markdown :deep(li) {
  margin: 0.15em 0;
}
.chat-markdown :deep(h1),
.chat-markdown :deep(h2),
.chat-markdown :deep(h3),
.chat-markdown :deep(h4),
.chat-markdown :deep(h5),
.chat-markdown :deep(h6) {
  font-family: 'IBM Plex Sans Condensed', -apple-system, sans-serif;
  font-weight: 600;
  margin: 0.8em 0 0.4em;
  line-height: 1.25;
}
.chat-markdown :deep(h1:first-child),
.chat-markdown :deep(h2:first-child),
.chat-markdown :deep(h3:first-child) {
  margin-top: 0;
}
.chat-markdown :deep(strong) {
  font-weight: 700;
}
.chat-markdown :deep(a) {
  color: theme('colors.ink');
  text-decoration: underline;
  text-underline-offset: 2px;
}
.chat-markdown :deep(code) {
  font-family: 'IBM Plex Mono', ui-monospace, SFMono-Regular, monospace;
  font-size: 0.85em;
  background: theme('colors.paperdim');
  padding: 0.1em 0.35em;
  border-radius: 1px;
}
.chat-markdown :deep(pre) {
  font-family: 'IBM Plex Mono', ui-monospace, SFMono-Regular, monospace;
  font-size: 0.8em;
  background: theme('colors.paperdim');
  border: 1px solid theme('colors.hairline');
  border-radius: 2px;
  padding: 0.6em 0.75em;
  overflow-x: auto;
  margin: 0 0 0.6em;
}
.chat-markdown :deep(pre code) {
  background: none;
  padding: 0;
  font-size: 1em;
}
.chat-markdown :deep(blockquote) {
  margin: 0 0 0.6em;
  padding-left: 0.75em;
  border-left: 2px solid theme('colors.hairline');
  color: theme('colors.fog');
}
.chat-markdown :deep(hr) {
  border: none;
  border-top: 1px solid theme('colors.hairline');
  margin: 0.75em 0;
}
.chat-markdown :deep(table) {
  width: 100%;
  border-collapse: collapse;
  font-family: 'IBM Plex Mono', ui-monospace, SFMono-Regular, monospace;
  font-size: 0.8em;
  margin: 0 0 0.6em;
}
.chat-markdown :deep(th),
.chat-markdown :deep(td) {
  border: 1px solid theme('colors.hairline');
  padding: 0.35em 0.6em;
  text-align: left;
}
.chat-markdown :deep(th) {
  font-weight: 600;
  background: theme('colors.paperdim');
}
</style>
