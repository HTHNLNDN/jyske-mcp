<script setup>
defineProps({
  side: { type: String, required: true }, // 'left' | 'right'
  inset: { type: String, required: true }, // CSS length matching the adjacent panel's width
  open: { type: Boolean, default: false }, // drives slide-in lockstep with the adjacent panel
})
</script>

<template>
  <!--
    Slides in/out in lockstep with the adjacent panel (same 200ms-out /
    150ms-in timings as the panel's own translate-x Transition).

    This component must be mounted unconditionally (no v-if) by its parent —
    NOT nested inside the backdrop's own v-if branch — and manage its own
    visibility entirely through the `open` prop below. Vue does not run a
    nested Transition's leave animation when the nested component is torn
    down as part of an ANCESTOR unmounting (e.g. the backdrop's own v-if
    going false): the ancestor's unmount tears down descendant component
    instances immediately, before their own leave hooks get a chance to run,
    so the child just vanishes with no animation. Keeping this component
    permanently mounted and driving visibility purely via its own internal
    v-if means its Transition's leave phase is never preempted like that.
    `appear` is still needed for the case where this mounts with `open`
    already true on the very first render (e.g. a deep link that opens the
    settings panel immediately after login).

    The panel's own slide distance is its width (`inset`), NOT this strip's
    own width (`w-24`) — Tailwind's `-translate-x-full`/`translate-x-full`
    would translate by the latter, causing the grain to visibly lag/lead the
    panel's edge throughout the animation instead of tracking it (only
    matching up at the fully-open/closed endpoints). Instead we bind the
    slide distance as a CSS custom property equal to `inset` itself, so the
    strip travels exactly as far as the panel does.
  -->
  <Transition
    appear
    enter-active-class="transition-transform ease-out duration-200"
    enter-from-class="translate-x-[var(--slide)]"
    enter-to-class="translate-x-0"
    leave-active-class="transition-transform ease-in duration-150"
    leave-from-class="translate-x-0"
    leave-to-class="translate-x-[var(--slide)]"
  >
    <div
      v-if="open"
      class="pointer-events-none fixed inset-y-0 z-30 w-24 text-ink/40 bg-stipple bg-stipple-sm"
      :class="side === 'left'
        ? '[mask-image:linear-gradient(to_right,black,transparent)] [-webkit-mask-image:linear-gradient(to_right,black,transparent)]'
        : '[mask-image:linear-gradient(to_left,black,transparent)]  [-webkit-mask-image:linear-gradient(to_left,black,transparent)]'"
      :style="side === 'left'
        ? { left: inset, '--slide': `calc(-1 * (${inset}))` }
        : { right: inset, '--slide': `calc(${inset})` }"
    />
  </Transition>
</template>
