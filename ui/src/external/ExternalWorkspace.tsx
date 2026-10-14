import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  Film,
  Image as ImageIcon,
  KeyRound,
  Loader2,
  RefreshCw,
  ShieldCheck,
  Trash2,
  X,
} from 'lucide-react'
import { AppModeToggle, MaestroBrand } from '../components/AppModeNavigation'
import { ModelLibrary } from './ModelLibrary'
import { useStore } from '../stores/useStore'
import {
  ExternalApiError,
  cancelExternalJob,
  clearExternalCredentials,
  fetchExternalJobs,
  fetchExternalProviders,
  quoteExternalJob,
  submitExternalJob,
  testExternalProvider,
  updateExternalProvider,
  type ExternalJob,
  type ExternalModality,
  type ExternalProviderInfo,
  type ExternalProvidersResponse,
  type ExternalQuote,
} from '../api/external'

const ACTIVE_STATES = new Set(['queued', 'submitting', 'running', 'downloading', 'cancelling'])

const inputClass =
  'w-full rounded-lg border border-border bg-bg-tertiary px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none'

// Numeric pairs are ranges ([81, 100] → 81–100); other lists are choices.
function formatLimit(value: unknown): string {
  if (!Array.isArray(value)) return String(value)
  return value.length === 2 && value.every(item => typeof item === 'number') ? value.join('–') : value.join(', ')
}

function errorText(error: unknown): string {
  if (error instanceof ExternalApiError) return error.code ? `${error.message} [${error.code}]` : error.message
  return error instanceof Error ? error.message : String(error)
}

