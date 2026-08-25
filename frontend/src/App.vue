<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { api, downloadUrl, query } from './api'
import AppModal from './components/AppModal.vue'
import AppPagination from './components/AppPagination.vue'
import MetricCard from './components/MetricCard.vue'
import StatusBadge from './components/StatusBadge.vue'

const section = ref('overview')
const campaignTab = ref('overview')
const busy = ref(false)
const error = ref('')
const toast = ref('')
const dashboard = ref(null)
const campaigns = ref([])
const selectedCampaign = ref(null)
const summary = ref(null)
const progress = ref(null)
const files = ref({ items: [], page: 1, pages: 0, total: 0 })
const events = ref({ items: [], page: 1, pages: 0, total: 0 })
const matrix = ref(null)
const systemStatus = ref(null)
const organizations = ref({ items: [], page: 1, pages: 0, total: 0 })
const expandedOrganizations = reactive(new Set())

const campaignFilters = reactive({ status: '', year: '', search: '' })
const orgFilters = reactive({ search: '', active: '' })
const progressFilters = reactive({ status: '', search: '', view: 'table' })
const progressDimensionFilters = reactive({})
const fileFilters = reactive({
  search: '', organization_id: '', parse_status: '', deleted: '', updated_only: false,
  overdue_only: false, attention_only: false, extension: '', dimension_key: '', dimension_value: '',
  date_from: '', date_to: '', sort: 'first_received_at', direction: 'desc',
})
const eventFilters = reactive({
  search: '', organization_id: '', event_type: '', date_from: '', date_to: '',
  dimension_key: '', dimension_value: '',
})
const matrixConfig = reactive({ column_dimension: '', filter_dimension: '', filter_value: '' })

const showCampaignWizard = ref(false)
const showOrganizationModal = ref(false)
const showImportModal = ref(false)
const importKind = ref('organizations')
const importFile = ref(null)
const importPreview = ref(null)
const editingOrganization = ref(null)
const folders = ref([])
const folderPath = ref('')

const wizard = reactive({
  step: 1,
  name: '', description: '', campaign_type: 'collection', year: new Date().getFullYear(),
  start_at: '', deadline_at: '', root_path: '', storage_user_id: '', status: 'draft',
  organization_ids: [], structure_mode: 'strict', requirements_mode: 'simple', requirement_file: null,
  dimensions: [
    { key: 'subject', name: 'Предмет', required: true, fixed: true, valuesText: '' },
    { key: 'grade', name: 'Параллель', required: true, fixed: true, valuesText: '7 класс\n8 класс\n9 класс\n10 класс\n11 класс' },
  ],
})
const orgForm = reactive({ code: '', name: '', short_name: '', folder_name: '', email: '', active: true })
const campaignSettings = reactive({ name: '', description: '', deadline_at: '', status: '' })

const activeCampaigns = computed(() => campaigns.value.filter((item) => item.status === 'active'))
const campaignYears = computed(() => [...new Set(campaigns.value.map((item) => item.year))].sort((a, b) => b - a))
const dimensions = computed(() => selectedCampaign.value?.dimensions || [])
const participantOrganizations = computed(() => progress.value?.organizations?.map((item) => item.organization) || [])

function notify(message) {
  toast.value = message
  window.setTimeout(() => { if (toast.value === message) toast.value = '' }, 5000)
}

async function run(task) {
  busy.value = true
  error.value = ''
  try { return await task() }
  catch (err) { error.value = err.message || String(err); throw err }
  finally { busy.value = false }
}

function fmtDate(value, short = false) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('ru-RU', short
    ? { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }
    : { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value))
}

function fmtSize(value) {
  const bytes = Number(value || 0)
  if (bytes < 1024) return `${bytes} Б`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} КБ`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} МБ`
  return `${(bytes / 1024 ** 3).toFixed(1)} ГБ`
}

function toInput(value) {
  if (!value) return ''
  const date = new Date(value)
  const offset = date.getTimezoneOffset() * 60000
  return new Date(date.getTime() - offset).toISOString().slice(0, 16)
}

function toIso(value) { return value ? new Date(value).toISOString() : null }

async function loadDashboard() {
  dashboard.value = await api('/api/dashboard')
}

async function loadCampaigns() {
  campaigns.value = await api(`/api/campaigns?${query(campaignFilters)}`)
}

async function loadOrganizations(page = 1) {
  organizations.value = await api(`/api/organizations?${query({ ...orgFilters, page, page_size: 50 })}`)
}

async function refreshTopLevel() {
  await Promise.all([loadDashboard(), loadCampaigns(), loadOrganizations(1)])
}

async function openCampaign(id, tab = 'overview') {
  await run(async () => {
    selectedCampaign.value = await api(`/api/campaigns/${id}`)
    Object.keys(progressDimensionFilters).forEach((key) => delete progressDimensionFilters[key])
    campaignTab.value = tab
    section.value = 'campaign'
    Object.assign(campaignSettings, {
      name: selectedCampaign.value.name,
      description: selectedCampaign.value.description,
      deadline_at: toInput(selectedCampaign.value.deadline_at),
      status: selectedCampaign.value.status,
    })
    await loadCampaignTab(tab)
  }).catch(() => {})
}

async function loadCampaignTab(tab = campaignTab.value) {
  if (!selectedCampaign.value) return
  const id = selectedCampaign.value.id
  if (tab === 'overview') summary.value = await api(`/api/campaigns/${id}/summary`)
  if (tab === 'progress') await loadProgress()
  if (tab === 'files') await loadFiles(1)
  if (tab === 'events') await loadEvents(1)
}

async function switchCampaignTab(tab) {
  campaignTab.value = tab
  await run(() => loadCampaignTab(tab)).catch(() => {})
}

