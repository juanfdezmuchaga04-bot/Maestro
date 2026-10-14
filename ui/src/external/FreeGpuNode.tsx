import { useEffect, useState } from 'react'
import { CheckCircle2, Cpu, Download, ExternalLink, Loader2, RefreshCw } from 'lucide-react'

interface Platform {
  id: string
  label: string
  free: boolean
  gpu: string
  quota: string
  account: string
  terms: string
  source: string
  confidence: string
  recommended: boolean
}

interface Peer {
  peer_id: string
  label: string
  base_url: string
  online: boolean
  capabilities: string[]
  notes: string
  last_probe: { at?: number; error?: string } | null
}

interface Status {
  platforms: Platform[]
  peer: Peer | null
  peers?: Peer[]
  notebook: string
  script: string
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail || res.status))
  }
  return res.json() as Promise<T>
}

export function FreeGpuNode() {
  const [status, setStatus] = useState<Status | null>(null)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let alive = true
    call<Status>('/api/v1/engines/free-gpu')
      .then(data => alive && setStatus(data))
      .catch(error => alive && setMessage({ ok: false, text: error instanceof Error ? error.message : String(error) }))
    return () => {
      alive = false
    }
  }, [reload])

  const connect = async () => {
    setBusy(true)
    setMessage(null)
    try {
      const result = await call<{ nodes: Array<{ online: boolean; peer: Peer }> }>('/api/v1/engines/free-gpu/connect', {
        method: 'POST',
        body: JSON.stringify({ code: code.trim() }),
      })
      const offline = result.nodes.filter(node => !node.online)
      setMessage(offline.length === 0
        ? { ok: true, text: `Connected to ${result.nodes.map(node => node.peer.label).join(' and ')}. Renders now go to ${result.nodes.length > 1 ? 'these GPUs (the least busy one first)' : 'this GPU'}.` }
        : { ok: false, text: `Saved, but ${offline.map(node => node.peer.label).join(', ')} did not answer yet: ${offline[0].peer.last_probe?.error || 'still starting?'}` })
      setCode('')
      setReload(value => value + 1)
    } catch (error) {
      setMessage({ ok: false, text: error instanceof Error ? error.message : String(error) })
    } finally {
      setBusy(false)
    }
  }

  const peers = status?.peers || (status?.peer ? [status.peer] : [])

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4 text-xs text-text-secondary">
      <section className="rounded-xl border border-border bg-bg-secondary p-4">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-text-primary"><Cpu size={15} /> Free NVIDIA GPU node</h2>
        <p className="mt-1 text-[11px] text-text-muted">
          Runs <strong>your own Maestro</strong> on the free NVIDIA GPU of a Kaggle or Lightning AI notebook and connects it to
          this computer. Renders sent there use Maestro&apos;s real engine — the same results as an NVIDIA PC, at that GPU&apos;s
          speed and within the platform&apos;s free quota. Nothing is paid. The node is protected by a random token and reached
          through an HTTPS TryCloudflare tunnel that lives only while the notebook runs.
        </p>
        <ol className="mt-3 list-decimal space-y-1 pl-5">
          <li>
            Download the notebook{' '}
            <a href="/api/v1/engines/free-gpu/notebook" className="inline-flex items-center gap-1 text-accent-warm hover:underline">
              <Download size={11} /> maestro_free_gpu_node.ipynb
            </a>{' '}
            and import it in Kaggle (<em>Create → Notebook → File → Import notebook</em>).
          </li>
          <li>In Kaggle: <em>Settings → Accelerator → GPU T4 ×2</em> and <em>Internet → On</em> (phone-verified account).</li>
          <li><em>Run all</em>. The first run installs PyTorch and Maestro (several minutes); the last cell prints a <code>maestro-node:…</code> code. With <em>T4 ×2</em> it starts one Maestro per GPU and the single code connects both; renders and the SeedVR2 4K pass go to the least busy one. Both share the session&apos;s ~29 GB of RAM.</li>
          <li>Paste the code below and press Connect. Keep the notebook open while you render; stop it when you finish.</li>
        </ol>
      </section>

      <section className="rounded-xl border border-border bg-bg-secondary p-4">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-semibold text-text-primary">Connection</h3>
          <button type="button" onClick={() => setReload(value => value + 1)} className="ml-auto rounded p-1 text-text-muted hover:bg-bg-hover" title="Refresh">
            <RefreshCw size={12} />
          </button>
        </div>
        {peers.length === 0 && <p className="mt-2 text-text-muted">No free GPU node connected yet.</p>}
        {peers.map(peer => (
          <div key={peer.peer_id} className="mt-2">
            <p className={`flex items-center gap-1 ${peer.online ? 'text-accent-green' : 'text-amber-400'}`}>
              {peer.online && <CheckCircle2 size={12} />}
              {peer.label} — {peer.online ? 'online' : 'offline (start the notebook again and paste its new code)'} · {peer.notes}
            </p>
            {peer.online && peer.capabilities.length > 0 && (
              <p className="mt-0.5 text-[11px] text-text-muted">Can render: {peer.capabilities.join(', ')}</p>
            )}
          </div>
        ))}
        <textarea
          value={code}
          onChange={event => setCode(event.target.value)}
          rows={3}
          spellCheck={false}
          placeholder="maestro-node:…"
          className="mt-3 w-full rounded-lg border border-border bg-bg-tertiary px-3 py-2 font-mono text-[11px] text-text-primary focus:border-accent-warm focus:outline-none"
        />
        <button
          type="button"
          onClick={() => void connect()}
          disabled={busy || !code.trim().startsWith('maestro-node:')}
          className="mt-2 inline-flex items-center gap-1 rounded-lg bg-accent-warm px-3 py-1.5 font-semibold text-black disabled:opacity-40"
        >
          {busy && <Loader2 size={12} className="animate-spin" />} Connect
        </button>
        {message && <p className={`mt-2 ${message.ok ? 'text-accent-green' : 'text-red-400'}`}>{message.text}</p>}
      </section>

      <section className="rounded-xl border border-border bg-bg-secondary p-4">
        <h3 className="mb-2 text-xs font-semibold text-text-primary">Free NVIDIA platforms</h3>
        <ul className="grid gap-2">
          {(status?.platforms || []).map(platform => (
            <li key={platform.id} className="rounded-lg border border-border bg-bg-tertiary p-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-text-primary">{platform.label}</span>
                <span className={platform.recommended ? 'text-accent-green' : 'text-red-400'}>
                  {platform.recommended ? 'usable' : 'not usable for this'}
                </span>
                <span className="text-[10px] text-text-muted">{platform.confidence}</span>
                <a href={platform.source} target="_blank" rel="noreferrer" className="ml-auto text-text-muted hover:text-text-primary"><ExternalLink size={11} /></a>
              </div>
              <div className="mt-0.5">{platform.gpu} · {platform.quota}</div>
              <div className="text-[11px] text-text-muted">{platform.account}. {platform.terms}</div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