function ProviderCard({
  provider,
  onChanged,
}: {
  provider: ExternalProviderInfo
  onChanged: (next: ExternalProvidersResponse) => void
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)

  const run = async (action: () => Promise<void>) => {
    setBusy(true)
    setMessage(null)
    try {
      await action()
    } catch (error) {
      setMessage({ ok: false, text: errorText(error) })
    } finally {
      setBusy(false)
    }
  }

  const save = () =>
    run(async () => {
      const credentials = Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim()))
      if (!Object.keys(credentials).length) return
      onChanged(await updateExternalProvider(provider.id, { credentials }))
      setValues({})
      setMessage({ ok: true, text: 'Saved in secure storage.' })
    })

  return (
    <section className="rounded-xl border border-border bg-bg-secondary p-4">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-text-primary">{provider.label}</h3>
          <p className="mt-0.5 text-[11px] text-text-muted">
            {provider.modalities.join(' + ')} · facts verified {provider.verified_on}
          </p>
        </div>
        <label className="flex items-center gap-2 text-xs text-text-secondary">
          <input
            type="checkbox"
            checked={provider.enabled}
            disabled={busy}
            onChange={event =>
              void run(async () => onChanged(await updateExternalProvider(provider.id, { enabled: event.target.checked })))
            }
          />
          Enabled
        </label>
      </div>

      <dl className="mt-3 grid gap-1.5 text-[11px] leading-relaxed text-text-secondary">
        <div><dt className="inline font-medium text-text-primary">Account: </dt><dd className="inline">{provider.account}</dd></div>
        <div><dt className="inline font-medium text-text-primary">Billing: </dt><dd className="inline">{provider.billing}</dd></div>
        {provider.free_quota && (
          <div><dt className="inline font-medium text-text-primary">Free quota: </dt><dd className="inline">{provider.free_quota}</dd></div>
        )}
        <div><dt className="inline font-medium text-text-primary">Data retention: </dt><dd className="inline">{provider.retention}</dd></div>
        <div><dt className="inline font-medium text-text-primary">Cancellation: </dt><dd className="inline">{provider.cancel_note}</dd></div>
        {Object.entries(provider.extra_models || {}).map(([key, model]) => (
          <div key={key}>
            <dt className="inline font-medium text-text-primary">{model.modality}: </dt>
            <dd className="inline">{model.label} (<code>{model.model}</code>) — {model.license}</dd>
          </div>
        ))}
        {Object.entries(provider.models).map(([modality, model]) => (
          <div key={modality}>
            <dt className="inline font-medium text-text-primary">{modality}: </dt>
            <dd className="inline">
              {model?.label} (<code>{model?.model}</code>){model?.price_basis ? ` — ${model.price_basis}` : ''}
            </dd>
          </div>
        ))}
      </dl>

      <div className="mt-3 flex flex-wrap gap-3 text-[11px]">
        <a className="inline-flex items-center gap-1 text-accent-warm hover:underline" href={provider.signup} target="_blank" rel="noreferrer">
          Create account <ExternalLink size={11} />
        </a>
        <a className="inline-flex items-center gap-1 text-accent-warm hover:underline" href={provider.terms} target="_blank" rel="noreferrer">
          Terms <ExternalLink size={11} />
        </a>
        {provider.docs.map(url => (
          <a key={url} className="inline-flex items-center gap-1 text-text-muted hover:text-text-primary hover:underline" href={url} target="_blank" rel="noreferrer">
            {new URL(url).pathname.split('/').filter(Boolean).slice(-1)[0] || 'docs'} <ExternalLink size={11} />
          </a>
        ))}
      </div>

      <div className="mt-4 grid gap-2">
        {provider.credentials.map(cred => (
          <label key={cred.field} className="grid gap-1">
            <span className="flex flex-wrap items-center gap-2 text-[11px] text-text-secondary">
              <KeyRound size={11} /> {cred.label}
              {cred.set ? (
                <span className="text-accent-green">
                  set {cred.hint} · {cred.source === 'env' ? `from ${cred.env_var}` : cred.source}
                </span>
              ) : (
                <span className="text-amber-400">not set{cred.env_var ? ` (or set ${cred.env_var})` : ''}</span>
              )}
            </span>
            <input
              type={cred.secret ? 'password' : 'text'}
              autoComplete="off"
              spellCheck={false}
              value={values[cred.field] || ''}
              placeholder={cred.set ? 'Enter a new value to replace it' : cred.label}
              onChange={event => setValues(prev => ({ ...prev, [cred.field]: event.target.value }))}
              className={inputClass}
            />
          </label>
        ))}
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy || !Object.values(values).some(v => v.trim())}
          onClick={() => void save()}
          className="rounded-lg bg-accent-warm px-3 py-1.5 text-xs font-medium text-black disabled:opacity-40"
        >
          Save keys
        </button>
        <button
          type="button"
          disabled={busy || !provider.configured}
          onClick={() =>
            void run(async () => {
              const result = await testExternalProvider(provider.id)
              setMessage({ ok: true, text: `${result.message} ${result.note || ''}`.trim() })
            })
          }
          className="rounded-lg border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-bg-hover disabled:opacity-40"
        >
          Check key (free)
        </button>
        <button
          type="button"
          disabled={busy || !provider.credentials.some(c => c.set && c.source !== 'env')}
          onClick={() => void run(async () => onChanged(await clearExternalCredentials(provider.id)))}
          className="inline-flex items-center gap-1 rounded-lg border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-bg-hover disabled:opacity-40"
        >
          <Trash2 size={12} /> Remove keys
        </button>
        {busy && <Loader2 size={14} className="animate-spin self-center text-text-muted" />}
      </div>
      {message && (
        <p className={`mt-2 text-[11px] ${message.ok ? 'text-accent-green' : 'text-red-400'}`}>{message.text}</p>
      )}
    </section>
  )
}

