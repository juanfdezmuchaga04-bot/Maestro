/* Maestro service worker.
 *
 * Two jobs, and deliberately nothing else:
 *
 *   1. Persistent notifications. Mobile browsers require them from a worker
 *      (`new Notification()` is desktop-only on several engines, including iOS
 *      WebKit).
 *   2. A fetch handler with an offline fallback. Chrome/Android only offers
 *      "Install app" for a manifest *plus* a worker that handles fetches, and
 *      an installed app that is opened with the computer unreachable should
 *      explain itself instead of showing a dead tab.
 *
 * Application bundles are still NOT served from cache. Every navigation and
 * asset request goes to the network first, so a Maestro update can never
 * strand a phone on an old UI; the cache holds exactly one document
 * (`/offline.html` plus its icon) and is only read after the network failed.
 * API responses are never cached at all — they are live data from your own
 * computer, and a stale job list would be a lie.
 */
const OFFLINE_CACHE = 'maestro-offline-v1'
const OFFLINE_URL = '/offline.html'
const OFFLINE_ASSETS = [OFFLINE_URL, '/maestro-icon-192.png']

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    try {
      const cache = await caches.open(OFFLINE_CACHE)
      await cache.addAll(OFFLINE_ASSETS)
    } catch {
      // A failed pre-cache (offline install, blocked storage) must not stop the
      // worker from activating: notifications keep working either way.
    }
    await self.skipWaiting()
  })())
})

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    // Drop caches from older versions so a renamed asset cannot linger.
    const names = await caches.keys()
    await Promise.all(
      names.filter(name => name !== OFFLINE_CACHE).map(name => caches.delete(name)),
    )
    await self.clients.claim()
  })())
})

self.addEventListener('fetch', event => {
  const request = event.request
  if (request.method !== 'GET') return
  // Navigations only. Sub-resources (bundles, media, posters) are left to the
  // browser and the server's own caching headers.
  if (request.mode !== 'navigate') return

  let url
  try {
    url = new URL(request.url)
  } catch {
    return
  }
  if (url.origin !== self.location.origin) return
  // The offline document itself, and anything the backend owns, is not
  // intercepted: a 401 from the access gate must reach the browser so it can
  // show the sign-in page.
  if (url.pathname === OFFLINE_URL || url.pathname.startsWith('/api/')) return

  event.respondWith((async () => {
    try {
      return await fetch(request)
    } catch {
      const cache = await caches.open(OFFLINE_CACHE)
      const cached = await cache.match(OFFLINE_URL)
      if (cached) return cached
      return new Response(
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">' +
        '<body style="background:#0a0a0a;color:#e8e0d0;font-family:system-ui;padding:24px">' +
        '<h1 style="font-size:18px">Maestro is not reachable</h1>' +
        '<p style="color:#b8b0a0;line-height:1.6">Check that the computer running Maestro is on, ' +
        'then reload.</p></body>',
        { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } },
      )
    }
  })())
})

self.addEventListener('push', event => {
  let payload = {}
  try {
    payload = event.data ? event.data.json() : {}
  } catch {
    payload = { body: event.data ? event.data.text() : '' }
  }
  const title = payload.title || 'Maestro'
  event.waitUntil((async () => {
    if (payload.onlyWhenHidden) {
      const windows = await self.clients.matchAll({
        type: 'window',
        includeUncontrolled: true,
      })
      if (windows.some(client => client.visibilityState === 'visible')) return
    }
    await self.registration.showNotification(title, {
      body: payload.body || 'Your Maestro generation has finished.',
      icon: '/maestro-icon-192.png',
      badge: '/maestro-icon-192.png',
      tag: payload.tag || 'maestro-generation',
      data: { url: payload.url || '/' },
    })
  })())
})

self.addEventListener('notificationclick', event => {
  event.notification.close()
  const targetUrl = event.notification.data?.url || '/'
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    })
    for (const client of windows) {
      if ('focus' in client) {
        if ('navigate' in client) await client.navigate(targetUrl)
        return client.focus()
      }
    }
    if (self.clients.openWindow) return self.clients.openWindow(targetUrl)
    return undefined
  })())
})
