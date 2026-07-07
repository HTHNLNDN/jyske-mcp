<script setup>
// Three bouncing dots shown while the assistant's first token is pending.
</script>

<template>
  <div class="flex justify-start">
    <div class="bg-paper border border-hairline rounded px-4 py-3.5 flex items-center gap-1.5">
      <span class="w-2 h-2 rounded-full bg-fog/40 typing-dot" style="animation-delay:   0ms"></span>
      <span class="w-2 h-2 rounded-full bg-fog/40 typing-dot" style="animation-delay: 200ms"></span>
      <span class="w-2 h-2 rounded-full bg-fog/40 typing-dot" style="animation-delay: 400ms"></span>
    </div>
  </div>
</template>