function DisclosurePanel({
  quote,
  onConfirm,
  onClose,
  busy,
}: {
  quote: ExternalQuote
  onConfirm: () => void
  onClose: () => void
  busy: boolean
}) {
  const [accepted, setAccepted] = useState(false)
  const d = quote.disclosure
  return (
    <section className="rounded-xl border border-accent-warm/60 bg-bg-secondary p-4" aria-label="Review before sending">
      <div className="flex items-start gap-2">
        <ShieldCheck size={16} className="mt-0.5 shrink-0 text-accent-warm" />
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-text-primary">Review before sending to {d.provider}</h3>
          <p className="text-[11px] text-text-muted">
            {d.model_label} (<code>{d.model}</code>) on {d.endpoint_host} · {d.model_license}
          </p>
        </div>
        <button type="button" onClick={onClose} className="rounded p-1 text-text-muted hover:bg-bg-hover" title="Close">
          <X size={14} />
        </button>
      </div>
      <div className="mt-3 grid gap-3 text-xs text-text-secondary md:grid-cols-2">
        <div>
          <div className="font-medium text-text-primary">Estimated cost</div>
          <p className={d.cost.free ? 'text-accent-green' : 'text-amber-400'}>{d.cost.summary}</p>
          <p className="mt-1 text-[11px] text-text-muted">
            Basis: {d.cost.basis}.{' '}
            <a href={d.cost.source} target="_blank" rel="noreferrer" className="text-accent-warm hover:underline">
              Check current price
            </a>
          </p>
        </div>
        <div>
          <div className="font-medium text-text-primary">Data that leaves this computer</div>
          <ul className="list-disc pl-4">
            {d.data_sent.map(item => <li key={item}>{item}</li>)}
          </ul>
          <p className="mt-1 text-[11px] text-text-muted">{d.not_sent}</p>
        </div>
        <div>
          <div className="font-medium text-text-primary">Account</div>
          <p>{d.account}</p>
        </div>
        <div>
          <div className="font-medium text-text-primary">Limits</div>
          <p>
            Timeout {Math.round(d.timeout_s / 60)} min
            {d.duration_s ? ` · clip ≈ ${d.duration_s} s` : ''}
            {Object.entries(d.limits).map(([key, value]) => ` · ${key.replace(/_/g, ' ')}: ${formatLimit(value)}`)}
          </p>
          <p className="mt-1 text-[11px] text-text-muted">{d.cancel_note}</p>
          <p className="mt-1 text-[11px] text-text-muted">{d.retention}</p>
        </div>
      </div>
      <label className="mt-3 flex items-start gap-2 text-xs text-text-primary">
        <input type="checkbox" checked={accepted} onChange={event => setAccepted(event.target.checked)} className="mt-0.5" />
        <span>
          I want to send the data above to {d.provider} and accept the cost shown ({d.cost.free ? 'free quota' : 'billed to my account'}).
        </span>
      </label>
      <button
        type="button"
        disabled={!accepted || busy}
        onClick={onConfirm}
        className="mt-3 rounded-lg bg-accent-warm px-4 py-2 text-xs font-semibold text-black disabled:opacity-40"
      >
        {busy ? 'Starting…' : 'Confirm and start'}
      </button>
    </section>
  )
}

function JobCard({ job, onCancel }: { job: ExternalJob; onCancel: (id: string) => void }) {
  const active = ACTIVE_STATES.has(job.state)
  const color =
    job.state === 'succeeded' ? 'text-accent-green' : job.state === 'failed' ? 'text-red-400' : job.state === 'cancelled' ? 'text-text-muted' : 'text-amber-400'
  return (
    <li className="rounded-xl border border-border bg-bg-secondary p-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {job.modality === 'video' ? <Film size={13} /> : <ImageIcon size={13} />}
        <span className="font-medium text-text-primary">{job.provider_label}</span>
        <span className={`font-semibold uppercase tracking-wide ${color}`}>{job.state}</span>
        {active && <Loader2 size={12} className="animate-spin text-text-muted" />}
        <span className="text-[11px] text-text-muted">{new Date(job.created_at * 1000).toLocaleString()}</span>
        {job.remote_id && <span className="text-[11px] text-text-muted">id {job.remote_id}</span>}
        {job.can_cancel && (
          <button
            type="button"
            onClick={() => onCancel(job.id)}
            className="ml-auto rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary hover:bg-bg-hover"
          >
            Cancel
          </button>
        )}
      </div>
      <p className="mt-1 line-clamp-2 text-xs text-text-secondary">{job.request.prompt}</p>
      <p className="mt-1 text-[11px] text-text-muted">{job.disclosure.cost.summary}</p>
      {job.error ? (
        <p className="mt-1 flex items-start gap-1 text-[11px] text-red-400">
          <AlertTriangle size={12} className="mt-0.5 shrink-0" />
          <span>
            {job.error.message} <code>[{job.error.code}{job.error.http_status ? ` · HTTP ${job.error.http_status}` : ''}]</code>
          </span>
        </p>
      ) : (
        <p className="mt-1 text-[11px] text-text-secondary">{job.progress}</p>
      )}
      {job.result && (
        <div className="mt-2">
          {job.result.kind === 'video' ? (
            <video src={job.result.url} controls className="max-h-80 w-full rounded-lg bg-black" />
          ) : (
            <img src={job.result.url} alt={job.request.prompt} className="max-h-80 rounded-lg" />
          )}
          <div className="mt-1 flex flex-wrap gap-3 text-[11px] text-text-muted">
            <span className="inline-flex items-center gap-1 text-accent-green">
              <CheckCircle2 size={11} /> {job.result.filename} · {(job.result.bytes / 1024).toFixed(0)} KB · workspace {job.result.workspace}
            </span>
            <a href={job.result.url} download={job.result.filename} className="text-accent-warm hover:underline">Download</a>
          </div>
        </div>
      )}
    </li>
  )
}

