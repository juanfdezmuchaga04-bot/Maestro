import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  Cloud,
  Copy,
  Cpu,
  Keyboard,
  Loader2,
  Monitor,
  Plug,
  RefreshCw,
  Server,
  Smartphone,
  Trash2,
  Users,
} from 'lucide-react'
import {
  addPublicSpace,
  addRemotePeer,
  cancelComfyRun,
  connectPublicCloud,
  fetchComfyRun,
  fetchComfyTemplates,
  fetchComputeAssignments,
  fetchEngines,
  fetchPublicProviders,
  fetchRemotePeers,
  forgetPublicProvider,
  probeComfyUi,
  probePublicProvider,
  probeRemotePeer,
  recoverComputeNode,
  registerComputeNode,
  removeComputeNode,
  removeRemotePeer,
  runComfyTemplate,
} from '../api/engines'
import type { ComfyRunState } from '../api/engines'
import type {
  ComputeAssignments,
  ComputeNodeInfo,
  ComfyProbeResult,
  ComfyTemplateDetail,
  EnginesStatus,
  PublicProvidersResponse,
  RemotePeerInfo,
} from '../types/engines'

/**
 * A clock read on an interval, never during render: reading the wall clock while
 * rendering is impure and makes the output unstable across re-renders.
 */
function useClock(intervalMs = 30000): number {
  const [now, setNow] = useState(0)
  useEffect(() => {
    const update = () => setNow(Date.now())
    update()
    const timer = window.setInterval(update, intervalMs)
    return () => window.clearInterval(timer)
  }, [intervalMs])
  return now
}

function RelativeTime({ value }: { value: number | null }) {
  const now = useClock()
  if (!value) return <span className="text-text-muted">never seen</span>
  if (!now) return <span className="text-text-muted">—</span>
  const seconds = Math.max(0, Math.round(now / 1000 - value))
  if (seconds < 60) return <span>{seconds}s ago</span>
  if (seconds < 3600) return <span>{Math.round(seconds / 60)}m ago</span>
  return <span>{Math.round(seconds / 3600)}h ago</span>
}

function CapabilityChips({ items }: { items: string[] }) {
  if (items.length === 0) {
    return <span className="text-[11px] text-text-muted">no capability declared</span>
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map(item => (
        <span
          key={item}
          className="rounded-md border border-border bg-bg-tertiary px-1.5 py-0.5 text-[10px] text-text-secondary"
        >
          {item.replace(/_/g, ' ')}
        </span>
      ))}
    </div>
  )
}

function CapabilityTable({ status }: { status: EnginesStatus }) {
  const localEvidence = status.local_engine?.evidence || {}
  return (
    <section className="rounded-xl border border-border bg-bg-secondary p-4">
      <div className="flex items-center gap-2">
        <Cpu size={15} className="text-accent-warm" />
        <h2 className="text-sm font-semibold text-text-primary">What this setup can do</h2>
      </div>
      <p className="mt-1 text-xs text-text-muted">
        Each capability is resolved to whichever engine can run it. A capability is only listed as
        available when a provider that really declares it is reachable now.
      </p>
      <ul className="mt-3 divide-y divide-border/60">
        {status.availability.map(entry => (
          <li key={entry.capability} className="flex flex-wrap items-start gap-2 py-2">
            <span
              className={`mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full ${
                entry.available ? 'bg-accent-green text-black' : 'bg-bg-tertiary text-text-muted'
              }`}
            >
              {entry.available ? <CheckCircle2 size={12} /> : <AlertTriangle size={11} />}
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-xs font-medium text-text-primary">{entry.label}</div>
              <div className="text-[11px] text-text-muted">
                {entry.available
                  ? entry.providers.join(', ')
                  : entry.needs_gpu
                    ? 'unavailable — needs an engine that can do it (usually a GPU or a connected computer)'
                    : 'unavailable'}
              </div>
              {(localEvidence[entry.capability] || []).length > 0 && (
                <div className="mt-0.5 text-[10px] text-text-muted">
                  local evidence: {localEvidence[entry.capability].join(', ')}
                </div>
              )}
            </div>
          </li>
        ))}
      </ul>
      <p className="mt-2 text-[10px] text-text-muted">
        {status.local_only
          ? 'Local-only mode: no commercial engine is configured, and none is required.'
          : 'A commercial adapter is registered. It is never used unless explicitly selected.'}
      </p>
    </section>
  )
}

