import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { installSignInRedirect } from './lib/session'

// A 401 means the backend's optional sign-in gate wants a password again; the
// next navigation shows its sign-in page. Installed before React mounts so no
// early request can slip past it.
installSignInRedirect()

// Mobile browsers require persistent notifications from a service worker;
// `new Notification()` is desktop-only on several engines, including iOS
// WebKit. This worker deliberately does not cache application assets, so a
// Maestro update can never strand users on an old UI bundle.
if ('serviceWorker' in navigator) {
  void navigator.serviceWorker.register('/maestro-sw.js', { scope: '/' }).catch(error => {
    console.warn('[Notifications] Service worker registration failed:', error)
  })
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
