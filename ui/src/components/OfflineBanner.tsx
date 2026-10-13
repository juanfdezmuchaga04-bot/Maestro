import { useEffect, useRef, useState } from 'react'
import { CloudOff, RefreshCw } from 'lucide-react'

/**
 * OfflineBanner — tells the user when the browser cannot reach their computer,
 * and refreshes live state the moment it can again.
 *
 * This is the failure mode a phone actually hits: the app is installed on the
 * Home Screen, the computer sleeps or Wi-Fi drops, and every panel would
 * otherwise look empty or stale with no explanation. The banner says what
 * happened, and `onReconnect` re-runs the same loaders the app uses at startup
 * so the queue, outputs and system state are current again without a manual
 * reload.
 */
export function OfflineBanner({ onReconnect }: { onReconnect: () => void }) {
  const [offline, setOffline] = useState(() => !navigator.onLine)
  const [reconnected, setReconnected] = useState(false)
  const wasOffline = useRef(!navigator.onLine)

  useEffect(() => {
    const goOffline = () => {
      wasOffline.current = true
      setReconnected(false)
      setOffline(true)
    }
    const goOnline = () => {
      setOffline(false)
      if (!wasOffline.current) return
      wasOffline.current = false
      setReconnected(true)
      onReconnect()
    }
    window.addEventListener('offline', goOffline)
    window.addEventListener('online', goOnline)
    return () => {
      window.removeEventListener('offline', goOffline)
      window.removeEventListener('online', goOnline)
    }
  }, [onReconnect])

  useEffect(() => {
    if (!reconnected) return
    const timer = setTimeout(() => setReconnected(false), 4000)
    return () => clearTimeout(timer)
  }, [reconnected])

  if (!offline && !reconnected) return null

  return (
    <div
      role="status"
      aria-live="polite"
      className={`fixed bottom-3 inset-x-3 z-50 md:inset-x-auto md:left-3 md:w-80 flex items-start gap-2.5 rounded-xl border px-3 py-2.5 backdrop-blur-sm shadow-lg ${
        offline
          ? 'bg-amber-500/20 border-amber-500/40'
          : 'bg-emerald-500/15 border-emerald-500/40'
      }`}
      style={{ paddingBottom: 'max(0.625rem, env(safe-area-inset-bottom))' }}
    >
      {offline ? (
        <CloudOff size={15} className="shrink-0 mt-0.5 text-indicator-warning" />
      ) : (
        <RefreshCw size={15} className="shrink-0 mt-0.5 text-accent-green" />
      )}
      <div className="flex-1 min-w-0 space-y-0.5">
        <p className="text-[11px] font-medium text-text-primary">
          {offline ? 'No connection to your computer' : 'Reconnected'}
        </p>
        <p className="text-[10px] leading-snug text-text-secondary">
          {offline
            ? 'Maestro runs on your own machine. Queued jobs and files are safe there; this returns by itself when the network does.'
            : 'Live state refreshed from your computer.'}
        </p>
      </div>
    </div>
  )
}
