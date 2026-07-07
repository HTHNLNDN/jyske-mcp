<script setup>
import { onMounted, watch } from 'vue'
import { useTipStore } from '../../stores/tip'
import TipCard from '../TipCard.vue'

// Thin container/glue — TipCard itself stays unchanged. Reports back whether
// it ended up with a real tip once loaded, same contract as the other widgets.
const emit = defineEmits(['loaded'])

const tip = useTipStore()

onMounted(() => tip.loadTodayTip())

watch(() => tip.tipLoaded, (loaded) => {
  if (loaded) emit('loaded', !!tip.todayTip)
}, { immediate: true })
</script>

<template>
  <TipCard v-if="tip.todayTip" :tip="tip.todayTip" />
</template>
