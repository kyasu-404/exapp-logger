const webroot = window.OC?.webroot || ''
export const API_BASE = `${webroot}/index.php/apps/app_api/proxy/file_control`

export async function api(path, options = {}) {
  const init = {
    credentials: 'same-origin',
    ...options,
    headers: { Accept: 'application/json', ...(options.headers || {}) },
  }
  if (init.body && !(init.body instanceof FormData) && typeof init.body !== 'string') {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(init.body)
  }
  const response = await fetch(`${API_BASE}${path}`, init)
  if (!response.ok) {
    let message = `Ошибка ${response.status}`
    try {
      const payload = await response.json()
      message = payload.detail || message
    } catch {}
    throw new Error(message)
  }
  if (response.status === 204) return null
  return response.json()
}

export function downloadUrl(path) {
  return `${API_BASE}${path}`
}

export function query(params) {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, value)
  })
  return search.toString()
}

