<script setup>
// Static, always-visible stipple halo feathering ~12px outward from all four
// edges of the slotted card into the page background. Purely decorative
// (pointer-events-none). Unlike GrainEdge.vue this never opens/closes, so the
// bands render unconditionally -- no `open` prop, no Transition. Reuses
// GrainEdge's technique: one linear-gradient mask per band, opaque at the card
// side, transparent at the far side.
//
// This wrapper must NOT be overflow-hidden -- the bands are translated fully
// outside the card's box. The card keeps its own overflow-hidden; that clips
// only the card's children, not these sibling bands.
</script>

<template>
  <div class="relative">
    <div class="pointer-events-none absolute -inset-x-3 top-0 h-3 -translate-y-full text-ink/40 bg-stipple bg-stipple-sm
      [mask-image:linear-gradient(to_top,black,transparent)] [-webkit-mask-image:linear-gradient(to_top,black,transparent)]" />
    <div class="pointer-events-none absolute -inset-x-3 bottom-0 h-3 translate-y-full text-ink/40 bg-stipple bg-stipple-sm
      [mask-image:linear-gradient(to_bottom,black,transparent)] [-webkit-mask-image:linear-gradient(to_bottom,black,transparent)]" />
    <div class="pointer-events-none absolute inset-y-0 left-0 w-3 -translate-x-full text-ink/40 bg-stipple bg-stipple-sm
      [mask-image:linear-gradient(to_left,black,transparent)] [-webkit-mask-image:linear-gradient(to_left,black,transparent)]" />
    <div class="pointer-events-none absolute inset-y-0 right-0 w-3 translate-x-full text-ink/40 bg-stipple bg-stipple-sm
      [mask-image:linear-gradient(to_right,black,transparent)] [-webkit-mask-image:linear-gradient(to_right,black,transparent)]" />
    <slot />
  </div>
</template>
