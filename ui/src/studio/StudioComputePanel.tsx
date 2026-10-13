import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
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
  cancelComfyRun,
  fetchComfyRun,
  fetchComfyTemplates,
  fetchComputeAssignments,
  fetchEngines,
  probeComfyUi,
  recoverComputeNode,
  registerComputeNode,
  removeComputeNode,
  runComfyTemplate,
} from '../api/engines'
import type { ComfyRunState } from '../api/engines'
import type {
  ComputeAssignments,
  ComputeNodeInfo,
  ComfyProbeResult,
  ComfyTemplateDetail,
  EnginesStatus,
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
      <ComfyProbeCard onRegistered={() => void refresh()} />
    </div>
  )
}
