import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'

const script = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
  .split('<script setup>')[1].split('</script>')[0].replace(/^import .*\n/gm, '')

function app(api) {
  return runInNewContext(script + '\n;({ section, filesPage, loadFiles, refreshSection })', {
    api,
    query: (params) => new URLSearchParams(params).toString(),
    localStorage: { getItem: () => '1' },
    ref: (value) => ({ value }),
    reactive: (value) => value,
    computed: (get) => ({ get value() { return get() } }),
    watch: () => {},
    onMounted: () => {},
    onBeforeUnmount: () => {},
  })
}

function pageNumber(url) {
  return Number(new URL(url, 'https://example.invalid').searchParams.get('page'))
}

test('refresh keeps the selected file page', async () => {
  const ui = app(async (url) => ({ items: [], page: pageNumber(url), pages: 4, total: 200 }))
  ui.section.value = 'files'
  await ui.loadFiles(3)
  await ui.refreshSection()
  assert.equal(ui.filesPage.value.page, 3)
})

test('an older refresh cannot overwrite a more recent page selection', async () => {
  let resolveRefresh
  let pending = false
  const ui = app(async (url) => {
    const page = pageNumber(url)
    if (page && pending) {
      pending = false
      await new Promise((resolve) => { resolveRefresh = resolve })
    }
    return { items: [], page, pages: 4, total: 200 }
  })
  ui.section.value = 'files'
  await ui.loadFiles(2)
  pending = true
  const refresh = ui.refreshSection()
  await ui.loadFiles(3)
  resolveRefresh()
  await refresh
  assert.equal(ui.filesPage.value.page, 3)
})
