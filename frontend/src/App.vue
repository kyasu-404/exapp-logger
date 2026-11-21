<script setup>
import { FilePickerClosed, getFilePickerBuilder } from '@nextcloud/dialogs'
import '@nextcloud/dialogs/style.css'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api, downloadUrl, query } from './api'

const nav = [
  { id: 'overview', label: 'Обзор', icon: '⌂' },
  { id: 'watches', label: 'Наблюдения', icon: '◎' },
  { id: 'files', label: 'Файлы', icon: '▤' },
  { id: 'activity', label: 'Активность', icon: '↻' },
  { id: 'reports', label: 'Отчёты', icon: '⇩' },
]
const statusLabels = {
  draft: 'Черновик',
  active: 'Активно',
  paused: 'Приостановлено',
  completed: 'Завершено',
  archived: 'В архиве',
  new: 'Есть',
  updated: 'Обновлён',
  deleted: 'Удалён',
}
const eventIcons = { created: '+', updated: '↻', renamed: '✎', moved: '→', copied: '⧉', deleted: '×' }
const weekdayLabels = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
const DIRECTORY_MIME = 'httpd/unix-directory'

const section = ref('overview')
const loading = ref(false)
const error = ref('')
const toast = ref('')
const access = ref({ is_admin: false })
const watches = ref([])
const dashboard = ref(null)
const selectedWatchId = ref(Number(localStorage.getItem('file-control-watch') || 0))
const summary = ref(null)
const filesPage = ref({ items: [], page: 1, pages: 1, total: 0 })
const eventsPage = ref({ items: [], page: 1, pages: 1, total: 0 })
const modalOpen = ref(false)
const editingId = ref(0)
const groups = ref([])
const folderInspecting = ref(false)
const folderVerified = ref(false)
const selectedBranch = ref('')
const reportSchedule = ref(null)
let refreshTimer = 0
let fileRequestId = 0
let requestedFilePage = 1
const REFRESH_INTERVAL_MS = 15000

const fileFilters = reactive({
  search: '',
  extension: '',
  status: '',
  date_from: '',
  date_to: '',
  path_prefix: '',
})
let appliedFileFilters = { ...fileFilters }
const eventFilters = reactive({
  search: '',
  event_type: '',
  date_from: '',
  date_to: '',
  path_prefix: '',
})
const watchForm = reactive({
  name: '',
  root_path: '',
  scope_type: '',
  group_ids: [],
  started_at: '',
  ended_at: '',
  status: 'active',
})
const scheduleForm = reactive({
  enabled: false,
  cadence: 'daily',
  local_time: '09:00',
  weekday: 0,
  day_of_month: 1,
  destination_path: '',
})
const changeFilters = reactive({
  date_from: moscowDate(-7),
  date_to: moscowDate(),
})

const currentWatch = computed(() => watches.value.find((item) => item.id === selectedWatchId.value) || null)
const currentNav = computed(() => nav.find((item) => item.id === section.value))
const branchStats = computed(() => summary.value?.tree?.find((item) => item.path === selectedBranch.value) || null)
const reportParams = computed(() => ({
  watch_id: selectedWatchId.value,
  path_prefix: selectedBranch.value,
}))
const lastSync = computed(() => summary.value?.last_reconciliation || null)
const canManage = computed(() => Boolean(access.value?.is_admin))

function showToast(message) {
  toast.value = message
  window.setTimeout(() => {
    if (toast.value === message) toast.value = ''
  }, 3500)
}

async function run(task) {
  loading.value = true
  error.value = ''
  try {
    return await task()
  } catch (err) {
    error.value = err.message || 'Неизвестная ошибка'
    return null
  } finally {
    loading.value = false
  }
}

async function loadWatches() {
  const result = await api('/api/watches')
  watches.value = result
  if (!watches.value.some((item) => item.id === selectedWatchId.value)) {
    selectedWatchId.value = watches.value[0]?.id || 0
  }
}

async function loadAccess() {
  access.value = await api('/api/access')
}

async function loadDashboard() {
  dashboard.value = await api('/api/dashboard')
}

async function inspectSelectedFolder(preservedGroups = []) {
  folderVerified.value = false
  watchForm.scope_type = ''
  watchForm.group_ids = []
  groups.value = []
  if (!watchForm.root_path) return

  folderInspecting.value = true
  error.value = ''
  try {
    const result = await api(`/api/folder-info?${query({ path: watchForm.root_path })}`)
    watchForm.scope_type = result.scope_type
    if (result.scope_type === 'team') {
      groups.value = result.groups || []
      watchForm.group_ids = [...(result.group_ids || [])]
    } else {
      groups.value = result.available_groups || []
      const available = new Set(groups.value.map((item) => item.id))
      watchForm.group_ids = preservedGroups.filter((item) => available.has(item))
    }
    folderVerified.value = true
  } catch (err) {
    error.value = err.message || 'Не удалось определить тип папки'
  } finally {
    folderInspecting.value = false
  }
}

