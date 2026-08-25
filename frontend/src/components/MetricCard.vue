<script setup>
defineProps({ label: String, value: [String, Number], hint: String, tone: { type: String, default: 'blue' } })
</script>

<template>
  <div class="metric-card" :class="`tone-${tone}`">
    <div class="metric-value">{{ value ?? '—' }}</div>
    <div class="metric-label">{{ label }}</div>
    <div v-if="hint" class="metric-hint">{{ hint }}</div>
  </div>
</template>