async function loadProgress() {
  const dimensionsFilter = Object.fromEntries(
    Object.entries(progressDimensionFilters).filter(([, value]) => value),
  )
  progress.value = await api(`/api/campaigns/${selectedCampaign.value.id}/progress?${query({
    status: progressFilters.status,
    search: progressFilters.search,
    dimensions: Object.keys(dimensionsFilter).length ? JSON.stringify(dimensionsFilter) : '',
  })}`)
  if (progressFilters.view === 'matrix') await loadMatrix()
}

async function loadMatrix() {
  if (!matrixConfig.column_dimension) {
    matrixConfig.column_dimension = dimensions.value[1]?.key || dimensions.value[0]?.key || ''
  }
  if (!matrixConfig.column_dimension) return
  matrix.value = await api(`/api/campaigns/${selectedCampaign.value.id}/matrix?${query(matrixConfig)}`)
}

async function loadFiles(page = 1) {
  files.value = await api(`/api/campaigns/${selectedCampaign.value.id}/files?${query({ ...fileFilters, page, page_size: 50 })}`)
}

async function loadEvents(page = 1) {
  events.value = await api(`/api/campaigns/${selectedCampaign.value.id}/events?${query({ ...eventFilters, page, page_size: 50 })}`)
}

function toggleOrg(id) {
  expandedOrganizations.has(id) ? expandedOrganizations.delete(id) : expandedOrganizations.add(id)
}

function resetWizard() {
  Object.assign(wizard, {
    step: 1, name: '', description: '', campaign_type: 'collection', year: new Date().getFullYear(),
    start_at: '', deadline_at: '', root_path: '', storage_user_id: '', status: 'draft',
    organization_ids: organizations.value.items.filter((item) => item.active).map((item) => item.id),
    structure_mode: 'strict', requirements_mode: 'simple', requirement_file: null,
    dimensions: [
      { key: 'subject', name: 'Предмет', required: true, fixed: true, valuesText: '' },
      { key: 'grade', name: 'Параллель', required: true, fixed: true, valuesText: '7 класс\n8 класс\n9 класс\n10 класс\n11 класс' },
    ],
  })
  showCampaignWizard.value = true
}

function addDimension() {
  wizard.dimensions.push({ key: `level_${wizard.dimensions.length + 1}`, name: 'Новый уровень', required: false, fixed: true, valuesText: '' })
}

async function browseFolders(path = folderPath.value) {
  await run(async () => {
    const data = await api(`/api/folders?${query({ path, user_id: wizard.storage_user_id })}`)
    folders.value = data.items
    folderPath.value = data.path || path
    if (!wizard.storage_user_id) wizard.storage_user_id = data.user_id
  }).catch(() => {})
}

function selectFolder(path) {
  wizard.root_path = path
  folderPath.value = path
  browseFolders(path)
}

async function createCampaign() {
  await run(async () => {
    const payload = {
      name: wizard.name, description: wizard.description, campaign_type: wizard.campaign_type,
      year: Number(wizard.year), start_at: toIso(wizard.start_at), deadline_at: toIso(wizard.deadline_at),
      root_path: wizard.root_path, storage_user_id: wizard.storage_user_id || null, status: wizard.status,
      structure_mode: wizard.structure_mode, requirements_mode: wizard.requirements_mode,
      organization_ids: wizard.organization_ids,
      dimensions: wizard.dimensions.map((dimension) => ({
        key: dimension.key, name: dimension.name, required: dimension.required, fixed: dimension.fixed,
        values: dimension.valuesText.split(/\r?\n|;/).map((value) => value.trim()).filter(Boolean)
          .map((value, index) => ({ value, display_name: value, sort_order: index })),
      })),
    }
    const created = await api('/api/campaigns', { method: 'POST', body: payload })
    if (wizard.requirements_mode === 'matrix' && wizard.requirement_file) {
      const form = new FormData(); form.append('file', wizard.requirement_file)
      await api(`/api/import/requirements?campaign_id=${created.id}&commit=true`, { method: 'POST', body: form })
    }
    showCampaignWizard.value = false
    notify('Кампания создана')
    await refreshTopLevel()
    await openCampaign(created.id)
  }).catch(() => {})
}

function editOrganization(item = null) {
  editingOrganization.value = item
  Object.assign(orgForm, item || { code: '', name: '', short_name: '', folder_name: '', email: '', active: true })
  showOrganizationModal.value = true
}

async function saveOrganization() {
  await run(async () => {
    const path = editingOrganization.value ? `/api/organizations/${editingOrganization.value.id}` : '/api/organizations'
    await api(path, { method: editingOrganization.value ? 'PATCH' : 'POST', body: { ...orgForm } })
    showOrganizationModal.value = false
    notify(editingOrganization.value ? 'Организация обновлена' : 'Организация добавлена')
    await loadOrganizations(organizations.value.page || 1)
  }).catch(() => {})
}

function openImport(kind) {
  importKind.value = kind
  importFile.value = null
  importPreview.value = null
  showImportModal.value = true
}

async function previewImport(commit = false) {
  if (!importFile.value) return
  await run(async () => {
    const form = new FormData(); form.append('file', importFile.value)
    const campaignParam = importKind.value === 'organizations' ? '' : `&campaign_id=${selectedCampaign.value.id}`
    importPreview.value = await api(`/api/import/${importKind.value}?commit=${commit}${campaignParam}`, { method: 'POST', body: form })
    if (commit) {
      notify(`Импорт завершён: добавлено ${importPreview.value.to_add}, обновлено ${importPreview.value.to_update}`)
      showImportModal.value = false
      if (importKind.value === 'organizations') await loadOrganizations(1)
      else selectedCampaign.value = await api(`/api/campaigns/${selectedCampaign.value.id}`)
    }
  }).catch(() => {})
}

