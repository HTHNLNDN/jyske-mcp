<script setup>
defineProps({
  date: { type: String, required: true },
  summary: { type: String, required: true },
})
</script>

<template>
  <div class="w-full bg-paper border border-hairline rounded px-4 py-3.5">
    <p class="text-[11px] font-mono tracking-wide uppercase text-fog mb-1.5">{{ date }}</p>
    <p class="text-sm text-ink leading-relaxed">{{ summary }}</p>
  </div>
</template>