async function loadSummary() {
  summary.value = selectedWatchId.value
    ? await api(`/api/watches/${selectedWatchId.value}/summary`)
    : null
  if (selectedBranch.value && !summary.value?.tree?.some((item) => item.path === selectedBranch.value)) {
    selectedBranch.value = ''
  }
}

async function loadReportSchedule() {
  reportSchedule.value = selectedWatchId.value
    ? await api(`/api/reports/schedule/${selectedWatchId.value}`)
    : null
  Object.assign(scheduleForm, {
    enabled: reportSchedule.value?.enabled || false,
    cadence: reportSchedule.value?.cadence || 'daily',
    local_time: reportSchedule.value?.local_time || '09:00',
    weekday: reportSchedule.value?.weekday ?? 0,
    day_of_month: reportSchedule.value?.day_of_month ?? 1,
    destination_path: reportSchedule.value?.destination_path || '',
  })
}

function moscowDate(dayOffset = 0) {
  const value = new Date(Date.now() + dayOffset * 86400000)
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Moscow',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(value)
  const get = (type) => parts.find((part) => part.type === type)?.value || ''
  return `${get('year')}-${get('month')}-${get('day')}`
}

function moscowBoundary(value, end = false) {
  if (!value) return ''
  return new Date(`${value}T${end ? '23:59:59.999' : '00:00:00'}+03:00`).toISOString()
}

function requestDates(filters) {
  return {
    date_from: moscowBoundary(filters.date_from),
    date_to: moscowBoundary(filters.date_to, true),
  }
}

async function loadFiles(page = requestedFilePage) {
  const requestId = ++fileRequestId
  const watchId = selectedWatchId.value
  requestedFilePage = page
  if (!watchId) {
    filesPage.value = { items: [], page: 1, pages: 1, total: 0 }
    return
  }
  const params = {
    page,
    page_size: 50,
    ...appliedFileFilters,
    ...requestDates(appliedFileFilters),
  }
  const isCurrent = () => requestId === fileRequestId && watchId === selectedWatchId.value
  try {
    const result = await api(`/api/watches/${watchId}/files?${query(params)}`)
    if (isCurrent()) {
      filesPage.value = result
      requestedFilePage = result.page
    }
  } catch (err) {
    if (isCurrent()) throw err
  }
}

function applyFileFilters() {
  appliedFileFilters = { ...fileFilters }
  return run(() => loadFiles(1))
}

async function loadEvents(page = 1) {
  if (!selectedWatchId.value) {
    eventsPage.value = { items: [], page: 1, pages: 1, total: 0 }
    return
  }
  const params = {
    page,
    page_size: 50,
    ...eventFilters,
    ...requestDates(eventFilters),
  }
  eventsPage.value = await api(`/api/watches/${selectedWatchId.value}/events?${query(params)}`)
}

async function refreshSection() {
  await run(async () => {
    if (section.value === 'overview') {
      await Promise.all([loadDashboard(), loadSummary()])
    } else if (section.value === 'watches') {
      await loadWatches()
    } else if (section.value === 'files') {
      await Promise.all([loadFiles(), loadSummary()])
    } else if (section.value === 'activity') {
      await Promise.all([loadEvents(), loadSummary()])
    } else if (section.value === 'reports') {
      await Promise.all([loadSummary(), loadReportSchedule()])
    }
  })
}

function switchSection(id) {
  section.value = id
}

function resetForm() {
  Object.assign(watchForm, {
    name: '',
    root_path: '',
    scope_type: '',
    group_ids: [],
    started_at: '',
    ended_at: '',
    status: 'active',
  })
  groups.value = []
  folderVerified.value = false
  folderInspecting.value = false
  editingId.value = 0
}

async function chooseFolder() {
  try {
    const selected = await pickFolder('Выберите папку', watchForm.root_path)
    if (selected) {
      watchForm.root_path = selected
      await inspectSelectedFolder()
    }
  } catch (err) {
    if (!(err instanceof FilePickerClosed)) error.value = err.message || 'Не удалось открыть выбор папки'
  }
}

async function pickFolder(title, startAt = '') {
  const builder = getFilePickerBuilder(title)
      .setMultiSelect(false)
      .allowDirectories(true)
      .setMimeTypeFilter([DIRECTORY_MIME])
      .setFilter((node) => node.mime === DIRECTORY_MIME)
      .setCanPick((node) => node.mime === DIRECTORY_MIME)
      .addButton({ label: 'Выбрать', callback: () => {} })
  if (startAt) builder.startAt(startAt)
  const selected = await builder.build().pick()
  return selected ? `/${String(selected).replace(/^\/+/, '')}` : ''
}

async function chooseReportFolder() {
  try {
    const selected = await pickFolder('Папка для автоматических отчётов', scheduleForm.destination_path)
    if (selected) scheduleForm.destination_path = selected
  } catch (err) {
    if (!(err instanceof FilePickerClosed)) error.value = err.message || 'Не удалось открыть выбор папки'
  }
}

function openCreate() {
  if (!canManage.value) return
  resetForm()
  modalOpen.value = true
}

