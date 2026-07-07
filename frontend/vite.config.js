import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import nodePath from 'node:path'
import fs from 'node:fs'

// Unique per-build hash for the service-worker cache name. A timestamp
// guarantees every `make build` yields a new cache name, so installed PWAs drop
// the stale shell on their next visit. Exposed as VITE_BUILD_HASH for any
// build-time consumer.
const BUILD_HASH = String(Date.now())
process.env.VITE_BUILD_HASH = BUILD_HASH

// Files in public/ are copied verbatim and never transformed, so import.meta.env
// substitution can't reach public/sw.js. Stamp the build hash into the emitted
// sw.js after the bundle (and the public/ copy) is written.
function stampServiceWorker() {
  let outDir
  return {
    name: 'stamp-service-worker',
    apply: 'build',
    configResolved(config) {
      outDir = nodePath.resolve(config.root, config.build.outDir)
    },
    closeBundle() {
      const swPath = nodePath.join(outDir, 'sw.js')
      try {
        const src = fs.readFileSync(swPath, 'utf8')
        fs.writeFileSync(swPath, src.replaceAll('__BUILD_HASH__', BUILD_HASH))
      } catch (err) {
        this.warn(`stamp-service-worker: could not stamp ${swPath}: ${err.message}`)
      }
    },
  }
}

export default defineConfig({
  plugins: [vue(), stampServiceWorker()],
  server: {
    proxy: {
      '/api': { target: 'http://localhost:8080', rewrite: path => path.replace(/^\/api/, '') },
      '/auth': 'http://localhost:8080',
      '/agents': 'http://localhost:8080',
      '/chat': 'http://localhost:8080',
      '/history': 'http://localhost:8080',
      '/consent': 'http://localhost:8080',
      '/tip': 'http://localhost:8080',
      '/budgets': 'http://localhost:8080',
      '/goals': 'http://localhost:8080',
      '/static': 'http://localhost:8080',
    }
  },
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
  }
})