async function reconcile() {
  await run(async () => {
    const result = await api(`/api/campaigns/${selectedCampaign.value.id}/reconcile`, { method: 'POST' })
    notify(`Сверка завершена. Проверено ${result.checked}; новых ${result.created}; обновлено ${result.updated}; отсутствует ${result.missing}.`)
    await loadCampaignTab(campaignTab.value)
  }).catch(() => {})
}

async function saveCampaignSettings() {
  await run(async () => {
    selectedCampaign.value = await api(`/api/campaigns/${selectedCampaign.value.id}`, {
      method: 'PATCH',
      body: {
        name: campaignSettings.name,
        description: campaignSettings.description,
        deadline_at: toIso(campaignSettings.deadline_at),
        status: campaignSettings.status,
      },
    })
    notify('Настройки кампании сохранены')
    await refreshTopLevel()
  }).catch(() => {})
}

async function cloneCampaign() {
  const year = Number(window.prompt('Год новой кампании', String(selectedCampaign.value.year + 1)))
  if (!year) return
  const name = window.prompt('Название новой кампании', selectedCampaign.value.name.replace(String(selectedCampaign.value.year), String(year)))
  if (!name) return
  const root = window.prompt('Корневая папка новой кампании', selectedCampaign.value.root_path.replace(String(selectedCampaign.value.year), String(year)))
  if (!root) return
  await run(async () => {
    const created = await api(`/api/campaigns/${selectedCampaign.value.id}/clone`, {
      method: 'POST', body: { name, year, root_path: root, copy_requirements: true },
    })
    notify('Создан новый черновик на основе кампании')
    await refreshTopLevel()
    await openCampaign(created.id)
  }).catch(() => {})
}

async function ignoreFile(file) {
  await run(async () => {
    await api(`/api/campaigns/${selectedCampaign.value.id}/files/${file.id}/ignore?ignored=true`, { method: 'PATCH' })
    notify('Предупреждение отмечено как игнорируемое')
    await loadFiles(files.value.page)
  }).catch(() => {})
}

async function reclassifyFiles() {
  await run(async () => {
    const result = await api(`/api/campaigns/${selectedCampaign.value.id}/reclassify`, { method: 'POST' })
    notify(`Распознавание завершено. Изменено записей: ${result.changed}.`)
    await loadCampaignTab(campaignTab.value)
  }).catch(() => {})
}

function openInNextcloud(file) {
  const webroot = window.OC?.webroot || ''
  window.open(`${webroot}/index.php/apps/files/?dir=${encodeURIComponent(file.directory)}`, '_blank', 'noopener')
}

function reportHref(format, table = '') {
  const params = { campaign_id: selectedCampaign.value.id }
  if (table) params.table = table
  if (table === 'progress') {
    const dimensionsFilter = Object.fromEntries(
      Object.entries(progressDimensionFilters).filter(([, value]) => value),
    )
    Object.assign(params, {
      status: progressFilters.status,
      search: progressFilters.search,
      dimensions: Object.keys(dimensionsFilter).length ? JSON.stringify(dimensionsFilter) : '',
    })
  }
  if (table === 'files') Object.assign(params, fileFilters)
  if (table === 'events') Object.assign(params, eventFilters)
  return downloadUrl(`/api/reports/${format}?${query(params)}`)
}

async function loadSystemStatus() {
  await run(async () => { systemStatus.value = await api('/api/system/status') }).catch(() => {})
}

watch(() => progressFilters.view, async (value) => { if (value === 'matrix' && selectedCampaign.value) await run(loadMatrix).catch(() => {}) })

onMounted(async () => {
  await run(refreshTopLevel).catch(() => {})
})
</script>