function dateInput(value) {
  if (!value) return ''
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date(value))
  const get = (type) => parts.find((part) => part.type === type)?.value || ''
  return `${get('year')}-${get('month')}-${get('day')}`
}

function openEdit(item) {
  if (!canManage.value) return
  editingId.value = item.id
  Object.assign(watchForm, {
    name: item.name,
    root_path: item.root_path,
    scope_type: '',
    group_ids: [],
    started_at: dateInput(item.started_at),
    ended_at: dateInput(item.ended_at),
    status: item.status,
  })
  modalOpen.value = true
  inspectSelectedFolder([...(item.group_ids || [])])
}

function watchPayload() {
  return {
    name: watchForm.name.trim(),
    root_path: watchForm.root_path,
    scope_type: watchForm.scope_type,
    group_ids: watchForm.group_ids,
    started_at: watchForm.started_at ? moscowBoundary(watchForm.started_at) : null,
    ended_at: watchForm.ended_at ? moscowBoundary(watchForm.ended_at, true) : null,
    status: watchForm.status,
  }
}

async function saveWatch() {
  if (!canManage.value) return
  if (!watchForm.name.trim() || !watchForm.root_path) {
    error.value = 'Укажите название и папку'
    return
  }
  if (!folderVerified.value || !watchForm.scope_type) {
    error.value = 'Дождитесь определения типа выбранной папки'
    return
  }
  await run(async () => {
    const result = editingId.value
      ? await api(`/api/watches/${editingId.value}`, { method: 'PATCH', body: watchPayload() })
      : await api('/api/watches', { method: 'POST', body: watchPayload() })
    modalOpen.value = false
    await loadWatches()
    selectedWatchId.value = result.id
    await Promise.all([loadDashboard(), loadSummary()])
    showToast(editingId.value ? 'Наблюдение сохранено' : 'Наблюдение создано')
  })
}

async function archiveWatch(item) {
  if (!canManage.value) return
  if (!window.confirm(`Переместить «${item.name}» в архив?`)) return
  await run(async () => {
    await api(`/api/watches/${item.id}`, { method: 'DELETE' })
    await loadWatches()
    await loadDashboard()
    showToast('Наблюдение перемещено в архив')
  })
}

async function reconcile() {
  if (!selectedWatchId.value || !canManage.value) return
  await run(async () => {
    const result = await api(`/api/watches/${selectedWatchId.value}/reconcile`, { method: 'POST' })
    const refreshes = [loadSummary(), loadDashboard()]
    if (section.value === 'activity') refreshes.push(loadEvents(eventsPage.value.page || 1))
    if (section.value === 'files') refreshes.push(loadFiles())
    await Promise.all(refreshes)
    showToast(`Сверка завершена: проверено ${result.checked}, новых ${result.created}, изменённых ${result.updated}`)
  })
}

function selectWatch(id) {
  selectedWatchId.value = Number(id)
}

function chooseBranch(path) {
  selectedBranch.value = path
  fileFilters.path_prefix = path
  appliedFileFilters = { ...appliedFileFilters, path_prefix: path }
  requestedFilePage = 1
  fileRequestId += 1
  eventFilters.path_prefix = path
}

function branchIndent(depth) {
  return { paddingLeft: `${Math.max(0, depth - 1) * 22 + 10}px` }
}

function groupName(groupId) {
  return groups.value.find((item) => item.id === groupId)?.name || groupId
}

function watchAccess(item) {
  return item.scope_type === 'team'
    ? `Командная: ${(item.group_ids || []).map(groupName).join(', ') || 'группы не выбраны'}`
    : 'Личная папка'
}

function activityLabel(event) {
  if (event.node_type !== 'folder') return event.event_label
  return {
    created: 'Папка создана',
    renamed: 'Папка переименована',
    moved: 'Папка перемещена',
    copied: 'Папка скопирована',
    deleted: 'Папка удалена',
  }[event.event_type] || event.event_label
}

function fmtDate(value) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'Europe/Moscow',
  }).format(new Date(value))
}

function fmtTime(value) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    timeZone: 'Europe/Moscow',
  }).format(new Date(value))
}

function fmtSize(bytes) {
  const value = Number(bytes || 0)
  if (value < 1024) return `${value} Б`
  const units = ['КБ', 'МБ', 'ГБ', 'ТБ']
  let size = value / 1024
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size >= 10 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return '—'
  return Number(seconds) < 1 ? `${Math.round(Number(seconds) * 1000)} мс` : `${Number(seconds).toFixed(1)} с`
}

function downloadReport(table) {
  if (!selectedWatchId.value) return
  const params = { ...reportParams.value, cache_bust: Date.now() }
  const path = table === 'xlsx'
    ? `/api/reports/xlsx?${query(params)}`
    : `/api/reports/csv?${query({ ...params, table })}`
  const link = document.createElement('a')
  link.href = downloadUrl(path)
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  link.remove()
}

