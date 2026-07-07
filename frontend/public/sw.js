// __BUILD_HASH__ is stamped with a unique per-build value by vite.config.js so
// every `make build` produces a new cache name. The activate handler then
// purges every cache that isn't the current one, dropping the stale shell.
const CACHE = 'finance-shell-__BUILD_HASH__'
const SHELL = ['/', '/static/manifest.json', '/static/icon.svg']

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)))
  self.skipWaiting()
})

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  )
})

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return

  const { pathname } = new URL(e.request.url)

  // API routes — always network, never cache
  if (pathname === '/agents' || pathname === '/chat' || pathname.startsWith('/auth') || pathname === '/history' || pathname.startsWith('/consent') || pathname.startsWith('/tip') || pathname.startsWith('/budgets') || pathname === '/goals' || pathname.startsWith('/audit') || pathname.startsWith('/providers') || pathname.startsWith('/sync')) {
    return
  }

  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached
      return fetch(e.request).then(res => {
        if (res.ok) {
          const clone = res.clone()
          caches.open(CACHE).then(c => c.put(e.request, clone))
        }
        return res
      })
    })
  )
})
