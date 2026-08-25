import { createApp } from 'vue'
import App from './App.vue'
import './styles.css'

const mount = () => {
  const target = document.getElementById('content')
  if (!target || target.dataset.fileControlMounted) return
  target.dataset.fileControlMounted = 'true'
  createApp(App).mount(target)
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mount, { once: true })
} else {
  mount()
}

