import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    cssCodeSplit: false,
    lib: {
      entry: 'src/main.js',
      name: 'FileControl',
      formats: ['iife'],
      fileName: () => 'app.js',
    },
    rollupOptions: {
      output: {
        assetFileNames: (assetInfo) => assetInfo.name?.endsWith('.css') ? 'app.css' : '[name][extname]',
      },
    },
  },
})