<template>
  <div class="fc-app">
    <aside class="sidebar">
      <div class="brand"><span class="brand-icon">▦</span><span>Сбор файлов</span></div>
      <nav aria-label="Основное меню">
        <button :class="{ active: section === 'overview' }" @click="section = 'overview'; selectedCampaign = null"><span>⌂</span>Обзор</button>
        <button :class="{ active: section === 'campaigns' || section === 'campaign' }" @click="section = 'campaigns'; selectedCampaign = null"><span>▣</span>Кампании</button>
        <button :class="{ active: section === 'organizations' }" @click="section = 'organizations'; selectedCampaign = null"><span>♙</span>Организации</button>
        <button :class="{ active: section === 'reports' }" @click="section = 'reports'; selectedCampaign = null"><span>⇩</span>Отчёты</button>
        <button :class="{ active: section === 'settings' }" @click="section = 'settings'; selectedCampaign = null; loadSystemStatus()"><span>⚙</span>Настройки</button>
      </nav>
      <div class="sidebar-footer">Данные файлов не копируются</div>
    </aside>

    <main class="main-content">
      <div v-if="busy" class="loading-line" aria-label="Загрузка"></div>
      <div v-if="error" class="alert alert-error"><span>{{ error }}</span><button @click="error = ''">×</button></div>
      <div v-if="toast" class="toast" role="status">✓ {{ toast }}</div>

      <template v-if="section === 'overview'">
        <header class="page-header">
          <div><p class="eyebrow">Центр контроля</p><h1>Обзор</h1><p>Текущее состояние активных кампаний</p></div>
          <button class="primary" @click="resetWizard">+ Новая кампания</button>
        </header>
        <div class="metrics-grid" v-if="dashboard">
          <MetricCard label="Активных кампаний" :value="dashboard.metrics.active_campaigns" />
          <MetricCard label="Файлов сегодня" :value="dashboard.metrics.files_today" tone="green" />
          <MetricCard label="Организаций с загрузками" :value="dashboard.metrics.organizations_today" tone="purple" />
          <MetricCard label="Требуют внимания" :value="dashboard.metrics.unrecognized" tone="orange" />
        </div>
        <section class="panel">
          <div class="panel-title"><div><h2>Активные кампании</h2><p>Откройте кампанию, чтобы увидеть подробности</p></div></div>
          <div v-if="!dashboard?.campaigns?.length" class="empty-state"><div>□</div><h3>Активных кампаний пока нет</h3><p>Создайте кампанию и выберите папку Nextcloud.</p></div>
          <div v-else class="campaign-card-grid">
            <button v-for="item in dashboard.campaigns" :key="item.campaign.id" class="campaign-card" @click="openCampaign(item.campaign.id)">
              <div class="campaign-card-header"><div><span class="campaign-year">{{ item.campaign.year }}</span><h3>{{ item.campaign.name }}</h3></div><StatusBadge :status="item.campaign.status" /></div>
              <div class="campaign-numbers">
                <span><strong>{{ item.summary.counts.submitted }}</strong> сдали</span>
                <span><strong>{{ item.summary.counts.partial }}</strong> частично</span>
                <span><strong>{{ item.summary.counts.not_submitted }}</strong> не начали</span>
              </div>
              <div class="progress-row"><div class="progress-track"><span :style="{ width: `${item.summary.percent}%` }"></span></div><strong>{{ item.summary.percent }}%</strong></div>
              <div class="campaign-card-footer"><span>Файлов: {{ item.summary.files.total }}</span><span>Требуют внимания: {{ item.summary.files.unrecognized }}</span></div>
            </button>
          </div>
        </section>
      </template>

      <template v-else-if="section === 'campaigns'">
        <header class="page-header">
          <div><p class="eyebrow">Управление</p><h1>Кампании</h1><p>Активные, завершённые и архивные периоды сбора</p></div>
          <button class="primary" @click="resetWizard">+ Новая кампания</button>
        </header>
        <div class="filter-bar">
          <input v-model="campaignFilters.search" placeholder="Поиск по названию" @keyup.enter="loadCampaigns" />
          <select v-model="campaignFilters.status" @change="loadCampaigns"><option value="">Все состояния</option><option value="draft">Черновики</option><option value="active">Активные</option><option value="completed">Завершённые</option><option value="archived">Архив</option></select>
          <select v-model="campaignFilters.year" @change="loadCampaigns"><option value="">Все годы</option><option v-for="year in campaignYears" :key="year" :value="year">{{ year }}</option></select>
          <button @click="loadCampaigns">Найти</button>
        </div>
        <section class="panel table-panel">
          <table><thead><tr><th>Кампания</th><th>Год</th><th>Папка</th><th>Срок</th><th>Состояние</th><th></th></tr></thead>
            <tbody><tr v-for="item in campaigns" :key="item.id" class="clickable" @click="openCampaign(item.id)"><td><strong>{{ item.name }}</strong><small>{{ item.description }}</small></td><td>{{ item.year }}</td><td class="path-cell">{{ item.root_path }}</td><td>{{ fmtDate(item.deadline_at) }}</td><td><StatusBadge :status="item.status" /></td><td>›</td></tr></tbody>
          </table>
        </section>
      </template>

      <template v-else-if="section === 'organizations'">
        <header class="page-header"><div><p class="eyebrow">Справочник</p><h1>Организации</h1><p>Организация определяется по имени папки первого уровня</p></div><div class="actions"><button @click="openImport('organizations')">⇧ Импорт</button><button class="primary" @click="editOrganization()">+ Добавить</button></div></header>
        <div class="filter-bar"><input v-model="orgFilters.search" placeholder="Название, код или папка" @keyup.enter="loadOrganizations(1)" /><select v-model="orgFilters.active" @change="loadOrganizations(1)"><option value="">Все</option><option value="true">Активные</option><option value="false">Неактивные</option></select><button @click="loadOrganizations(1)">Найти</button></div>
        <section class="panel table-panel"><table><thead><tr><th>Код</th><th>Организация</th><th>Краткое название</th><th>Имя папки</th><th>Email</th><th>Состояние</th><th></th></tr></thead>
          <tbody><tr v-for="item in organizations.items" :key="item.id"><td><strong>{{ item.code }}</strong></td><td>{{ item.name }}</td><td>{{ item.short_name || '—' }}</td><td><code>{{ item.folder_name }}</code></td><td>{{ item.email || '—' }}</td><td>{{ item.active ? 'Активна' : 'Отключена' }}</td><td><button class="link-button" @click="editOrganization(item)">Изменить</button></td></tr></tbody></table>
          <AppPagination :page="organizations.page" :pages="organizations.pages" :total="organizations.total" @change="loadOrganizations" />
        </section>
      </template>

      <template v-else-if="section === 'reports'">
        <header class="page-header"><div><p class="eyebrow">Выгрузка</p><h1>Отчёты</h1><p>Выберите кампанию и формат готового отчёта</p></div></header>
        <section class="panel report-list">
          <div v-for="item in campaigns" :key="item.id" class="report-row"><div><strong>{{ item.name }}</strong><span>{{ item.year }} · {{ item.root_path }}</span></div><div class="actions"><button @click="openCampaign(item.id, 'reports')">Открыть отчёты</button></div></div>
        </section>
      </template>

      <template v-else-if="section === 'settings'">
        <header class="page-header"><div><p class="eyebrow">Приложение</p><h1>Настройки</h1><p>Состояние подключения и права доступа</p></div></header>
        <section class="panel settings-grid" v-if="systemStatus">
          <div><h3>Состояние</h3><p><span class="health-dot"></span>Приложение работает</p><p>База данных: {{ systemStatus.database === 'connected' ? 'подключена' : systemStatus.database }}</p><p>Среда Nextcloud: {{ systemStatus.nextcloud_runtime ? 'да' : 'локальный режим' }}</p></div>
          <div><h3>Автоматическая сверка</h3><p>Каждые {{ systemStatus.reconcile_interval_minutes }} минут для активных кампаний.</p><p>Дополнительные службы и cron не требуются.</p></div>
          <div><h3>Группы администраторов</h3><p><code v-for="group in systemStatus.admin_groups" :key="group">{{ group }}</code></p><h3>Группы наблюдателей</h3><p><code v-for="group in systemStatus.observer_groups" :key="group">{{ group }}</code></p></div>
        </section>
      </template>

      <template v-else-if="section === 'campaign' && selectedCampaign">
        <header class="page-header campaign-header">
          <div><button class="back-button" @click="section = 'campaigns'; selectedCampaign = null">‹ Все кампании</button><p class="eyebrow">{{ selectedCampaign.year }} · {{ selectedCampaign.root_path }}</p><h1>{{ selectedCampaign.name }}</h1><p>{{ selectedCampaign.description }}</p></div>
          <div class="actions"><StatusBadge :status="selectedCampaign.status" /><button @click="reconcile">↻ Сверить с Nextcloud</button></div>
        </header>
        <nav class="tabs"><button v-for="tab in [['overview','Обзор'],['progress','Ход сдачи'],['files','Файлы'],['events','Активность'],['reports','Отчёты'],['settings','Настройки']]" :key="tab[0]" :class="{ active: campaignTab === tab[0] }" @click="switchCampaignTab(tab[0])">{{ tab[1] }}</button></nav>

        <template v-if="campaignTab === 'overview' && summary">
          <div class="metrics-grid campaign-metrics"><MetricCard label="Организаций" :value="summary.organizations.length" /><MetricCard label="Полностью сдали" :value="summary.counts.submitted" tone="green" /><MetricCard label="Частично" :value="summary.counts.partial" tone="orange" /><MetricCard label="Не начали" :value="summary.counts.not_submitted" tone="red" /><MetricCard label="Файлов" :value="summary.files.total" tone="purple" /><MetricCard label="Сегодня" :value="summary.files.today" tone="blue" /><MetricCard label="Удалено" :value="summary.files.deleted" tone="red" /><MetricCard label="Не распознано" :value="summary.files.unrecognized" tone="orange" /></div>
          <section class="panel progress-panel"><div class="panel-title"><div><h2>Общее выполнение</h2><p>{{ summary.completed }} из {{ summary.required }} обязательных сдач</p></div><strong class="big-percent">{{ summary.percent }}%</strong></div><div class="progress-track large"><span :style="{ width: `${summary.percent}%` }"></span></div></section>
          <section class="panel table-panel"><div class="panel-title"><div><h2>Организации</h2><p>Последнее состояние сдачи</p></div><button @click="switchCampaignTab('progress')">Все организации →</button></div><table><thead><tr><th>Организация</th><th>Выполнено</th><th>Последняя сдача</th><th>Статус</th></tr></thead><tbody><tr v-for="item in summary.organizations.slice(0, 10)" :key="item.organization.id"><td>{{ item.organization.name }}</td><td>{{ item.completed }} / {{ item.required }}</td><td>{{ fmtDate(item.last_received_at) }}</td><td><StatusBadge :status="item.status" /></td></tr></tbody></table></section>
        </template>

        <template v-else-if="campaignTab === 'progress'">
          <div class="filter-bar wrap">
            <div class="segmented"><button :class="{ active: progressFilters.view === 'table' }" @click="progressFilters.view='table'">Таблица</button><button :class="{ active: progressFilters.view === 'matrix' }" @click="progressFilters.view='matrix'">Матрица</button></div>
            <input v-model="progressFilters.search" placeholder="Поиск организации" @keyup.enter="loadProgress" />
            <select v-model="progressFilters.status" @change="loadProgress"><option value="">Все статусы</option><option value="submitted">Сдано</option><option value="partial">Частично</option><option value="not_submitted">Не сдано</option><option value="overdue">Просрочено</option></select>
            <label v-for="dimension in dimensions" :key="dimension.key" class="compact-filter">{{ dimension.name }}<select v-model="progressDimensionFilters[dimension.key]"><option value="">Все</option><option v-for="value in dimension.values" :key="value.id" :value="value.value">{{ value.display_name }}</option></select></label>
            <button @click="loadProgress">Применить</button>
            <a class="button" :href="reportHref('xlsx','progress')">⇩ Excel</a>
            <a class="button" :href="reportHref('csv','progress')">⇩ CSV</a>
          </div>
          <section v-if="progressFilters.view === 'table'" class="panel table-panel"><table><thead><tr><th>Организация</th><th>Выполнено</th><th>Требуется</th><th>Прогресс</th><th>Последняя сдача</th><th>Статус</th><th></th></tr></thead><tbody>
            <template v-for="item in progress?.organizations || []" :key="item.organization.id"><tr class="clickable" @click="toggleOrg(item.organization.id)"><td><strong>{{ item.organization.name }}</strong><small>Папка: {{ item.organization.folder_name }}</small></td><td>{{ item.completed }}</td><td>{{ item.required }}</td><td><div class="mini-progress"><span :style="{ width: `${item.percent}%` }"></span></div>{{ item.percent }}%</td><td>{{ fmtDate(item.last_received_at) }}</td><td><StatusBadge :status="item.status" /></td><td>{{ expandedOrganizations.has(item.organization.id) ? '⌃' : '⌄' }}</td></tr>
              <tr v-if="expandedOrganizations.has(item.organization.id)" class="detail-row"><td colspan="7"><div v-if="!item.requirements.length" class="empty-detail">В простом режиме достаточно любого файла в папке организации.</div><div v-else class="requirement-grid"><div v-for="req in item.requirements" :key="req.id" class="requirement-item"><div><strong>{{ Object.values(req.dimensions).join(' · ') }}</strong><small>{{ req.received }} из {{ req.required }} файлов · срок {{ fmtDate(req.deadline_at) }}</small></div><StatusBadge :status="req.status" /></div></div></td></tr>
            </template></tbody></table></section>
          <template v-else>
            <div class="matrix-controls"><label>Колонки<select v-model="matrixConfig.column_dimension" @change="loadMatrix"><option v-for="dimension in dimensions" :key="dimension.key" :value="dimension.key">{{ dimension.name }}</option></select></label><label>Отбор по уровню<select v-model="matrixConfig.filter_dimension"><option value="">Без отбора</option><option v-for="dimension in dimensions" :key="dimension.key" :value="dimension.key">{{ dimension.name }}</option></select></label><label>Значение<input v-model="matrixConfig.filter_value" placeholder="Например, Математика" /></label><button @click="loadMatrix">Показать</button></div>
            <section class="panel matrix-wrap"><table class="matrix"><thead><tr><th>Организация</th><th v-for="column in matrix?.columns || []" :key="column">{{ column }}</th></tr></thead><tbody><tr v-for="row in matrix?.rows || []" :key="row.organization.id"><th>{{ row.organization.short_name || row.organization.name }}</th><td v-for="column in matrix.columns" :key="column"><StatusBadge v-if="row.cells[column]" :status="row.cells[column]" /><span v-else class="not-required">—</span></td></tr></tbody></table></section>
          </template>
        </template>

        <template v-else-if="campaignTab === 'files'">
          <div class="filter-bar wrap">
            <input v-model="fileFilters.search" placeholder="Файл, организация или путь" @keyup.enter="loadFiles(1)" />
            <select v-model="fileFilters.organization_id"><option value="">Все организации</option><option v-for="org in participantOrganizations" :key="org.id" :value="org.id">{{ org.short_name || org.name }}</option></select>
            <select v-model="fileFilters.parse_status"><option value="">Любое распознавание</option><option value="unknown_organization">Неизвестная организация</option><option value="unknown_dimension">Неизвестное значение</option><option value="missing_dimension">Не хватает уровня</option><option value="outside_structure">Вне структуры</option></select>
            <select v-model="fileFilters.dimension_key"><option value="">Любой уровень</option><option v-for="dimension in dimensions" :key="dimension.key" :value="dimension.key">{{ dimension.name }}</option></select>
            <input v-model="fileFilters.dimension_value" placeholder="Значение уровня" />
            <input v-model="fileFilters.extension" class="short-input" placeholder="Расширение" />
            <input v-model="fileFilters.date_from" type="datetime-local" title="Поступили с даты" />
            <input v-model="fileFilters.date_to" type="datetime-local" title="Поступили до даты" />
            <label class="check"><input v-model="fileFilters.updated_only" type="checkbox" /> Обновлённые</label><label class="check"><input v-model="fileFilters.attention_only" type="checkbox" /> Требуют внимания</label><label class="check"><input v-model="fileFilters.deleted" type="checkbox" true-value="true" false-value="" /> Удалённые</label>
            <button @click="loadFiles(1)">Применить</button><a class="button" :href="reportHref('csv','files')">⇩ Выгрузить</a>
          </div>
          <section class="panel table-panel"><table><thead><tr><th>Организация</th><th>Параметры</th><th>Файл</th><th>Размер</th><th>Поступил</th><th>Обновлён</th><th>Состояние</th><th></th></tr></thead><tbody><tr v-for="file in files.items" :key="file.id"><td>{{ file.organization?.short_name || file.organization?.name || 'Не распознано' }}</td><td><span class="dimension-text">{{ Object.values(file.dimensions).join(' / ') || '—' }}</span></td><td><strong>{{ file.filename }}</strong><small class="path-cell">{{ file.full_path }}</small></td><td>{{ fmtSize(file.size) }}</td><td>{{ fmtDate(file.first_received_at, true) }}</td><td>{{ fmtDate(file.last_received_at, true) }}<small v-if="file.revision_count">{{ file.revision_count }} обновл.</small></td><td><StatusBadge :status="file.deleted ? 'deleted' : (file.parse_status !== 'ok' ? file.parse_status : (file.revision_count ? 'updated' : 'submitted'))" /><small v-if="file.parse_message">{{ file.parse_message }}</small></td><td><button class="link-button" @click="openInNextcloud(file)">Открыть</button><button v-if="file.parse_status !== 'ok' && file.parse_status !== 'ignored'" class="link-button" @click="ignoreFile(file)">Игнорировать</button></td></tr></tbody></table><AppPagination :page="files.page" :pages="files.pages" :total="files.total" @change="loadFiles" /></section>
        </template>

        <template v-else-if="campaignTab === 'events'">
          <div class="filter-bar wrap"><input v-model="eventFilters.search" placeholder="Файл или путь" @keyup.enter="loadEvents(1)" /><select v-model="eventFilters.organization_id"><option value="">Все организации</option><option v-for="org in participantOrganizations" :key="org.id" :value="org.id">{{ org.short_name || org.name }}</option></select><select v-model="eventFilters.event_type"><option value="">Все действия</option><option value="created">Загружен</option><option value="written">Обновлён</option><option value="renamed">Перемещён</option><option value="deleted">Удалён</option><option value="copied">Скопирован</option></select><select v-model="eventFilters.dimension_key"><option value="">Любой уровень</option><option v-for="dimension in dimensions" :key="dimension.key" :value="dimension.key">{{ dimension.name }}</option></select><input v-model="eventFilters.dimension_value" placeholder="Значение уровня" /><input v-model="eventFilters.date_from" type="datetime-local" title="С даты" /><input v-model="eventFilters.date_to" type="datetime-local" title="До даты" /><button @click="loadEvents(1)">Применить</button><a class="button" :href="reportHref('csv','events')">⇩ Выгрузить</a></div>
          <section class="panel activity-list"><article v-for="event in events.items" :key="event.id" class="activity-item"><time>{{ fmtDate(event.occurred_at) }}</time><div class="activity-icon">{{ {created:'↓',written:'↻',renamed:'→',deleted:'×',copied:'⧉'}[event.event_type] || '•' }}</div><div><strong>{{ event.file.organization }}</strong><span>{{ event.event_label }}</span><p>{{ Object.values(event.file.dimensions).join(' / ') }}<b>{{ event.file.filename }}</b></p><small>{{ event.new_path || event.old_path }}</small></div></article><AppPagination :page="events.page" :pages="events.pages" :total="events.total" @change="loadEvents" /></section>
        </template>

        <template v-else-if="campaignTab === 'reports'">
          <section class="panel reports-grid"><a class="report-card primary-report" :href="reportHref('xlsx')"><span class="report-icon">X</span><div><h3>Полный отчёт Excel</h3><p>Сводка, выполнение, не сдали, файлы, активность и ошибки в одной книге.</p></div><strong>Скачать XLSX →</strong></a><a class="report-card" :href="reportHref('csv','progress')"><span class="report-icon">;</span><div><h3>Ход сдачи</h3><p>Текущие итоги по организациям.</p></div><strong>Скачать CSV →</strong></a><a class="report-card" :href="reportHref('csv','files')"><span class="report-icon">;</span><div><h3>Реестр файлов</h3><p>Все зарегистрированные файлы.</p></div><strong>Скачать CSV →</strong></a><a class="report-card" :href="reportHref('csv','events')"><span class="report-icon">;</span><div><h3>Журнал активности</h3><p>История загрузок и изменений.</p></div><strong>Скачать CSV →</strong></a></section>
        </template>

        <template v-else-if="campaignTab === 'settings'">
          <section class="panel form-panel"><h2>Настройки кампании</h2><div class="form-grid"><label>Название<input v-model="campaignSettings.name" /></label><label>Состояние<select v-model="campaignSettings.status"><option value="draft">Черновик</option><option value="active">Активна</option><option value="completed">Завершена</option><option value="archived">Архив</option></select></label><label class="wide">Описание<textarea v-model="campaignSettings.description" rows="3"></textarea></label><label>Срок сдачи<input v-model="campaignSettings.deadline_at" type="datetime-local" /></label><label>Корневая папка<input :value="selectedCampaign.root_path" disabled /></label></div><div class="form-actions"><button class="primary" @click="saveCampaignSettings">Сохранить</button><button @click="cloneCampaign">Создать новую на основе этой</button><button @click="openImport('dimensions')">Импортировать справочники</button><button @click="openImport('requirements')">Импортировать требования</button><button @click="reclassifyFiles">Пересчитать структуру файлов</button></div></section>
          <section class="panel"><h2>Структура папок</h2><div class="structure-preview"><div><span>1</span><strong>Организация</strong><small>обязательно · имя папки</small></div><div v-for="dimension in dimensions" :key="dimension.id"><span>{{ dimension.position + 1 }}</span><strong>{{ dimension.name }}</strong><small>{{ dimension.required ? 'обязательно' : 'необязательно' }} · {{ dimension.fixed ? 'справочник' : 'произвольно' }}</small></div></div><p class="muted">Для изменения структуры активной кампании API потребует явное подтверждение и повторно классифицирует файлы.</p></section>
        </template>
      </template>
    </main>

    <AppModal v-if="showOrganizationModal" :title="editingOrganization ? 'Изменить организацию' : 'Добавить организацию'" @close="showOrganizationModal = false">
      <div class="form-grid"><label>Код<input v-model="orgForm.code" /></label><label>Имя папки<input v-model="orgForm.folder_name" /></label><label class="wide">Полное название<input v-model="orgForm.name" /></label><label>Краткое название<input v-model="orgForm.short_name" /></label><label>Email<input v-model="orgForm.email" type="email" /></label><label class="check"><input v-model="orgForm.active" type="checkbox" /> Активна</label></div>
      <template #footer><button @click="showOrganizationModal = false">Отмена</button><button class="primary" @click="saveOrganization">Сохранить</button></template>
    </AppModal>

    <AppModal v-if="showImportModal" title="Импорт данных" wide @close="showImportModal = false">
      <div class="import-help"><strong>{{ importKind === 'organizations' ? 'Организации' : (importKind === 'requirements' ? 'План требований' : 'Справочники структуры') }}</strong><p>Выберите XLSX или CSV. Сначала приложение покажет результат проверки; данные не изменятся до подтверждения.</p></div>
      <label class="file-drop"><input type="file" accept=".xlsx,.csv" @change="importFile = $event.target.files[0]; importPreview = null" /><span>⇧</span><strong>{{ importFile?.name || 'Выберите файл XLSX или CSV' }}</strong></label>
      <div v-if="importPreview" class="import-preview"><div class="metrics-grid"><MetricCard label="Строк" :value="importPreview.rows" /><MetricCard label="Будет добавлено" :value="importPreview.to_add" tone="green" /><MetricCard label="Будет обновлено" :value="importPreview.to_update" tone="purple" /><MetricCard label="Ошибок" :value="importPreview.errors.length" tone="orange" /></div><div v-if="importPreview.errors.length" class="error-list"><h3>Ошибочные строки</h3><p v-for="row in importPreview.errors.slice(0, 20)" :key="row.row"><strong>Строка {{ row.row }}:</strong> {{ row.message }}</p></div></div>
      <template #footer><button @click="showImportModal = false">Отмена</button><button :disabled="!importFile" @click="previewImport(false)">Проверить</button><button class="primary" :disabled="!importPreview || importPreview.errors.length > 0" @click="previewImport(true)">Импортировать</button></template>
    </AppModal>

    <AppModal v-if="showCampaignWizard" title="Новая кампания" wide @close="showCampaignWizard = false">
      <div class="wizard-steps"><span v-for="number in 6" :key="number" :class="{ active: wizard.step === number, done: wizard.step > number }">{{ wizard.step > number ? '✓' : number }}</span></div>
      <div v-if="wizard.step === 1" class="wizard-page"><h3>Основное</h3><div class="form-grid"><label class="wide">Название<input v-model="wizard.name" placeholder="Например, Олимпиада 2027" /></label><label>Год<input v-model.number="wizard.year" type="number" min="2000" max="2200" /></label><label>Тип<input v-model="wizard.campaign_type" /></label><label>Дата начала<input v-model="wizard.start_at" type="datetime-local" /></label><label>Срок сдачи<input v-model="wizard.deadline_at" type="datetime-local" /></label><label class="wide">Описание<textarea v-model="wizard.description" rows="3"></textarea></label></div></div>
      <div v-else-if="wizard.step === 2" class="wizard-page"><h3>Корневая папка Nextcloud</h3><div class="form-grid"><label>Владелец папки<input v-model="wizard.storage_user_id" placeholder="По умолчанию — текущий пользователь" /></label><label>Путь<input v-model="wizard.root_path" placeholder="/Олимпиада 2027" /></label></div><button @click="browseFolders('')">Выбрать папку</button><div v-if="folders.length" class="folder-browser"><div class="folder-current">{{ folderPath || '/' }}</div><button v-for="folder in folders" :key="folder.path" @click="selectFolder(folder.path)">▰ {{ folder.name }}</button></div><p class="hint">Внутри этой папки первый уровень должен соответствовать именам папок организаций.</p></div>
      <div v-else-if="wizard.step === 3" class="wizard-page"><div class="wizard-title"><div><h3>Организации</h3><p>Выбрано: {{ wizard.organization_ids.length }}</p></div><div><button @click="wizard.organization_ids = organizations.items.map(item => item.id)">Выбрать все</button><button @click="wizard.organization_ids = []">Снять все</button></div></div><div class="organization-picker"><label v-for="org in organizations.items" :key="org.id"><input v-model="wizard.organization_ids" type="checkbox" :value="org.id" /><span><strong>{{ org.short_name || org.name }}</strong><small>Папка {{ org.folder_name }}</small></span></label></div></div>
      <div v-else-if="wizard.step === 4" class="wizard-page"><div class="wizard-title"><div><h3>Структура</h3><p>Первый уровень «Организация» добавляется автоматически.</p></div><select v-model="wizard.structure_mode"><option value="strict">Строгая структура</option><option value="free">Свободная структура</option></select></div><div v-if="wizard.structure_mode === 'strict'" class="dimension-editor"><div class="dimension-row locked"><span>1</span><strong>Организация</strong><small>обязательно</small></div><div v-for="(dimension, index) in wizard.dimensions" :key="index" class="dimension-row"><span>{{ index + 2 }}</span><div><input v-model="dimension.name" placeholder="Название уровня" /><input v-model="dimension.key" placeholder="Ключ" /><label class="check"><input v-model="dimension.required" type="checkbox" /> Обязательно</label><textarea v-model="dimension.valuesText" rows="4" placeholder="Допустимые значения, по одному на строку"></textarea></div><button class="icon-button" @click="wizard.dimensions.splice(index, 1)">×</button></div><button @click="addDimension">+ Добавить уровень</button></div><div v-else class="info-box">Будет сохранён произвольный путь после папки организации. Справочники уровней не применяются.</div></div>
      <div v-else-if="wizard.step === 5" class="wizard-page"><h3>Требования к сдаче</h3><label class="choice-card"><input v-model="wizard.requirements_mode" type="radio" value="simple" /><span><strong>Простой режим</strong><small>Организация считается сдавшей, если в её папке есть хотя бы один файл.</small></span></label><label class="choice-card"><input v-model="wizard.requirements_mode" type="radio" value="matrix" /><span><strong>Матрица требований</strong><small>Учитываются только предметы, параллели и другие комбинации, в которых организация участвует.</small></span></label><label v-if="wizard.requirements_mode === 'matrix'" class="file-drop small"><input type="file" accept=".xlsx,.csv" @change="wizard.requirement_file = $event.target.files[0]" /><span>⇧</span><strong>{{ wizard.requirement_file?.name || 'Выберите план XLSX/CSV' }}</strong></label></div>
      <div v-else class="wizard-page"><h3>Проверка</h3><dl class="review-list"><div><dt>Название</dt><dd>{{ wizard.name }}</dd></div><div><dt>Период</dt><dd>{{ wizard.year }} · до {{ fmtDate(wizard.deadline_at) }}</dd></div><div><dt>Папка</dt><dd>{{ wizard.root_path }}</dd></div><div><dt>Организаций</dt><dd>{{ wizard.organization_ids.length }}</dd></div><div><dt>Структура</dt><dd>{{ wizard.structure_mode === 'strict' ? wizard.dimensions.map(item => item.name).join(' → ') : 'Свободная' }}</dd></div><div><dt>Требования</dt><dd>{{ wizard.requirements_mode === 'simple' ? 'Любой файл' : 'Матрица требований' }}</dd></div></dl><label class="choice-card"><input v-model="wizard.status" type="checkbox" true-value="active" false-value="draft" /><span><strong>Сразу запустить кампанию</strong><small>После создания начнётся обработка файловых событий.</small></span></label></div>
      <template #footer><button v-if="wizard.step > 1" @click="wizard.step--">Назад</button><span class="footer-spacer"></span><button @click="showCampaignWizard = false">Отмена</button><button v-if="wizard.step < 6" class="primary" :disabled="(wizard.step === 1 && !wizard.name) || (wizard.step === 2 && !wizard.root_path)" @click="wizard.step++">Далее</button><button v-else class="primary" @click="createCampaign">Создать кампанию</button></template>
    </AppModal>
  </div>
</template>
