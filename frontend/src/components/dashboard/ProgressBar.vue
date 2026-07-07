<script setup>
// Shared track+fill sub-component — extracted from BudgetCard's original
// inline markup so BudgetCard and GoalCard render identical progress bars.
// Not a "card": no header, no border, just the bar itself.
defineProps({
  percent: { type: Number, required: true },
  // 'over' -> solid ink fill (draws attention, no color).
  // 'warning' -> dense stipple fill.
  // 'ok' -> sparse stipple fill, muted.
  variant: { type: String, default: 'ok' },
})
</script>

<template>
  <div class="h-2 bg-paperdim rounded overflow-hidden">
    <div
      class="h-full rounded"
      :class="variant === 'over' ? 'bg-ink' : variant === 'warning' ? 'bg-stipple bg-stipple-sm text-ink' : 'bg-stipple bg-stipple-md text-fog'"
      :style="'width:' + Math.min(percent, 100) + '%'"
    ></div>
  </div>
</template>