function NodeCard({
  node,
  parked,
  onChanged,
}: {
  node: ComputeNodeInfo
  parked: number
  onChanged: () => void
}) {
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const recover = useCallback(async () => {
    setBusy('recover')
    setError('')
    setMessage('')
    try {
      const result = await recoverComputeNode(node.node_id)
      setMessage(
        result.count > 0
          ? `${result.count} job(s) handed back to the queue`
          : 'Nothing was waiting for this node',
      )
      onChanged()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy('')
    }
  }, [node.node_id, onChanged])

  const forget = useCallback(async () => {
    setBusy('remove')
    setError('')
    setMessage('')
    try {
      const result = await removeComputeNode(node.node_id)
      setMessage(
        result.parked_jobs > 0
          ? `Node removed; ${result.parked_jobs} job(s) kept, waiting for a node`
          : 'Node removed',
      )
      onChanged()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy('')
    }
  }, [node.node_id, onChanged])

  return (
    <li className="rounded-xl border border-border bg-bg-secondary p-4">
      <div className="flex items-start gap-2">
        <span
          className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${
            node.online ? 'bg-accent-green' : 'bg-text-muted'
          }`}
          title={node.online ? 'online' : 'offline'}
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            {node.local ? <Smartphone size={12} /> : <Monitor size={12} />}
            <span className="truncate text-xs font-medium text-text-primary">{node.label}</span>
            <span className="rounded border border-border px-1 py-0.5 text-[10px] text-text-muted">
              {node.kind}
            </span>
          </div>
          <div className="mt-0.5 text-[11px] text-text-muted">
            {node.node_id} · {node.online ? 'online' : 'offline'} · last seen{' '}
            <RelativeTime value={node.last_seen} />
            {node.address ? ` · ${node.address}` : ''}
          </div>
          <div className="mt-2">
            <CapabilityChips items={node.capabilities} />
          </div>
          {node.models.length > 0 && (
            <div className="mt-1.5 text-[10px] text-text-muted">models: {node.models.join(', ')}</div>
          )}
          <div className="mt-1 text-[10px] text-text-muted">
            up to {node.max_concurrent_jobs} concurrent job(s)
            {parked > 0 ? ` · ${parked} job(s) waiting for this node` : ''}
          </div>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => void recover()}
          disabled={busy !== ''}
          className="rounded-lg border border-border px-2.5 py-2 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
        >
          {busy === 'recover' ? 'Checking…' : 'Recover waiting work'}
        </button>
        <button
          type="button"
          onClick={() => void forget()}
          disabled={busy !== ''}
          className="flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-2 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-chip-red disabled:opacity-40"
        >
          <Trash2 size={12} /> Remove
        </button>
      </div>
      {message && <p className="mt-2 text-[11px] text-accent-green">{message}</p>}
      {error && <p className="mt-2 text-[11px] text-chip-red">{error}</p>}
    </li>
  )
}

const CAPABILITY_OPTIONS = [
  'image_generation',
  'text_to_video',
  'image_to_video',
  'video_to_video',
  'video_extension',
  'frame_interpolation',
  'upscaling',
  'inpainting',
  'voice_generation',
  'audio_generation',
  'transcription',
  'auxiliary_processing',
]

function RegisterNodeForm({ onChanged }: { onChanged: () => void }) {
  const [nodeId, setNodeId] = useState('')
  const [label, setLabel] = useState('')
  const [kind, setKind] = useState('compute_node')
  const [address, setAddress] = useState('')
  const [capabilities, setCapabilities] = useState<string[]>(['text_to_video'])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [token, setToken] = useState('')

  const toggle = (capability: string) => {
    setCapabilities(prev =>
      prev.includes(capability) ? prev.filter(item => item !== capability) : [...prev, capability],
    )
  }

  const submit = useCallback(async () => {
    setBusy(true)
    setError('')
    setToken('')
    try {
      const registered = await registerComputeNode({
        nodeId: nodeId.trim(),
        label: label.trim() || undefined,
        kind,
        capabilities,
        address: address.trim() || undefined,
      })
      setToken(registered.token)
      onChanged()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }, [nodeId, label, kind, address, capabilities, onChanged])

  return (
    <section className="rounded-xl border border-border bg-bg-secondary p-4">
      <div className="flex items-center gap-2">
        <Plug size={15} className="text-accent-warm" />
        <h2 className="text-sm font-semibold text-text-primary">Connect a computer</h2>
      </div>
      <p className="mt-1 text-xs text-text-muted">
        Direct this project from the phone and let another computer render. The node announces what it
        can do; work is only sent to a node that declared the capability, and it is parked — never
        lost — while the node is away.
      </p>

      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        <input
          value={nodeId}
          onChange={event => setNodeId(event.target.value)}
          placeholder="node id (e.g. editing-pc)"
          className="rounded-lg border border-border bg-bg-tertiary px-3 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none"
        />
        <input
          value={label}
          onChange={event => setLabel(event.target.value)}
          placeholder="label (optional)"
          className="rounded-lg border border-border bg-bg-tertiary px-3 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none"
        />
        <select
          value={kind}
          onChange={event => setKind(event.target.value)}
          className="rounded-lg border border-border bg-bg-tertiary px-3 py-2.5 text-sm text-text-primary focus:border-accent-warm focus:outline-none"
        >
          <option value="compute_node">Compute node (another Maestro)</option>
          <option value="comfyui">ComfyUI server</option>
        </select>
        <input
          value={address}
          onChange={event => setAddress(event.target.value)}
          placeholder={kind === 'comfyui' ? 'http://192.168.1.20:8188' : 'address (optional)'}
          className="rounded-lg border border-border bg-bg-tertiary px-3 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none"
        />
      </div>

      <div className="mt-3">
        <div className="text-[11px] text-text-muted">Capabilities this node declares</div>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {CAPABILITY_OPTIONS.map(capability => (
            <button
              key={capability}
              type="button"
              onClick={() => toggle(capability)}
              className={`rounded-md border px-2 py-1 text-[10px] transition-colors ${
                capabilities.includes(capability)
                  ? 'border-accent-warm bg-accent-warm/15 text-text-primary'
                  : 'border-border bg-bg-tertiary text-text-muted hover:text-text-secondary'
              }`}
            >
              {capability.replace(/_/g, ' ')}
            </button>
          ))}
        </div>
      </div>

      <button
        type="button"
        onClick={() => void submit()}
        disabled={!nodeId.trim() || capabilities.length === 0 || busy}
        className="mt-3 w-full rounded-lg bg-accent-warm px-3 py-2.5 text-xs font-medium text-black disabled:opacity-40"
      >
        {busy ? 'Registering…' : 'Register node'}
      </button>

      {error && (
        <p className="mt-2 flex items-start gap-2 rounded-lg border border-chip-red/40 bg-chip-red/10 p-2.5 text-[11px] text-text-primary">
          <AlertTriangle size={12} className="mt-0.5 shrink-0 text-chip-red" />
          <span className="flex-1">{error}</span>
        </p>
      )}
      {token && (
        <div className="mt-2 rounded-lg border border-indicator-warning/40 bg-indicator-warning/10 p-3 text-[11px] text-text-primary">
          <div className="flex items-center gap-1.5 font-medium">
            <Keyboard size={12} /> Node token — shown once
          </div>
          <p className="mt-1 break-all font-mono text-[10px] text-text-secondary">{token}</p>
          <p className="mt-1 text-text-muted">
            Store it on the node now. Only its hash is kept, so it cannot be shown again; sending a
            heartbeat without it is rejected.
          </p>
          <button
            type="button"
            onClick={() => void navigator.clipboard?.writeText(token)}
            className="mt-2 flex items-center gap-1.5 rounded-md border border-border px-2 py-1.5 text-[10px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
          >
            <Copy size={11} /> Copy
          </button>
        </div>
      )}
    </section>
  )
}

/**
 * Run one workflow template. The form is built from the template's own manifest,
 * so a template with different inputs gets a different form: nothing is assumed
 * about which fields a workflow takes.
 */
function ComfyRunCard({ baseUrl }: { baseUrl: string }) {
  const [templates, setTemplates] = useState<ComfyTemplateDetail[]>([])
  const [templateId, setTemplateId] = useState('')
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [run, setRun] = useState<ComfyRunState | null>(null)

  useEffect(() => {
    let cancelled = false
    void fetchComfyTemplates()
      .then(response => {
        if (cancelled) return
        setTemplates(response.templates)
        setTemplateId(current => current || response.templates[0]?.template_id || '')
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const declared = useMemo(
    () => templates.find(item => item.template_id === templateId)?.inputs || {},
    [templates, templateId],
  )

  // Poll the server's own view while a prompt is in flight.
  const promptId = run?.prompt_id
  const finished = Boolean(run?.completed)
  useEffect(() => {
    if (!promptId || finished) return
    const timer = window.setInterval(() => {
      void fetchComfyRun(baseUrl, promptId)
        .then(setRun)
        .catch(() => undefined)
    }, 3000)
    return () => window.clearInterval(timer)
  }, [baseUrl, promptId, finished])

  const submit = useCallback(async () => {
    setBusy(true)
    setError('')
    setRun(null)
    const params: Record<string, unknown> = {}
    for (const [name, spec] of Object.entries(declared)) {
      const raw = values[name]
      if (raw === undefined || raw === '') continue // let the manifest default apply
      params[name] = spec.type === 'number' || spec.type === 'integer' ? Number(raw) : raw
    }
    try {
      const started = await runComfyTemplate(baseUrl, templateId, params)
      setRun(await fetchComfyRun(baseUrl, started.prompt_id))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }, [baseUrl, templateId, declared, values])

  const stop = useCallback(async () => {
    if (!run?.prompt_id) return
    try {
      await cancelComfyRun(baseUrl, run.prompt_id)
      setRun(await fetchComfyRun(baseUrl, run.prompt_id))
    } catch (err) {
      setError((err as Error).message)
    }
  }, [baseUrl, run?.prompt_id])

  if (templates.length === 0) {
    return (
      <p className="mt-3 text-[11px] text-text-muted">
        No workflow template is available in the library yet.
      </p>
    )
  }

  const inFlight = Boolean(run && !run.completed)

  return (
    <div className="mt-3 rounded-lg border border-border bg-bg-tertiary p-3">
      <div className="text-[11px] font-medium text-text-primary">Run a template</div>
      <select
        value={templateId}
        onChange={event => {
          setTemplateId(event.target.value)
          setValues({})
        }}
        className="mt-2 w-full rounded-lg border border-border bg-bg-secondary px-2.5 py-2 text-[11px] text-text-primary focus:border-accent-warm focus:outline-none"
      >
        {templates.map(template => (
          <option key={template.template_id} value={template.template_id}>
            {template.label} (v{template.version})
          </option>
        ))}
      </select>

      <div className="mt-2 flex flex-col gap-2">
        {Object.entries(declared).map(([name, spec]) => (
          <label key={name} className="text-[10px] text-text-muted">
            {name.replace(/_/g, ' ')}
            {spec.required ? ' *' : ''}
            <input
              value={values[name] ?? ''}
              onChange={event => setValues(prev => ({ ...prev, [name]: event.target.value }))}
              type={spec.type === 'number' || spec.type === 'integer' ? 'number' : 'text'}
              placeholder={spec.default !== undefined ? String(spec.default) : ''}
              className="mt-0.5 w-full rounded-lg border border-border bg-bg-secondary px-2.5 py-2 text-[11px] text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none"
            />
          </label>
        ))}
      </div>

      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => void submit()}
          disabled={busy || !templateId}
          className="rounded-lg bg-accent-warm px-2.5 py-2 text-[11px] font-medium text-black disabled:opacity-40"
        >
          {busy ? 'Submitting…' : 'Submit to ComfyUI'}
        </button>
        {inFlight && (
          <button
            type="button"
            onClick={() => void stop()}
            className="rounded-lg border border-border px-2.5 py-2 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
          >
            Cancel
          </button>
        )}
      </div>

      {run && (
        <p className="mt-2 text-[10px] text-text-secondary">
          {run.prompt_id} — {run.state}
        </p>
      )}
      {run?.results && run.results.files.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-[10px] text-text-muted">
          {run.results.files.map(file => (
            <li key={`${file.node_id}-${file.filename}`}>
              {file.kind}: {file.filename}
            </li>
          ))}
        </ul>
      )}
      {run && !run.completed && run.state === 'unknown' && (
        <p className="mt-1 text-[10px] text-indicator-warning">
          ComfyUI has no record of this prompt any more — it may have been interrupted or the server
          restarted.
        </p>
      )}
      {error && <p className="mt-2 text-[10px] text-chip-red">{error}</p>}
    </div>
  )
}

function ComfyProbeCard({ onRegistered }: { onRegistered: () => void }) {
  const [baseUrl, setBaseUrl] = useState('http://127.0.0.1:8188')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<ComfyProbeResult | null>(null)

  const probe = useCallback(async () => {
    setBusy(true)
    setError('')
    setResult(null)
    try {
      setResult(await probeComfyUi(baseUrl.trim()))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }, [baseUrl])

  return (
    <section className="rounded-xl border border-border bg-bg-secondary p-4">
      <div className="flex items-center gap-2">
        <Server size={15} className="text-accent-warm" />
        <h2 className="text-sm font-semibold text-text-primary">ComfyUI</h2>
      </div>
      <p className="mt-1 text-xs text-text-muted">
        Its capabilities are read from the server itself: a workflow only counts when every node class
        it uses is present. Nothing is assumed from a workflow's name.
      </p>

      <div className="mt-3 flex gap-2">
        <input
          value={baseUrl}
          onChange={event => setBaseUrl(event.target.value)}
          inputMode="url"
          className="min-w-0 flex-1 rounded-lg border border-border bg-bg-tertiary px-3 py-2.5 text-sm text-text-primary focus:border-accent-warm focus:outline-none"
        />
        <button
          type="button"
          onClick={() => void probe()}
          disabled={busy || !baseUrl.trim()}
          className="rounded-lg bg-accent-warm px-3 py-2.5 text-xs font-medium text-black disabled:opacity-40"
        >
          {busy ? 'Probing…' : 'Probe'}
        </button>
      </div>

      {error && (
        <p className="mt-2 flex items-start gap-2 rounded-lg border border-chip-red/40 bg-chip-red/10 p-2.5 text-[11px] text-text-primary">
          <AlertTriangle size={12} className="mt-0.5 shrink-0 text-chip-red" />
          <span className="flex-1">{error}</span>
        </p>
      )}
      {result && (
        <div className="mt-3 text-[11px] text-text-secondary">
          <div className="font-medium text-text-primary">
            Reachable{result.comfyui_version ? ` · ComfyUI ${result.comfyui_version}` : ''} ·{' '}
            {result.object_info_nodes} node classes
          </div>
          {result.devices.length > 0 && (
            <div className="mt-1">
              devices: {result.devices.map(device => `${device.name} (${device.type})`).join(', ')}
            </div>
          )}
          <div className="mt-1.5">
            <CapabilityChips items={result.capabilities} />
          </div>
          {result.models.length > 0 && (
            <div className="mt-1 text-text-muted">checkpoints: {result.models.join(', ')}</div>
          )}
          {result.templates.length > 0 && (
            <ul className="mt-2 space-y-1">
              {result.templates.map(template => (
                <li key={template.template_id} className="text-text-muted">
                  ✓ {template.label} (v{template.version}) — {template.capabilities.join(', ')}
                </li>
              ))}
            </ul>
          )}
          {result.unsupported_templates.length > 0 && (
            <ul className="mt-2 space-y-1">
              {result.unsupported_templates.map(template => (
                <li key={template.template_id} className="text-indicator-warning">
                  ✗ {template.label} — missing node classes:{' '}
                  {template.missing_node_classes.join(', ')}
                </li>
              ))}
            </ul>
          )}
          <button
            type="button"
            onClick={async () => {
              await registerComputeNode({
                nodeId: `comfyui-${baseUrl.replace(/[^A-Za-z0-9]+/g, '-').slice(-24)}`,
                label: `ComfyUI ${baseUrl}`,
                kind: 'comfyui',
                address: baseUrl.trim(),
                capabilities: result.capabilities,
                models: result.models,
              })
              onRegistered()
            }}
            className="mt-3 rounded-lg border border-border px-2.5 py-2 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
          >
            Register this server so its capabilities are used
          </button>
          {result.capabilities.length > 0 && <ComfyRunCard baseUrl={baseUrl.trim()} />}
        </div>
      )}
    </section>
  )
}

/**
 * What a peer's own report says about its queue worker, in one sentence appended
 * to a probe result. A peer whose worker is stopped still accepts a job into its
 * queue — the render simply waits there, which is the one thing worth saying out
 * loud when a computer is registered for delegation.
 */
function workerNotice(probe: Record<string, unknown> | null | undefined): string {
  if (!probe || probe.queue_worker_running !== false) return ''
  return ' Its queue worker is not running, so a delegated render waits there until you start it.'
}

/**
 * Free public compute over the internet — the engine for a computer that has no
 * GPU and no second machine to borrow. Nothing here needs configuring: Maestro asks
 * the public Hub which Spaces are running on free shared GPU hardware right now,
 * connects to one that declares the task, and re-checks on a timer. A public Space
 * runs its own model, so a render there *approximates* the Maestro recipe, which is
 * why a peer is still preferred when one exists.
 */
function PublicComputeSection({ onChanged }: { onChanged: () => void }) {
  const [data, setData] = useState<PublicProvidersResponse | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState('')
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ space: '', capability: 'text_to_video' })

  const refresh = useCallback(async () => {
    try {
      setData(await fetchPublicProviders())
      setError('')
    } catch (err) {
      setError((err as Error).message)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const connect = useCallback(async () => {
    setBusy('connect')
    setError('')
    setNotice('')
    try {
      const result = await connectPublicCloud({ candidates: 10 })
      setNotice(result.summary)
      await refresh()
      onChanged()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy('')
    }
  }, [refresh, onChanged])

  const probe = useCallback(
    async (providerId: string) => {
      setBusy(providerId)
      setError('')
      setNotice('')
      try {
        const result = await probePublicProvider(providerId)
        setNotice(
          result.provider.online
            ? `${result.provider.space_id} answered: ${result.provider.capabilities.join(', ') || 'no capability reported'}.`
            : `${result.provider.space_id} did not answer. Maestro will look for another running Space.`,
        )
        await refresh()
        onChanged()
      } catch (err) {
        setError((err as Error).message)
      } finally {
        setBusy('')
      }
    },
    [refresh, onChanged],
  )

  const forget = useCallback(
    async (providerId: string) => {
      setBusy(providerId)
      setError('')
      setNotice('')
      try {
        const result = await forgetPublicProvider(providerId)
        setNotice(result.note)
        await refresh()
        onChanged()
      } catch (err) {
        setError((err as Error).message)
      } finally {
        setBusy('')
      }
    },
    [refresh, onChanged],
  )

  const add = useCallback(async () => {
    setBusy('add')
    setError('')
    setNotice('')
    try {
      const result = await addPublicSpace({
        space: form.space.trim(),
        capability: form.capability,
      })
      setNotice(
        result.provider.online
          ? `${result.provider.space_id} can run ${form.capability.replace(/_/g, ' ')}.`
          : `${result.provider.space_id} did not answer with an endpoint for that task, so nothing will be sent to it.`,
      )
      setForm({ space: '', capability: form.capability })
      setOpen(false)
      await refresh()
      onChanged()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy('')
    }
  }, [form, refresh, onChanged])

  const providers = data?.providers || []
  const sources = data?.sources || []
  const empty = (data?.served_capabilities || []).filter(
    capability => !sources.some(source => source.capabilities.includes(capability)),
  )

  return (
    <section className="rounded-xl border border-border bg-bg-secondary p-4">
      <div className="flex items-center gap-2">
        <Cloud size={15} className="text-accent-warm" />
        <h2 className="text-sm font-semibold text-text-primary">Public compute (internet)</h2>
      </div>
      <p className="mt-1 text-xs text-text-muted">
        A free engine you do not have to own, install or sign into. Maestro asks the public Hub
        which Spaces are running on free shared GPU hardware right now, connects to one that
        declares the task, and re-checks it on a timer — so a computer with no GPU and no second
        machine can still render. It is used only after this computer and any connected computer
        have had their turn, and the public Space runs its own model, so the render approximates
        the Maestro recipe rather than replaying it. Free shared GPU time is small; signing
        Hugging Face in with the launcher's optional action uses that free account's larger
        allowance, and no key of yours is ever sent.
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void connect()}
          disabled={busy !== ''}
          className="flex items-center gap-1.5 rounded-lg bg-accent-warm px-3 py-2 text-xs font-medium text-black disabled:opacity-40"
        >
          {busy === 'connect' ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
          {busy === 'connect' ? 'Looking for a free engine…' : 'Connect now'}
        </button>
        <span className="text-[11px] text-text-muted">
          {sources.length > 0
            ? `${sources.length} public engine(s) connected`
            : 'not connected yet — Maestro keeps looking on its own'}
        </span>
      </div>

      {providers.length === 0 ? (
        <p className="mt-3 text-[11px] text-text-muted">
          No public engine connected yet. Studio and Films still work, and any request that needs a
          render will look for one before it is refused.
        </p>
      ) : (
        <ul className="mt-3 flex flex-col gap-3">
          {providers.map(provider => (
            <li key={provider.provider_id} className="rounded-lg border border-border bg-bg-tertiary p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className={`h-2.5 w-2.5 shrink-0 rounded-full ${
                    provider.online ? 'bg-accent-green' : 'bg-text-muted'
                  }`}
                  title={provider.online ? 'answered a recent probe' : 'not answering'}
                />
                <span className="text-xs font-medium text-text-primary">{provider.space_id}</span>
                <span className="rounded-md border border-border px-1.5 py-0.5 text-[10px] text-text-muted">
                  {provider.hardware || 'free GPU'}
                </span>
                <span className="rounded-md border border-accent-green/40 px-1.5 py-0.5 text-[10px] text-text-secondary">
                  free
                </span>
                <span className="ml-auto flex items-center gap-1.5">
                  <button
                    type="button"
                    onClick={() => void probe(provider.provider_id)}
                    disabled={busy === provider.provider_id}
                    className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-[10px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
                  >
                    {busy === provider.provider_id ? (
                      <Loader2 size={11} className="animate-spin" />
                    ) : (
                      <RefreshCw size={11} />
                    )}
                    Probe
                  </button>
                  <button
                    type="button"
                    onClick={() => void forget(provider.provider_id)}
                    disabled={busy === provider.provider_id}
                    className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-[10px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
                  >
                    <Trash2 size={11} /> Forget
                  </button>
                </span>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                {provider.capabilities.map(capability => (
                  <span
                    key={capability}
                    className="rounded-md border border-accent-warm/40 bg-accent-warm/10 px-1.5 py-0.5 text-[10px] text-text-primary"
                  >
                    {capability.replace(/_/g, ' ')}
                    {provider.endpoints?.[capability]?.api_name
                      ? ` · ${provider.endpoints[capability].api_name}`
                      : ''}
                  </span>
                ))}
              </div>
              <div className="mt-1 text-[10px] text-text-muted">
                {provider.accepts_media ? 'can receive a start frame · ' : ''}
                last answered <RelativeTime value={((provider.last_probe || {}).at as number) ?? null} />
                {' · '}
                this computer's worker sends the job
              </div>
            </li>
          ))}
        </ul>
      )}

      {empty.length > 0 && providers.length > 0 && (
        <p className="mt-2 text-[10px] text-text-muted">
          Nothing is connected right now for: {empty.map(name => name.replace(/_/g, ' ')).join(', ')}.
          Maestro asks the public Hub again on its own; those requests wait instead of failing.
        </p>
      )}

      <button
        type="button"
        onClick={() => setOpen(value => !value)}
        className="mt-3 flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs text-text-secondary hover:bg-bg-hover hover:text-text-primary"
      >
        <Plug size={13} /> {open ? 'Cancel' : 'Use one particular public Space'}
      </button>

      {open && (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <input
            value={form.space}
            onChange={event => setForm({ ...form, space: event.target.value })}
            placeholder="owner/space or https://…hf.space"
            className="rounded-lg border border-border bg-bg-tertiary px-3 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none"
          />
          <select
            value={form.capability}
            onChange={event => setForm({ ...form, capability: event.target.value })}
            className="rounded-lg border border-border bg-bg-tertiary px-3 py-2.5 text-sm text-text-primary focus:border-accent-warm focus:outline-none"
          >
            {(data?.served_capabilities || []).map(capability => (
              <option key={capability} value={capability}>
                {capability.replace(/_/g, ' ')}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => void add()}
            disabled={!form.space.trim() || busy === 'add'}
            className="rounded-lg bg-accent-warm px-3 py-2.5 text-xs font-medium text-black disabled:opacity-40 sm:col-span-2"
          >
            {busy === 'add' ? 'Reading that Space’s own API…' : 'Add and probe'}
          </button>
          <p className="text-[10px] text-text-muted sm:col-span-2">
            The Space is probed before it is used, so one that cannot run the task is reported
            instead of receiving work.
          </p>
        </div>
      )}

      {notice && (
        <p className="mt-2 flex items-start gap-2 rounded-lg border border-accent-green/40 bg-accent-green/10 p-2.5 text-[11px] text-text-primary">
          <CheckCircle2 size={12} className="mt-0.5 shrink-0 text-accent-green" />
          <span className="flex-1">{notice}</span>
        </p>
      )}
      {error && (
        <p className="mt-2 flex items-start gap-2 rounded-lg border border-chip-red/40 bg-chip-red/10 p-2.5 text-[11px] text-text-primary">
          <AlertTriangle size={12} className="mt-0.5 shrink-0 text-chip-red" />
          <span className="flex-1">{error}</span>
        </p>
      )}
    </section>
  )
}

/**
 * Remote computers — another Maestro on the private network that renders what
 * this one queues, and hands the finished files back into this workspace. A peer
 * counts as online only while its last probe is fresh, and its capabilities come
 * from its own report or from what you declared; the token is stored on this
 * computer and is never returned by the API.
 */
function RemotePeersSection({ onChanged }: { onChanged: () => void }) {
  const [peers, setPeers] = useState<RemotePeerInfo[]>([])
  const [engines, setEngines] = useState<string[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState('')
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ peerId: '', label: '', baseUrl: '', token: '' })

  const refresh = useCallback(async () => {
    try {
      const data = await fetchRemotePeers()
      setPeers(data.peers)
      setEngines(data.engines)
      setError('')
    } catch (err) {
      setError((err as Error).message)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const add = useCallback(async () => {
    setBusy('add')
    setError('')
    setNotice('')
    try {
      const result = await addRemotePeer({
        peerId: form.peerId.trim(),
        label: form.label.trim() || undefined,
        baseUrl: form.baseUrl.trim(),
        token: form.token.trim() || undefined,
      })
      setNotice(
        result.online
          ? `${result.peer.label} answered and can render what it reported.${workerNotice(result.probe)}`
          : `${result.peer.label} was saved, but it did not answer yet: jobs wait for it instead of failing.`,
      )
      setForm({ peerId: '', label: '', baseUrl: '', token: '' })
      setOpen(false)
      await refresh()
      onChanged()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy('')
    }
  }, [form, refresh, onChanged])

  const probe = useCallback(
    async (peerId: string) => {
      setBusy(peerId)
      setError('')
      setNotice('')
      try {
        const result = await probeRemotePeer(peerId)
        setNotice(
          result.online
            ? `${result.peer.label} answered: ${result.peer.capabilities.join(', ') || 'no capability reported'}.${workerNotice(result.probe)}`
            : `${result.peer.label} did not answer. Jobs stay queued until it does.`,
        )
        await refresh()
        onChanged()
      } catch (err) {
        setError((err as Error).message)
      } finally {
        setBusy('')
      }
    },
    [refresh, onChanged],
  )

  const forget = useCallback(
    async (peerId: string) => {
      setBusy(peerId)
      setError('')
      setNotice('')
      try {
        const result = await removeRemotePeer(peerId)
        setNotice(result.note)
        await refresh()
        onChanged()
      } catch (err) {
        setError((err as Error).message)
      } finally {
        setBusy('')
      }
    },
    [refresh, onChanged],
  )

  return (
    <section className="rounded-xl border border-border bg-bg-secondary p-4">
      <div className="flex items-center gap-2">
        <Server size={15} className="text-accent-warm" />
        <h2 className="text-sm font-semibold text-text-primary">Remote computers with a GPU</h2>
      </div>
      <p className="mt-1 text-xs text-text-muted">
        A peer is another Maestro on your private network. When this computer cannot generate — no
        CUDA GPU — Studio and Films queue the work on that computer's own engine and download the
        result back here, so the gallery, the storyboard and the editor see it like any other render.
        Saving a peer is the whole setup: from then on the peers are asked again automatically, the
        queue worker on the computer with the GPU is started when work is handed over, and the media
        a request brings from here (a start frame, references, a control video) is sent to that
        computer before the job, so it renders the same video. If that computer is off, the job waits
        instead of failing.
      </p>

      {peers.length === 0 ? (
        <p className="mt-3 text-[11px] text-text-muted">
          No remote computer configured. Add the machine that has the GPU — the address it is reachable
          at (its Tailscale name, or its LAN address) and the token you set there as
          {' '}
          <span className="font-mono text-text-secondary">MAESTRO_ACCESS_TOKEN</span>.
        </p>
      ) : (
        <ul className="mt-3 flex flex-col gap-3">
          {peers.map(peer => {
            // Measured, not assumed: the peer's own report says whether its queue
            // worker is running. A stopped worker is why a delegated render would
            // arrive there and wait, so it is shown instead of left to be guessed.
            const workerStopped =
              peer.online &&
              (peer.last_probe as { queue_worker_running?: boolean } | null)?.queue_worker_running === false
            return (
            <li key={peer.peer_id} className="rounded-lg border border-border bg-bg-tertiary p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className={`h-2.5 w-2.5 shrink-0 rounded-full ${
                    peer.online ? 'bg-accent-green' : 'bg-text-muted'
                  }`}
                  title={peer.online ? 'answered a recent probe' : 'not answering'}
                />
                <span className="text-xs font-medium text-text-primary">{peer.label}</span>
                <span className="text-[10px] text-text-muted">{peer.base_url}</span>
                {!peer.token_set && (
                  <span className="rounded-md border border-indicator-warning/40 px-1.5 py-0.5 text-[10px] text-text-primary">
                    no token
                  </span>
                )}
                <span className="ml-auto flex items-center gap-1.5">
                  <button
                    type="button"
                    onClick={() => void probe(peer.peer_id)}
                    disabled={busy === peer.peer_id}
                    className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-[10px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
                  >
                    {busy === peer.peer_id ? (
                      <Loader2 size={11} className="animate-spin" />
                    ) : (
                      <RefreshCw size={11} />
                    )}
                    Probe
                  </button>
                  <button
                    type="button"
                    onClick={() => void forget(peer.peer_id)}
                    disabled={busy === peer.peer_id}
                    className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-[10px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
                  >
                    <Trash2 size={11} /> Forget
                  </button>
                </span>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                {peer.capabilities.length === 0 ? (
                  <span className="text-[10px] text-text-muted">
                    No capability reported yet — probe it.
                  </span>
                ) : (
                  peer.capabilities.map(capability => (
                    <span
                      key={capability}
                      className="rounded-md border border-accent-warm/40 bg-accent-warm/10 px-1.5 py-0.5 text-[10px] text-text-primary"
                    >
                      {capability.replace(/_/g, ' ')}
                    </span>
                  ))
                )}
              </div>
              <div className="mt-1 text-[10px] text-text-muted">
                Queue engine {peer.engine_id} · this computer's worker hands jobs over
              </div>
              {workerStopped && (
                <p className="mt-1.5 flex items-start gap-1.5 text-[10px] text-text-secondary">
                  <AlertTriangle size={11} className="mt-0.5 shrink-0 text-indicator-warning" />
                  <span>
                    That computer answered, but its own queue worker is not running: the next render
                    sent to it starts it there automatically. To start it now, use Probe or send a
                    render.
                  </span>
                </p>
              )}
            </li>
            )
          })}
        </ul>
      )}

      {engines.length > 0 && (
        <p className="mt-2 text-[10px] text-text-muted">
          Delegation engines registered here: {engines.join(', ')}
        </p>
      )}

      <button
        type="button"
        onClick={() => setOpen(value => !value)}
        className="mt-3 flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs text-text-secondary hover:bg-bg-hover hover:text-text-primary"
      >
        <Plug size={13} /> {open ? 'Cancel' : 'Add a remote computer'}
      </button>

      {open && (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <input
            value={form.peerId}
            onChange={event => setForm({ ...form, peerId: event.target.value })}
            placeholder="id (e.g. desk-1)"
            className="rounded-lg border border-border bg-bg-tertiary px-3 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none"
          />
          <input
            value={form.label}
            onChange={event => setForm({ ...form, label: event.target.value })}
            placeholder="label (e.g. PC with RTX 4070)"
            className="rounded-lg border border-border bg-bg-tertiary px-3 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none"
          />
          <input
            value={form.baseUrl}
            onChange={event => setForm({ ...form, baseUrl: event.target.value })}
            placeholder="https://pc.tailnet.ts.net"
            className="rounded-lg border border-border bg-bg-tertiary px-3 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none"
          />
          <input
            value={form.token}
            onChange={event => setForm({ ...form, token: event.target.value })}
            placeholder="MAESTRO_ACCESS_TOKEN of that computer"
            type="password"
            className="rounded-lg border border-border bg-bg-tertiary px-3 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none"
          />
          <button
            type="button"
            onClick={() => void add()}
            disabled={!form.peerId.trim() || !form.baseUrl.trim() || busy === 'add'}
            className="rounded-lg bg-accent-warm px-3 py-2.5 text-xs font-medium text-black disabled:opacity-40 sm:col-span-2"
          >
            {busy === 'add' ? 'Probing that computer…' : 'Add and probe'}
          </button>
        </div>
      )}

      {notice && (
        <p className="mt-2 flex items-start gap-2 rounded-lg border border-accent-green/40 bg-accent-green/10 p-2.5 text-[11px] text-text-primary">
          <CheckCircle2 size={12} className="mt-0.5 shrink-0 text-accent-green" />
          <span className="flex-1">{notice}</span>
        </p>
      )}
      {error && (
        <p className="mt-2 flex items-start gap-2 rounded-lg border border-chip-red/40 bg-chip-red/10 p-2.5 text-[11px] text-text-primary">
          <AlertTriangle size={12} className="mt-0.5 shrink-0 text-chip-red" />
          <span className="flex-1">{error}</span>
        </p>
      )}
    </section>
  )
}


export function StudioComputePanel() {
  const [status, setStatus] = useState<EnginesStatus | null>(null)
  const [nodes, setNodes] = useState<ComputeNodeInfo[]>([])
  const [parked, setParked] = useState<ComputeAssignments['parked']>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [engines, compute] = await Promise.all([fetchEngines(), fetchComputeAssignments()])
      setStatus(engines)
      setNodes(compute.summary.nodes)
      setParked(compute.parked)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // Real parked work, straight from the assignment registry — not a guess.
  const parkedByNode = useMemo(() => {
    const map = new Map<string, number>()
    parked.forEach(entry => {
      map.set(entry.node_id, (map.get(entry.node_id) || 0) + 1)
    })
    return map
  }, [parked])

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => void refresh()}
          className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs text-text-secondary hover:bg-bg-hover hover:text-text-primary"
        >
          {loading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} Refresh
        </button>
        {status && (
          <span className="text-[11px] text-text-muted">
            {status.compute.online_count} of {status.compute.node_count} node(s) online ·{' '}
            {status.compute.active_assignments} running · {status.compute.waiting_for_node} waiting for
            a node
          </span>
        )}
      </div>

      {error && (
        <p className="flex items-start gap-2 rounded-lg border border-chip-red/40 bg-chip-red/10 p-2.5 text-[11px] text-text-primary">
          <AlertTriangle size={12} className="mt-0.5 shrink-0 text-chip-red" />
          <span className="flex-1">{error}</span>
        </p>
      )}

      {status && <CapabilityTable status={status} />}

      <PublicComputeSection onChanged={() => void refresh()} />

      <section className="rounded-xl border border-border bg-bg-secondary p-4">
        <div className="flex items-center gap-2">
          <Users size={15} className="text-accent-warm" />
          <h2 className="text-sm font-semibold text-text-primary">Compute nodes</h2>
        </div>
        <p className="mt-1 text-xs text-text-muted">
          The node that renders does not have to be the device you direct from. A node is online only
          while its heartbeat is fresh.
        </p>
        {nodes.length === 0 ? (
          <p className="mt-3 text-[11px] text-text-muted">
            No node registered yet. This machine can still plan, import and assemble.
          </p>
        ) : (
          <ul className="mt-3 flex flex-col gap-3">
            {nodes.map(node => (
              <NodeCard
                key={node.node_id}
                node={node}
                parked={parkedByNode.get(node.node_id) || 0}
                onChanged={() => void refresh()}
              />
            ))}
          </ul>
        )}
      </section>

      <RegisterNodeForm onChanged={() => void refresh()} />
      <RemotePeersSection onChanged={() => void refresh()} />
      <ComfyProbeCard onRegistered={() => void refresh()} />
    </div>
  )
}