function downloadChanges() {
  if (!selectedWatchId.value || !changeFilters.date_from || !changeFilters.date_to) {
    error.value = 'Укажите начало и окончание периода'
    return
  }
  const dates = requestDates(changeFilters)
  const params = { ...reportParams.value, ...dates, cache_bust: Date.now() }
  const link = document.createElement('a')
  link.href = downloadUrl(`/api/reports/changes/xlsx?${query(params)}`)
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  link.remove()
}

async function saveReportSchedule() {
  if (!canManage.value || !selectedWatchId.value) return
  await run(async () => {
    reportSchedule.value = await api(`/api/reports/schedule/${selectedWatchId.value}`, {
      method: 'PUT',
      body: { ...scheduleForm },
    })
    showToast(scheduleForm.enabled ? 'Расписание сохранено' : 'Расписание отключено')
  })
}

async function runReportNow() {
  if (!canManage.value || !selectedWatchId.value) return
  await run(async () => {
    reportSchedule.value = await api(`/api/reports/schedule/${selectedWatchId.value}`, {
      method: 'PUT',
      body: { ...scheduleForm },
    })
    const result = await api(`/api/reports/schedule/${selectedWatchId.value}/run`, { method: 'POST' })
    await loadReportSchedule()
    showToast(`Отчёт сохранён: ${result.path}`)
  })
}

function pageRange(page, pages) {
  const result = []
  for (let value = Math.max(1, page - 2); value <= Math.min(pages, page + 2); value += 1) result.push(value)
  return result
}

function refreshWhenVisible() {
  if (!document.hidden && !modalOpen.value && !loading.value) refreshSection()
}

watch(section, refreshSection)
watch(selectedWatchId, async (value) => {
  localStorage.setItem('file-control-watch', String(value || 0))
  selectedBranch.value = ''
  requestedFilePage = 1
  fileRequestId += 1
  filesPage.value = { items: [], page: 1, pages: 1, total: 0 }
  if (value) await refreshSection()
})

onMounted(() =>
  run(async () => {
    await Promise.all([loadAccess(), loadWatches()])
    await Promise.all([loadDashboard(), loadSummary()])
    document.addEventListener('visibilitychange', refreshWhenVisible)
    refreshTimer = window.setInterval(refreshWhenVisible, REFRESH_INTERVAL_MS)
  }),
)
onBeforeUnmount(() => {
  window.clearInterval(refreshTimer)
  document.removeEventListener('visibilitychange', refreshWhenVisible)
})
</script>