export function ExternalWorkspace() {
  const loadOutputs = useStore(s => s.loadOutputs)
  const [status, setStatus] = useState<ExternalProvidersResponse | null>(null)
  const [jobs, setJobs] = useState<ExternalJob[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [modality, setModality] = useState<ExternalModality>('image')
  const [providerId, setProviderId] = useState('')
  const [prompt, setPrompt] = useState('')
  const [aspect, setAspect] = useState('')
  const [steps, setSteps] = useState(4)
  const [modelKey, setModelKey] = useState('default')
  const [size, setSize] = useState('')
  const [resolution, setResolution] = useState('480p')
  const [frames, setFrames] = useState(81)
  const [fps, setFps] = useState(16)
  const [seed, setSeed] = useState('')
  const [quote, setQuote] = useState<ExternalQuote | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [tab, setTab] = useState<'library' | 'cloud'>('library')

  const refresh = useCallback(async () => {
    try {
      const [providers, jobList] = await Promise.all([fetchExternalProviders(), fetchExternalJobs()])
      setStatus(providers)
      setJobs(jobList.jobs)
      setLoadError(null)
    } catch (error) {
      setLoadError(errorText(error))
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const anyActive = jobs.some(job => ACTIVE_STATES.has(job.state))
  useEffect(() => {
    if (!anyActive) return
    const timer = setInterval(() => {
      void fetchExternalJobs()
        .then(result => {
          setJobs(previous => {
            const finished = result.jobs.some(
              job => job.state === 'succeeded' && previous.find(old => old.id === job.id && old.state !== 'succeeded'),
            )
            if (finished) void loadOutputs()
            return result.jobs
          })
        })
        .catch(() => undefined)
    }, 2000)
    return () => clearInterval(timer)
  }, [anyActive, loadOutputs])

  const candidates = useMemo(
    () => (status?.providers || []).filter(p => p.modalities.includes(modality)),
    [status, modality],
  )
  const provider = candidates.find(p => p.id === providerId) || candidates.find(p => p.enabled && p.configured) || candidates[0]
  const modelOptions = useMemo(() => {
    if (!provider) return []
    const base = provider.models[modality]
    const extra = Object.entries(provider.extra_models || {}).filter(([, m]) => m.modality === modality)
    return [...(base ? [['default', base] as const] : []), ...extra]
  }, [provider, modality])
  const effectiveModelKey = modelOptions.some(([key]) => key === modelKey) ? modelKey : 'default'
  const model = modelOptions.find(([key]) => key === effectiveModelKey)?.[1]
  const sizeOptions = (model?.limits.sizes as string[] | undefined) || []
  const effectiveSize = sizeOptions.includes(size) ? size : sizeOptions[0] || ''
  const aspectOptions = (model?.limits.aspect_ratios as string[] | undefined) || []
  const effectiveAspect = aspectOptions.includes(aspect) ? aspect : aspectOptions[0] || ''

  const params = useMemo(() => {
    if (!provider) return null
    const out: Record<string, unknown> = { provider: provider.id, modality, prompt }
    if (seed.trim()) out.seed = seed.trim()
    if (effectiveModelKey !== 'default') out.model_key = effectiveModelKey
    if (effectiveSize) out.size = effectiveSize
    else if (provider.id === 'cloudflare') out.steps = steps
    if (effectiveAspect) out.aspect_ratio = effectiveAspect
    if (modality === 'video') Object.assign(out, { resolution, num_frames: frames, frames_per_second: fps })
    return out
  }, [provider, modality, prompt, seed, steps, effectiveAspect, effectiveModelKey, effectiveSize, resolution, frames, fps])

  // Any change to the request invalidates a disclosure already shown.
  useEffect(() => {
    setQuote(null)
  }, [params])

  const review = async () => {
    if (!params) return
    setFormError(null)
    try {
      setQuote(await quoteExternalJob(params))
    } catch (error) {
      setFormError(errorText(error))
    }
  }

  const confirm = async () => {
    if (!params || !quote) return
    setBusy(true)
    setFormError(null)
    try {
      const job = await submitExternalJob(params, quote.confirm_token)
      setJobs(previous => [job, ...previous.filter(item => item.id !== job.id)])
      setQuote(null)
    } catch (error) {
      setFormError(errorText(error))
    } finally {
      setBusy(false)
    }
  }

  const cancel = async (jobId: string) => {
    try {
      const job = await cancelExternalJob(jobId)
      setJobs(previous => previous.map(item => (item.id === job.id ? job : item)))
    } catch (error) {
      setFormError(errorText(error))
    }
  }

  const blocked = !provider
    ? 'No provider offers this modality.'
    : !provider.configured
      ? `${provider.label} has no API key yet. Add it under Providers.`
      : !provider.enabled
        ? `${provider.label} is disabled. Enable it under Providers to send anything.`
        : null

  return (
    <div className="flex h-full w-full flex-col bg-bg-primary">
      <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-border bg-bg-secondary px-3 py-2">
        <MaestroBrand compact />
        <div className="leading-tight">
          <div className="text-sm font-semibold text-text-primary">Models &amp; cloud</div>
          <div className="text-[10px] text-text-muted">Model library, downloads and opt-in cloud generation</div>
        </div>
        <div className="flex rounded-lg border border-border bg-bg-tertiary p-0.5" role="tablist">
          {([['library', 'Model library'], ['cloud', 'Cloud generation']] as const).map(([id, label]) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={tab === id}
              onClick={() => setTab(id)}
              className={`rounded-md px-3 py-1 text-xs ${tab === id ? 'bg-bg-hover text-text-primary' : 'text-text-secondary hover:text-text-primary'}`}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={() => void refresh()}
            className="rounded-md p-1.5 text-text-muted hover:bg-bg-hover hover:text-text-primary"
            title="Refresh"
          >
            <RefreshCw size={14} />
          </button>
          <AppModeToggle size="sm" />
        </div>
      </header>

      {tab === 'library' ? <ModelLibrary /> : (
      <div className="grid min-h-0 flex-1 gap-4 overflow-y-auto p-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col gap-4">
          {loadError && <p className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-300">{loadError}</p>}
          <section className="rounded-xl border border-border bg-bg-secondary p-4">
            <h2 className="text-sm font-semibold text-text-primary">Generate</h2>
            <p className="mt-1 text-[11px] text-text-muted">
              {status?.policy || 'Loading providers…'} Local engines in Studio are unchanged and never send anything.
            </p>
            <div className="mt-3 flex gap-2" role="group" aria-label="Modality">
              {(['image', 'video'] as ExternalModality[]).map(item => (
                <button
                  key={item}
                  type="button"
                  onClick={() => setModality(item)}
                  className={`inline-flex items-center gap-1 rounded-lg border px-3 py-1.5 text-xs ${
                    modality === item ? 'border-accent-warm bg-bg-hover text-text-primary' : 'border-border text-text-secondary hover:bg-bg-hover'
                  }`}
                >
                  {item === 'image' ? <ImageIcon size={13} /> : <Film size={13} />} {item === 'image' ? 'Image' : 'Video'}
                </button>
              ))}
            </div>
            <label className="mt-3 grid gap-1 text-[11px] text-text-secondary">
              Provider
              <select
                value={provider?.id || ''}
                onChange={event => setProviderId(event.target.value)}
                className={inputClass}
              >
                {candidates.map(p => (
                  <option key={p.id} value={p.id}>
                    {p.label}
                    {!p.configured ? ' (no key)' : !p.enabled ? ' (disabled)' : ''}
                  </option>
                ))}
              </select>
            </label>
            {modelOptions.length > 1 && (
              <label className="mt-3 grid gap-1 text-[11px] text-text-secondary">
                Model
                <select value={effectiveModelKey} onChange={event => setModelKey(event.target.value)} className={inputClass}>
                  {modelOptions.map(([key, m]) => (
                    <option key={key} value={key}>{m.label} — {m.license}</option>
                  ))}
                </select>
              </label>
            )}
            <label className="mt-3 grid gap-1 text-[11px] text-text-secondary">
              Description
              <textarea
                value={prompt}
                onChange={event => setPrompt(event.target.value)}
                rows={4}
                maxLength={2000}
                placeholder={modality === 'image' ? 'A lighthouse on a cliff at dusk, cinematic light' : 'Slow aerial shot over ocean waves at sunrise'}
                className={inputClass}
              />
            </label>
            <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
              {aspectOptions.length > 0 && (
                <label className="grid gap-1 text-[11px] text-text-secondary">
                  Aspect
                  <select value={effectiveAspect} onChange={event => setAspect(event.target.value)} className={inputClass}>
                    {aspectOptions.map(option => <option key={option}>{option}</option>)}
                  </select>
                </label>
              )}
              {sizeOptions.length > 0 && (
                <label className="grid gap-1 text-[11px] text-text-secondary">
                  Size
                  <select value={effectiveSize} onChange={event => setSize(event.target.value)} className={inputClass}>
                    {sizeOptions.map(option => <option key={option}>{option}</option>)}
                  </select>
                </label>
              )}
              {provider?.id === 'cloudflare' && sizeOptions.length === 0 && (
                <label className="grid gap-1 text-[11px] text-text-secondary">
                  Steps (1–8)
                  <input type="number" min={1} max={8} value={steps} onChange={event => setSteps(Number(event.target.value))} className={inputClass} />
                </label>
              )}
              {modality === 'video' && (
                <>
                  <label className="grid gap-1 text-[11px] text-text-secondary">
                    Resolution
                    <select value={resolution} onChange={event => setResolution(event.target.value)} className={inputClass}>
                      <option>480p</option>
                      <option>720p</option>
                    </select>
                  </label>
                  <label className="grid gap-1 text-[11px] text-text-secondary">
                    Frames (81–100)
                    <input type="number" min={81} max={100} value={frames} onChange={event => setFrames(Number(event.target.value))} className={inputClass} />
                  </label>
                  <label className="grid gap-1 text-[11px] text-text-secondary">
                    FPS (5–24)
                    <input type="number" min={5} max={24} value={fps} onChange={event => setFps(Number(event.target.value))} className={inputClass} />
                  </label>
                </>
              )}
              <label className="grid gap-1 text-[11px] text-text-secondary">
                Seed (optional)
                <input value={seed} onChange={event => setSeed(event.target.value)} inputMode="numeric" className={inputClass} />
              </label>
            </div>
            {modality === 'video' && <p className="mt-2 text-[11px] text-text-muted">Clip length ≈ {(frames / Math.max(fps, 1)).toFixed(1)} s, no audio.</p>}
            {blocked && <p className="mt-3 text-xs text-amber-400">{blocked}</p>}
            {formError && <p className="mt-3 text-xs text-red-400">{formError}</p>}
            <button
              type="button"
              disabled={Boolean(blocked) || !prompt.trim() || Boolean(quote)}
              onClick={() => void review()}
              className="mt-3 rounded-lg bg-accent-warm px-4 py-2 text-xs font-semibold text-black disabled:opacity-40"
            >
              Review cost and data
            </button>
          </section>

          {quote && <DisclosurePanel quote={quote} busy={busy} onClose={() => setQuote(null)} onConfirm={() => void confirm()} />}

          <section>
            <h2 className="mb-2 text-sm font-semibold text-text-primary">Jobs</h2>
            {jobs.length === 0 ? (
              <p className="text-xs text-text-muted">No external jobs yet.</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {jobs.map(job => <JobCard key={job.id} job={job} onCancel={id => void cancel(id)} />)}
              </ul>
            )}
          </section>
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          <h2 className="text-sm font-semibold text-text-primary">Providers</h2>
          {status && (
            <p className="text-[11px] text-text-muted">
              Keys are stored in: {status.storage.label}. They are never shown again, never written to Maestro's settings file
              and only sent to the provider they belong to.
            </p>
          )}
          {(status?.providers || []).map(item => (
            <ProviderCard key={item.id} provider={item} onChanged={setStatus} />
          ))}
        </div>
      </div>
      )}
    </div>
  )
}
