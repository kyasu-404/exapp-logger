<script setup>
const props = defineProps({ status: { type: String, required: true } })
const map = {
  submitted: ['✓', 'Сдано'],
  not_submitted: ['×', 'Не сдано'],
  partial: ['◐', 'Частично'],
  overdue: ['!', 'Просрочено'],
  updated: ['↻', 'Обновлено'],
  deleted: ['⌫', 'Удалено'],
  unrecognized: ['?', 'Не распознано'],
  ok: ['✓', 'Распознано'],
  unknown_organization: ['?', 'Неизвестная организация'],
  unknown_dimension: ['?', 'Неизвестное значение'],
  missing_dimension: ['?', 'Не хватает уровня'],
  outside_structure: ['?', 'Вне структуры'],
  ignored: ['–', 'Игнорируется'],
  draft: ['○', 'Черновик'],
  active: ['●', 'Активна'],
  completed: ['✓', 'Завершена'],
  archived: ['□', 'Архив'],
}
const info = map[props.status] || ['•', props.status]
</script>

<template>
  <span class="status-badge" :class="`status-${status}`"><span aria-hidden="true">{{ info[0] }}</span>{{ info[1] }}</span>
</template>