<template>
  <div class="fc-app">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-icon">◎</span>
        <div><strong>Отчёт сдачи</strong><small>ExApp</small></div>
      </div>
      <nav>
        <button
          v-for="item in nav"
          :key="item.id"
          :class="{ active: section === item.id }"
          @click="switchSection(item.id)"
        >
          <span>{{ item.icon }}</span>{{ item.label }}
        </button>
      </nav>
      <div class="sidebar-footer">Содержимое файлов не копируется</div>
    </aside>

    <main class="main-content">
      <div v-if="loading" class="loading-line"></div>
      <header class="page-header">
        <div>
          <small class="eyebrow">СБОР И КОНТРОЛЬ ФАЙЛОВ</small>
          <h1>{{ currentNav?.label }}</h1>
          <p v-if="section === 'overview'">Состояние наблюдаемых папок и последние поступления</p>
          <p v-else-if="section === 'watches'">Отслеживаемые папки</p>
          <p v-else-if="section === 'files'">Все файлы на любой глубине вложенности</p>
          <p v-else-if="section === 'activity'">Хронология создания, изменения, перемещения и удаления</p>
          <p v-else>Выгрузка произвольной папки или всего наблюдения</p>
        </div>
        <div class="header-actions">
          <select
            v-if="section !== 'watches' && watches.length"
            :value="selectedWatchId"
            aria-label="Наблюдение"
            @change="selectWatch($event.target.value)"
          >
            <option v-for="item in watches" :key="item.id" :value="item.id">{{ item.name }}</option>
          </select>
          <div v-if="currentWatch && section !== 'watches'" class="sync-control">
            <button v-if="canManage" @click="reconcile">↻ Сверить</button>
            <small>
              Обновлено: {{ lastSync?.finished_at ? fmtDate(lastSync.finished_at) : 'сверка ещё не выполнялась' }}
              <span>автоматически раз в {{ summary?.reconcile_interval_minutes || 45 }} минут</span>
            </small>
          </div>
          <button v-if="canManage" class="primary" @click="openCreate">+ Наблюдение</button>
        </div>
      </header>

      <div v-if="error" class="alert alert-error"><span>{{ error }}</span><button @click="error = ''">×</button></div>

      <template v-if="section === 'overview'">
        <section v-if="!currentWatch" class="panel empty-state">
          <div>◎</div><h2>{{ canManage ? 'Создайте первое наблюдение' : 'Нет доступных наблюдений' }}</h2>
          <p v-if="canManage">Выберите папку Nextcloud — приложение начнёт фиксировать всё дерево внутри неё.</p>
          <p v-else>У вас пока нет доступа ни к одной отслеживаемой папке.</p>
          <button v-if="canManage" class="primary" @click="openCreate">Создать наблюдение</button>
        </section>
        <template v-else>
          <section class="watch-hero panel">
            <div>
              <span :class="['status-badge', `status-${currentWatch.status}`]">{{ statusLabels[currentWatch.status] }}</span>
              <h2>{{ currentWatch.name }}</h2>
              <code>{{ currentWatch.root_path }}</code>
            </div>
            <dl>
              <div><dt>Доступ</dt><dd>{{ watchAccess(currentWatch) }}</dd></div>
              <div><dt>Период</dt><dd>{{ dateInput(currentWatch.started_at) || 'без даты' }} — {{ dateInput(currentWatch.ended_at) || 'без даты' }}</dd></div>
            </dl>
          </section>

          <section class="metrics-grid metrics-five">
            <article class="metric-card"><strong>{{ summary?.metrics.files ?? 0 }}</strong><span>Файлов сейчас</span></article>
            <article class="metric-card tone-green"><strong>{{ summary?.metrics.received_today ?? 0 }}</strong><span>Получено сегодня</span></article>
            <article class="metric-card tone-blue"><strong>{{ summary?.metrics.updated_today ?? 0 }}</strong><span>Обновлено сегодня</span></article>
            <article class="metric-card tone-red"><strong>{{ summary?.metrics.deleted ?? 0 }}</strong><span>Удалено</span></article>
            <article class="metric-card tone-purple"><strong>{{ summary?.metrics.folders ?? 0 }}</strong><span>Папок</span></article>
          </section>

          <section class="panel diagnostics-panel">
            <div class="panel-title"><div><h2>Диагностика наблюдения</h2><p>Состояние доставки событий и последней сверки</p></div></div>
            <dl>
              <div><dt>Последний webhook</dt><dd>{{ fmtDate(summary?.diagnostics?.last_webhook_at) }}</dd></div>
              <div><dt>Длительность сверки</dt><dd>{{ fmtDuration(summary?.diagnostics?.reconciliation_duration_seconds) }}</dd></div>
              <div><dt>Расхождений</dt><dd>{{ summary?.diagnostics?.discrepancies ?? '—' }}</dd></div>
              <div>
                <dt>Последняя ошибка</dt>
                <dd :class="{ 'diagnostic-error': summary?.diagnostics?.last_error && !summary?.diagnostics?.last_error_resolved }">
                  {{ summary?.diagnostics?.last_error || 'Ошибок нет' }}
                  <small v-if="summary?.diagnostics?.last_error_at">{{ fmtDate(summary.diagnostics.last_error_at) }}</small>
                  <small v-if="summary?.diagnostics?.last_error_resolved">После ошибки сверка прошла успешно</small>
                </dd>
              </div>
            </dl>
          </section>

          <div class="overview-grid">
            <section class="panel table-panel">
              <div class="panel-title"><div><h2>Последние поступления</h2><p>Время регистрации приложением</p></div><button @click="switchSection('files')">Все файлы →</button></div>
              <div class="table-scroll">
                <table>
                  <thead><tr><th>Поступил</th><th>Путь</th><th>Файл</th><th>Размер</th></tr></thead>
                  <tbody>
                    <tr v-for="file in summary?.recent_files || []" :key="file.id">
                      <td class="nowrap">{{ fmtDate(file.first_seen_at) }}</td>
                      <td class="path-cell" :title="file.parent_path">{{ file.parent_path || 'Корень наблюдения' }}</td>
                      <td><strong>{{ file.filename }}</strong></td>
                      <td class="nowrap">{{ fmtSize(file.size) }}</td>
                    </tr>
                  </tbody>
                </table>
                <div v-if="!summary?.recent_files?.length" class="empty-row">Файлы ещё не зарегистрированы</div>
              </div>
            </section>

            <section class="panel tree-panel">
              <div class="panel-title"><div><h2>Дерево папок</h2><p>Строится автоматически из путей</p></div></div>
              <button
                v-for="folder in summary?.tree || []"
                :key="folder.path"
                :class="['tree-row', { selected: selectedBranch === folder.path }]"
                :style="branchIndent(folder.depth)"
                @click="chooseBranch(folder.path)"
              >
                <span>▾</span><strong>{{ folder.name }}</strong><small>{{ folder.files }} файлов</small>
              </button>
              <div v-if="!summary?.tree?.length" class="empty-row">Дерево появится после первого файла</div>
            </section>
          </div>

          <section v-if="branchStats" class="panel branch-card">
            <div><small>ВЫБРАННАЯ ВЕТКА</small><h2>{{ branchStats.path }}</h2></div>
            <dl>
              <div><dt>Файлов</dt><dd>{{ branchStats.files }}</dd></div>
              <div><dt>Первый файл</dt><dd>{{ fmtDate(branchStats.first_seen_at) }}</dd></div>
              <div><dt>Последний файл</dt><dd>{{ fmtDate(branchStats.last_seen_at) }}</dd></div>
              <div><dt>Обновлений</dt><dd>{{ branchStats.updates }}</dd></div>
              <div><dt>Удалено</dt><dd>{{ branchStats.deleted }}</dd></div>
            </dl>
            <button class="primary" @click="switchSection('reports')">Выгрузить эту ветку</button>
          </section>
        </template>
      </template>

      <template v-else-if="section === 'watches'">
        <section class="panel table-panel">
          <div class="panel-title"><div><h2>Наблюдения</h2><p>{{ watches.length }} настроено</p></div></div>
          <div class="table-scroll">
            <table>
              <thead><tr><th>Название</th><th>Папка</th><th>Период</th><th>Файлов</th><th>Статус</th><th v-if="canManage"></th></tr></thead>
              <tbody>
                <tr v-for="item in watches" :key="item.id">
                  <td><strong>{{ item.name }}</strong><small>{{ watchAccess(item) }}</small></td>
                  <td class="path-cell" :title="item.root_path">{{ item.root_path }}</td>
                  <td class="nowrap">{{ dateInput(item.started_at) || '—' }} — {{ dateInput(item.ended_at) || '—' }}</td>
                  <td>{{ item.counts?.files || 0 }}</td>
                  <td><span :class="['status-badge', `status-${item.status}`]">{{ statusLabels[item.status] }}</span></td>
                  <td v-if="canManage" class="row-actions"><button @click="openEdit(item)">Изменить</button><button class="danger-link" @click="archiveWatch(item)">В архив</button></td>
                </tr>
              </tbody>
            </table>
            <div v-if="!watches.length" class="empty-row">Наблюдений пока нет</div>
          </div>
        </section>
      </template>

      <template v-else-if="section === 'files'">
        <section v-if="currentWatch" class="filter-bar">
          <input v-model="fileFilters.search" placeholder="Поиск по пути или имени" @keyup.enter="applyFileFilters" />
          <input v-model="fileFilters.path_prefix" placeholder="Путь начинается с…" @keyup.enter="applyFileFilters" />
          <input v-model="fileFilters.extension" class="short" placeholder="Тип: xlsx" @keyup.enter="applyFileFilters" />
          <select v-model="fileFilters.status"><option value="">Все состояния</option><option value="new">Новые</option><option value="updated">Обновлённые</option><option value="deleted">Удалённые</option></select>
          <input v-model="fileFilters.date_from" type="date" title="Дата от" />
          <input v-model="fileFilters.date_to" type="date" title="Дата до" />
          <button class="primary" @click="applyFileFilters">Применить</button>
        </section>
        <section class="panel table-panel">
          <div class="panel-title"><div><h2>Файлы</h2><p>{{ filesPage.total }} записей</p></div></div>
          <div class="table-scroll">
            <table class="files-table">
              <thead><tr><th>Путь</th><th>Файл</th><th>Поступил</th><th>Обновлён</th><th>Размер</th><th>Статус</th></tr></thead>
              <tbody>
                <tr v-for="file in filesPage.items" :key="file.id">
                  <td class="path-cell" :title="file.parent_path">{{ file.parent_path || 'Корень наблюдения' }}</td>
                  <td><strong>{{ file.filename }}</strong><small>{{ file.mime_type }}</small></td>
                  <td class="nowrap">{{ fmtDate(file.first_seen_at) }}</td>
                  <td class="nowrap">{{ file.revision_count ? fmtDate(file.last_seen_at) : '—' }}</td>
                  <td class="nowrap">{{ file.exists ? fmtSize(file.size) : '—' }}</td>
                  <td><span :class="['status-badge', `status-${file.state}`]">{{ statusLabels[file.state] }}</span></td>
                </tr>
              </tbody>
            </table>
            <div v-if="!filesPage.items.length" class="empty-row">По выбранным условиям файлов нет</div>
          </div>
          <div v-if="filesPage.pages > 1" class="pagination">
            <button :disabled="filesPage.page <= 1" @click="loadFiles(filesPage.page - 1)">←</button>
            <button v-for="page in pageRange(filesPage.page, filesPage.pages)" :key="page" :class="{ active: page === filesPage.page }" @click="loadFiles(page)">{{ page }}</button>
            <button :disabled="filesPage.page >= filesPage.pages" @click="loadFiles(filesPage.page + 1)">→</button>
          </div>
        </section>
      </template>

      <template v-else-if="section === 'activity'">
        <section v-if="currentWatch" class="filter-bar">
          <input v-model="eventFilters.search" placeholder="Поиск по пути или имени" @keyup.enter="loadEvents(1)" />
          <input v-model="eventFilters.path_prefix" placeholder="Путь начинается с…" @keyup.enter="loadEvents(1)" />
          <select v-model="eventFilters.event_type"><option value="">Все действия</option><option value="created">Создан</option><option value="updated">Обновлён</option><option value="renamed">Переименован</option><option value="moved">Перемещён</option><option value="copied">Скопирован</option><option value="deleted">Удалён</option></select>
          <input v-model="eventFilters.date_from" type="date" title="Дата от" />
          <input v-model="eventFilters.date_to" type="date" title="Дата до" />
          <button class="primary" @click="loadEvents(1)">Применить</button>
        </section>
        <section class="panel activity-panel">
          <div class="panel-title"><div><h2>Хронология</h2><p>{{ eventsPage.total }} событий</p></div></div>
          <article v-for="event in eventsPage.items" :key="event.id" class="activity-item">
            <time>{{ fmtTime(event.event_time) }}</time>
            <span :class="['activity-icon', `event-${event.event_type}`]">{{ eventIcons[event.event_type] }}</span>
            <div>
              <strong>{{ activityLabel(event) }}</strong>
              <p>{{ event.path }}</p>
              <small v-if="event.old_path && event.old_path !== event.path">из: {{ event.old_path }}</small>
            </div>
          </article>
          <div v-if="!eventsPage.items.length" class="empty-row">Событий пока нет</div>
          <div v-if="eventsPage.pages > 1" class="pagination">
            <button :disabled="eventsPage.page <= 1" @click="loadEvents(eventsPage.page - 1)">←</button>
            <button v-for="page in pageRange(eventsPage.page, eventsPage.pages)" :key="page" :class="{ active: page === eventsPage.page }" @click="loadEvents(page)">{{ page }}</button>
            <button :disabled="eventsPage.page >= eventsPage.pages" @click="loadEvents(eventsPage.page + 1)">→</button>
          </div>
        </section>
      </template>

      <template v-else-if="section === 'reports'">
        <section v-if="currentWatch" class="panel report-scope">
          <div><small class="eyebrow">ОБЛАСТЬ ВЫГРУЗКИ</small><h2>{{ selectedBranch || 'Всё наблюдение' }}</h2><p>{{ currentWatch.root_path }}<template v-if="selectedBranch">/{{ selectedBranch }}</template></p></div>
          <select v-model="selectedBranch">
            <option value="">Всё наблюдение</option>
            <option v-for="folder in summary?.tree || []" :key="folder.path" :value="folder.path">{{ '— '.repeat(Math.max(0, folder.depth - 1)) }}{{ folder.path }}</option>
          </select>
        </section>
        <section class="reports-grid">
          <article class="report-card primary-report">
            <span class="report-icon">X</span>
            <div><h2>Полный отчёт Excel</h2><p>Листы «Файлы», «Активность», «По папкам» и пояснения к полям. Каждый раз формируется новый файл.</p></div>
            <button v-if="selectedWatchId" class="button primary" type="button" @click="downloadReport('xlsx')">Скачать XLSX</button>
          </article>
          <article class="report-card">
            <span class="report-icon">C</span>
            <div><h2>Список файлов CSV</h2><p>Пути, время поступления и изменения, размер и состояние.</p></div>
            <button v-if="selectedWatchId" class="button" type="button" @click="downloadReport('files')">Скачать CSV</button>
          </article>
          <article class="report-card">
            <span class="report-icon">A</span>
            <div><h2>Активность CSV</h2><p>Полная хронология событий с прежними и новыми путями.</p></div>
            <button v-if="selectedWatchId" class="button" type="button" @click="downloadReport('events')">Скачать CSV</button>
          </article>
          <article class="report-card">
            <span class="report-icon">D</span>
            <div><h2>По папкам CSV</h2><p>Автоматическая агрегация по каждой ветке дерева.</p></div>
            <button v-if="selectedWatchId" class="button" type="button" @click="downloadReport('folders')">Скачать CSV</button>
          </article>
          <article class="report-card changes-report">
            <span class="report-icon">Δ</span>
            <div>
              <h2>Изменения за период</h2>
              <p>Добавленные, изменённые, перемещённые и удалённые файлы и папки. Время — московское.</p>
              <div class="inline-dates">
                <label><span>С</span><input v-model="changeFilters.date_from" type="date" /></label>
                <label><span>По</span><input v-model="changeFilters.date_to" type="date" /></label>
              </div>
            </div>
            <button v-if="selectedWatchId" class="button" type="button" @click="downloadChanges">Скачать XLSX</button>
          </article>
        </section>

        <section v-if="currentWatch && canManage" class="panel schedule-panel">
          <div class="panel-title">
            <div><h2>Автоматические отчёты</h2><p>Полный XLSX сохраняется в выбранную папку Nextcloud по московскому времени</p></div>
            <label class="toggle-label"><input v-model="scheduleForm.enabled" type="checkbox" /><span>Включено</span></label>
          </div>
          <div class="schedule-grid">
            <label><span>Периодичность</span><select v-model="scheduleForm.cadence"><option value="daily">Ежедневно</option><option value="weekly">Еженедельно</option><option value="monthly">Ежемесячно</option></select></label>
            <label><span>Время (МСК)</span><input v-model="scheduleForm.local_time" type="time" required /></label>
            <label v-if="scheduleForm.cadence === 'weekly'"><span>День недели</span><select v-model.number="scheduleForm.weekday"><option v-for="(label, index) in weekdayLabels" :key="label" :value="index">{{ label }}</option></select></label>
            <label v-if="scheduleForm.cadence === 'monthly'"><span>День месяца</span><input v-model.number="scheduleForm.day_of_month" type="number" min="1" max="28" /></label>
            <label class="schedule-folder"><span>Папка Nextcloud</span><div class="folder-input"><input v-model="scheduleForm.destination_path" readonly placeholder="Папка не выбрана" /><button type="button" @click="chooseReportFolder">Выбрать</button></div></label>
          </div>
          <div v-if="reportSchedule" class="schedule-status">
            <span>Следующий запуск: <strong>{{ fmtDate(reportSchedule.next_run_at) }}</strong></span>
            <span>Последний результат: <strong>{{ reportSchedule.last_status === 'completed' ? 'успешно' : reportSchedule.last_status === 'failed' ? 'ошибка' : reportSchedule.last_status === 'running' ? 'выполняется' : 'ещё не запускался' }}</strong></span>
            <span v-if="reportSchedule.last_file_path">Файл: <code>{{ reportSchedule.last_file_path }}</code></span>
            <span v-if="reportSchedule.last_error" class="diagnostic-error">{{ reportSchedule.last_error }}</span>
          </div>
          <footer class="schedule-actions"><button type="button" @click="saveReportSchedule">Сохранить</button><button class="primary" type="button" :disabled="!scheduleForm.destination_path" @click="runReportNow">Сформировать сейчас</button></footer>
          <p class="info-box">Папка отчётов не должна находиться внутри наблюдаемой папки — приложение проверит это перед сохранением.</p>
        </section>
      </template>
    </main>

    <div v-if="modalOpen && canManage" class="modal-backdrop" @click.self="modalOpen = false">
      <form class="modal-card" @submit.prevent="saveWatch">
        <header class="modal-header"><div><small class="eyebrow">{{ editingId ? 'НАСТРОЙКА' : 'НОВОЕ НАБЛЮДЕНИЕ' }}</small><h2>{{ editingId ? 'Изменить наблюдение' : 'Наблюдать за папкой' }}</h2></div><button type="button" class="icon-button" @click="modalOpen = false">×</button></header>
        <div class="modal-body">
          <div class="form-grid">
            <label class="wide"><span>Название</span><input v-model="watchForm.name" required placeholder="Например, Олимпиада 2026" /></label>
            <label><span>Дата начала</span><input v-model="watchForm.started_at" type="date" /></label>
            <label><span>Дата окончания</span><input v-model="watchForm.ended_at" type="date" /></label>
            <label><span>Статус</span><select v-model="watchForm.status"><option value="active">Активно</option><option value="paused">Приостановлено</option><option value="completed">Завершено</option><option value="draft">Черновик</option></select></label>
            <label>
              <span>Тип папки</span>
              <span class="readonly-field">
                {{ folderInspecting ? 'Определяется…' : watchForm.scope_type === 'team' ? 'Командная' : watchForm.scope_type === 'personal' ? 'Личная' : 'Определится после выбора' }}
              </span>
            </label>
            <label class="wide"><span>Папка</span><div class="folder-input"><input v-model="watchForm.root_path" required readonly placeholder="Папка не выбрана" /><button type="button" @click="chooseFolder">Выбрать</button></div></label>
          </div>
          <div v-if="watchForm.scope_type === 'team'" class="group-picker">
            <div><strong>Кому доступна командная папка</strong><small>Определено автоматически по настройкам Team folders в Nextcloud</small></div>
            <div v-for="group in groups" :key="group.id" class="group-info">
              <span>{{ group.name }}</span><small>доступ настроен в Nextcloud</small>
            </div>
            <div v-if="!groups.length" class="empty-row">К папке не назначены группы</div>
          </div>
          <div v-else-if="watchForm.scope_type === 'personal'" class="group-picker">
            <div><strong>Доступные группы</strong><small>Для личной папки можно выбрать группы из полного списка Nextcloud</small></div>
            <label v-for="group in groups" :key="group.id">
              <input v-model="watchForm.group_ids" type="checkbox" :value="group.id" />
              <span>{{ group.name }}</span><small>{{ group.users == null ? '' : `${group.users} польз.` }}</small>
            </label>
            <div v-if="!groups.length" class="empty-row">Доступные группы не найдены</div>
          </div>
          <p class="info-box">
            Приложение учитывает всё дерево ниже выбранной папки. Тип командной папки и назначенные ей группы
            определяются автоматически; права доступа по-прежнему настраиваются в Nextcloud.
          </p>
        </div>
        <footer class="modal-footer"><button type="button" @click="modalOpen = false">Отмена</button><span></span><button type="submit" class="primary" :disabled="folderInspecting || !folderVerified">{{ editingId ? 'Сохранить' : 'Создать наблюдение' }}</button></footer>
      </form>
    </div>

    <div v-if="toast" class="toast">{{ toast }}</div>
  </div>
</template>
