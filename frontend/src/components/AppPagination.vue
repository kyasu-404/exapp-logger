<script setup>
defineProps({ page: Number, pages: Number, total: Number })
defineEmits(['change'])
</script>

<template>
  <div v-if="pages > 1" class="pagination">
    <span>Всего: {{ total }}</span>
    <button type="button" :disabled="page <= 1" @click="$emit('change', page - 1)">Назад</button>
    <span>Страница {{ page }} из {{ pages }}</span>
    <button type="button" :disabled="page >= pages" @click="$emit('change', page + 1)">Далее</button>
  </div>
</template>

