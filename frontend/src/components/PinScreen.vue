<script setup>
import { ref, watch, onMounted } from 'vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const pinInput = ref(null)

function focusInput() {
  pinInput.value?.focus()
}

function onKey(e) {
  if (auth.lockedUntil > Date.now()) return

  if (e.key >= '0' && e.key <= '9') {
    e.preventDefault()
    if (auth.pin.length < 6) {
      auth.pin += e.key
      if (auth.pin.length === 6) auth.submitPin(auth.pin)
    }
  } else if (e.key === 'Backspace') {
    e.preventDefault()
    auth.pin = auth.pin.slice(0, -1)
    auth.pinError = ''
  }
}

// Re-focus the hidden input after a wrong-PIN shake settles.
watch(() => auth.pinShake, (shaking) => {
  if (!shaking) focusInput()
})

// Re-focus once a lockout countdown finishes.
watch(() => auth.countdown, (now, prev) => {
  if (now === 0 && prev > 0) focusInput()
})

onMounted(focusInput)
</script>

<template>
  <div
    @click="focusInput"
    class="fixed inset-0 z-50 flex flex-col items-center justify-center bg-paper cursor-pointer select-none"
  >
    <p class="mb-10 text-xs font-mono tracking-[0.25em] uppercase text-fog/50">Finance</p>

    <!-- 6 circles — outline always ink, fills solid ink on each digit -->
    <div class="flex gap-4" :class="auth.pinShake ? 'shake' : ''">
      <div
        v-for="i in 6"
        :key="i"
        class="w-4 h-4 rounded-full border-2 border-ink transition-colors duration-100"
        :class="auth.pin.length >= i ? 'bg-ink' : 'bg-transparent'"
      ></div>
    </div>

    <!-- Hidden input: receives focus on click, captures all keystrokes -->
    <input
      ref="pinInput"
      type="tel"
      inputmode="numeric"
      autocomplete="off"
      @keydown="onKey"
      class="sr-only"
      aria-label="Enter 6-digit PIN"
    />

    <!-- Status: lockout countdown / wrong-PIN error / idle hint — errors read
         through bold weight rather than a warning color. -->
    <div class="mt-6 h-5 text-sm text-center font-mono">
      <span v-if="auth.countdown > 0" class="text-ink font-semibold">
        Locked — {{ auth.countdown }}s remaining
      </span>
      <span v-else-if="auth.pinError" class="text-ink font-semibold">{{ auth.pinError }}</span>
      <span v-else class="text-fog/60 text-xs">
        Click · then type your PIN
      </span>
    </div>
  </div>
</template>
