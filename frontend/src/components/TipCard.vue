<script setup>
import { ref } from 'vue'
import { api } from '../api'

// Self-contained widget: takes tip data in as a prop, talks to its own API
// call directly, and only emits a plain event out. No dependency on the chat
// store, the message-loop array, or any chat-specific composable — this is
// what lets it be dropped into a future dashboard/widget view unchanged.
const props = defineProps({
  tip: { type: Object, default: null },
})

const emit = defineEmits(['feedback-submitted'])

// Real API shape is { id, tip_date, tip_text, feedback_status, ... } (see
// GET /tip/today) — note the field is `id`, not `tip_id`.
const evaluating = ref(false)
const reasonText = ref('')
const submitting = ref(false)
const submitError = ref(false)

// A tip already evaluated in a previous session (e.g. after a page reload)
// should render in the same collapsed "noted" state, not the active card.
const submitted = ref(!!props.tip && props.tip.feedback_status !== 'pending')

function startEvaluating() {
  submitError.value = false
  evaluating.value = true
}

function cancelEvaluating() {
  evaluating.value = false
  reasonText.value = ''
  submitError.value = false
}

async function submitFeedback() {
  const text = reasonText.value.trim()
  if (!text || submitting.value || !props.tip) return

  submitting.value = true
  submitError.value = false
  try {
    await api.submitTipFeedback(props.tip.id, text)
    submitted.value = true
    evaluating.value = false
    emit('feedback-submitted', { tipId: props.tip.id, reasonText: text })
  } catch (_) {
    submitError.value = true
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div
    v-if="tip"
    class="relative w-full bg-paper border border-hairline rounded overflow-hidden"
  >
    <div class="relative px-4 pt-3 pb-2.5 border-b border-hairline flex items-baseline gap-1.5">
      <span class="text-[11px] font-mono font-semibold tracking-[0.12em] uppercase text-ink">Today's Tip</span>
      <span v-if="tip.tip_date" class="text-[11px] font-mono text-fog">· {{ tip.tip_date }}</span>
    </div>

    <div class="relative px-4 py-3">
      <p class="text-sm text-ink leading-relaxed">{{ tip.tip_text }}</p>

      <!-- Collapsed confirmation state — mirrors MessageBubble's post-vote checkmark -->
      <p v-if="submitted" class="mt-3 text-[11px] font-mono text-fog/70">noted — thanks</p>

      <!-- Quiet tagline affordance — matches ChatView's "scroll up for history" treatment -->
      <button
        v-else-if="!evaluating"
        type="button"
        @click="startEvaluating"
        class="mt-3 text-[11px] font-mono text-fog/60 hover:text-ink transition-colors"
      >
        Evaluate tip
      </button>

      <div v-else class="mt-3 space-y-2">
        <textarea
          v-model="reasonText"
          rows="2"
          placeholder="What did you think of this tip?"
          class="w-full text-sm bg-paperdim border border-hairline rounded px-2.5 py-2 text-ink placeholder:text-fog/50 focus:outline-none focus:border-ink/40"
        ></textarea>
        <p v-if="submitError" class="text-[11px] font-mono text-fog/70">Couldn't send — try again.</p>
        <div class="flex items-center gap-4">
          <button
            type="button"
            @click="submitFeedback"
            :disabled="!reasonText.trim() || submitting"
            class="text-[11px] font-mono font-semibold tracking-[0.08em] uppercase text-ink disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {{ submitting ? 'Sending…' : 'Submit' }}
          </button>
          <button
            type="button"
            @click="cancelEvaluating"
            class="text-[11px] font-mono text-fog/60 hover:text-ink transition-colors"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
