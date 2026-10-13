/**
 * Session handling for a Maestro reachable beyond loopback.
 *
 * When the optional sign-in gate is enabled on the backend, a request made with
 * an expired or missing session answers 401 and the server's next *navigation*
 * is the sign-in page. Rather than teaching every one of the app's fetch call
 * sites about that, this wraps `window.fetch` once at startup: a 401 triggers a
 * single reload, which lands on the sign-in form, and the form posts the
 * password and returns here.
 *
 * Two guards keep it from being annoying:
 *   - the sign-in endpoints themselves are ignored, so a wrong password shows
 *     the server's message instead of reloading the page under the user;
 *   - a reload is only attempted once per minute, so a genuine 401 loop cannot
 *     turn into an infinite reload.
 */

const RELOAD_GUARD_KEY = 'maestro-signin-reload-at'
const RELOAD_GUARD_WINDOW_MS = 60_000

function shouldIgnore(path: string): boolean {
  return path.includes('/api/v1/access/')
}

function reloadOnce(): void {
  let last = 0
  try {
    last = Number(sessionStorage.getItem(RELOAD_GUARD_KEY) || 0)
  } catch {
    last = 0
  }
  const now = Date.now()
  if (Number.isFinite(last) && now - last < RELOAD_GUARD_WINDOW_MS) return
  try {
    sessionStorage.setItem(RELOAD_GUARD_KEY, String(now))
  } catch {
    /* private mode: fall through and try the reload anyway */
  }
  window.location.reload()
}

export function installSignInRedirect(): void {
  if (typeof window === 'undefined' || typeof window.fetch !== 'function') return
  const original = window.fetch.bind(window)
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const response = await original(input, init)
    if (response.status === 401) {
      const target =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.pathname
            : input.url
      if (!shouldIgnore(target)) reloadOnce()
    }
    return response
  }
}
