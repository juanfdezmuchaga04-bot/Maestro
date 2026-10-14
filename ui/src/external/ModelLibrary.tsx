import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, ChevronDown, ChevronRight, Download, ExternalLink, Loader2, RefreshCw, X } from 'lucide-react'
import {
  cancelDownload,
  fetchDownloads,
  fetchLibrary,
  inspectModel,
  startDownload,
  type Accelerator,
  type DownloadJob,
  type InspectReport,
  type LibraryItem,
  type LibraryResponse,
} from '../api/modelLibrary'

const ACCELERATORS: Array<{ id: '' | Accelerator; label: string }> = [
  { id: '', label: 'This computer' },
  { id: 'cuda', label: 'NVIDIA (CUDA)' },
  { id: 'rocm', label: 'AMD (ROCm)' },
  { id: 'mps', label: 'Apple Silicon' },
  { id: 'directml', label: 'Windows DirectML' },
  { id: 'vulkan', label: 'Vulkan' },
  { id: 'cpu', label: 'CPU only' },
]

const KIND_STYLE: Record<string, string> = {
  free_recurring: 'border-accent-green/50 text-accent-green',
  free_web: 'border-accent-green/30 text-accent-green',
  public_space: 'border-accent-blue/50 text-accent-blue',
  trial_credits: 'border-amber-400/50 text-amber-400',
  paid: 'border-border text-text-muted',
}

const ACTIVE = new Set(['queued', 'checking', 'downloading'])

function gb(bytes: number | null | undefined): string {
  if (!bytes) return '0 GB'
  return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`
}

const inputClass =
  'rounded-lg border border-border bg-bg-tertiary px-2.5 py-1.5 text-xs text-text-primary focus:border-accent-warm focus:outline-none'

function ItemDetails({
  item,
  library,
  accelerator,
  onStarted,
}: {
  item: LibraryItem
  library: LibraryResponse
  accelerator: string
  onStarted: (job: DownloadJob) => void
}) {
  const [profile, setProfile] = useState('quality')
  const [report, setReport] = useState<InspectReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [accepted, setAccepted] = useState(false)
  const needsAck = item.license_ack_required.length > 0

  const check = async () => {
    setBusy(true)
    setError(null)
    try {
      setReport(await inspectModel({ id: item.id, profile, accelerator }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const download = async () => {
    setBusy(true)
    setError(null)
    try {
      onStarted(await startDownload({ id: item.id, profile, accelerator, accept_license: accepted }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-3 grid gap-3 border-t border-border pt-3 text-xs text-text-secondary">
      {item.description && <p>{item.description}</p>}
      <div className="grid gap-1">
        <div>
          <span className="font-medium text-text-primary">Licence: </span>
          <a href={item.license_url} target="_blank" rel="noreferrer" className="text-accent-warm hover:underline">
            {item.license}
          </a>
        </div>
        {item.territory && (
          <div className="flex items-start gap-1 text-red-400">
            <AlertTriangle size={12} className="mt-0.5 shrink-0" /> {item.territory}
          </div>
        )}
        <div><span className="font-medium text-text-primary">Max output: </span>{item.max_output}</div>
        <div>
          <span className="font-medium text-text-primary">On the selected hardware: </span>{item.run_here}
        </div>
        <div className="text-[11px] text-text-muted">
          CPU estimate ({item.feasibility.basis}): {item.feasibility.cpu.replace('_', ' ')} — {item.feasibility.why}
          {item.feasibility.estimated_ram_gb ? ` ≈ ${item.feasibility.estimated_ram_gb} GB RAM at BF16.` : ''}
        </div>
      </div>

      <div>
        <div className="mb-1 font-medium text-text-primary">Ways to use it (researched {library.researched_on})</div>
        {item.routes.length === 0 ? (
          <p className="text-text-muted">No hosted access found; local only.</p>
        ) : (
          <ul className="grid gap-1.5">
            {item.routes.map(route => {
              const info = library.routes[route.id]
              return (
                <li key={route.id} className="rounded-lg border border-border bg-bg-tertiary p-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-text-primary">{info.label}</span>
                    <span className={`rounded border px-1.5 text-[10px] ${KIND_STYLE[route.kind] || ''}`}>
                      {library.route_kinds[route.kind]}
                    </span>
                    <span className="text-[10px] text-text-muted">{route.confidence}</span>
                    {!route.same_weights && <span className="text-[10px] text-amber-400">base model, not this fine-tune</span>}
                    <a href={info.source} target="_blank" rel="noreferrer" className="ml-auto text-text-muted hover:text-text-primary">
                      <ExternalLink size={11} />
                    </a>
                  </div>
                  {route.note && <div className="mt-0.5">{route.note}</div>}
                  <div className="mt-0.5 text-[11px] text-text-muted">{info.limits} · Account: {info.account} · Maestro: {info.maestro}</div>
                </li>
              )
            })}
          </ul>
        )}
        <div className="mt-2 text-[11px] text-text-muted">
          Local engines: {item.local_backends.map(id => `${library.local_backends[id]?.label} (${library.local_backends[id]?.hardware})`).join(' · ')}
        </div>
      </div>

      <div className="rounded-lg border border-border p-2">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <span className="font-medium text-text-primary">Download</span>
          <select value={profile} onChange={event => { setProfile(event.target.value); setReport(null) }} className={inputClass}>
            <option value="quality">Best quality (BF16/FP16)</option>
            <option value="compact">Smaller (INT8 / GGUF)</option>
          </select>
          <button type="button" onClick={() => void check()} disabled={busy} className="rounded-lg border border-border px-2.5 py-1.5 hover:bg-bg-hover disabled:opacity-40">
            Check files and sizes
          </button>
          {busy && <Loader2 size={13} className="animate-spin" />}
        </div>
        {report && (
          <div className="mb-2 grid gap-1">
            <div className={report.free_to_download ? 'text-accent-green' : 'text-amber-400'}>
              {report.files_to_download} file(s) to download, {gb(report.bytes_to_download)}.{' '}
              {report.free_to_download ? 'Free to download.' : `Not publicly downloadable: ${report.blocked.slice(0, 3).join(', ')}`}
            </div>
            {report.repos.filter(r => r.message).map(r => <div key={r.repo} className="text-amber-400">{r.repo}: {r.message}</div>)}
            <ul className="max-h-40 overflow-y-auto text-[11px] text-text-muted">
              {report.files.map(file => (
                <li key={file.target || file.url}>
                  {file.present ? '✓ installed' : file.availability} · {file.size ? gb(file.size) : '?'} · {file.target} ({file.role})
                </li>
              ))}
            </ul>
            {report.notes.map(note => <div key={note} className="text-[11px] text-text-muted">{note}</div>)}
          </div>
        )}
        {needsAck && (
          <label className="mb-2 flex items-start gap-2 text-text-primary">
            <input type="checkbox" checked={accepted} onChange={event => setAccepted(event.target.checked)} className="mt-0.5" />
            <span>I have read the licence and accept its terms: {item.license_ack_required.join(' · ')}</span>
          </label>
        )}
        <button
          type="button"
          onClick={() => void download()}
          disabled={busy || (needsAck && !accepted)}
          className="inline-flex items-center gap-1 rounded-lg bg-accent-warm px-3 py-1.5 font-semibold text-black disabled:opacity-40"
        >
          <Download size={13} /> Download to Maestro
        </button>
        {error && <p className="mt-2 text-red-400">{error}</p>}
      </div>
    </div>
  )
}

export function ModelLibrary() {
  const [accelerator, setAccelerator] = useState<'' | Accelerator>('')
  const [library, setLibrary] = useState<LibraryResponse | null>(null)
  const [jobs, setJobs] = useState<DownloadJob[]>([])
  const [error, setError] = useState<string | null>(null)
  const [modality, setModality] = useState('all')
  const [query, setQuery] = useState('')
  const [freeOnly, setFreeOnly] = useState(false)
  const [portableOnly, setPortableOnly] = useState(false)
  const [showHidden, setShowHidden] = useState(false)
  const [open, setOpen] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [lib, dl] = await Promise.all([fetchLibrary(accelerator), fetchDownloads()])
      setLibrary(lib)
      setJobs(dl.jobs)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [accelerator])

  useEffect(() => {
    let alive = true
    Promise.all([fetchLibrary(accelerator), fetchDownloads()])
      .then(([lib, dl]) => {
        if (!alive) return
        setLibrary(lib)
        setJobs(dl.jobs)
        setError(null)
      })
      .catch(e => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      alive = false
    }
  }, [accelerator])

  const active = jobs.some(job => ACTIVE.has(job.state))
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => {
      void fetchDownloads().then(result => setJobs(result.jobs)).catch(() => undefined)
    }, 1500)
    return () => clearInterval(timer)
  }, [active])

  const items = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return (library?.items || []).filter(item =>
      (showHidden || item.visible)
      && (modality === 'all' || item.modality === modality)
      && (!freeOnly || item.free_api)
      && (!portableOnly || item.portable_here)
      && (!needle || `${item.name} ${item.id} ${item.family_label}`.toLowerCase().includes(needle)))
  }, [library, modality, query, freeOnly, portableOnly, showHidden])

  const effective = library?.hardware.accelerator || 'cpu'

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
      <section className="rounded-xl border border-border bg-bg-secondary p-4">
        <h2 className="text-sm font-semibold text-text-primary">Model library</h2>
        <p className="mt-1 text-[11px] text-text-muted">
          Every model Maestro ships, how to reach it for free or locally, and downloads that need no GPU: files are verified
          and placed where Maestro loads them, so they also work after copying the folder to a computer with a GPU.
          Downloading never needs NVIDIA; running video models does need a capable GPU somewhere (this computer, one of
          yours, free public compute or an API).
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          <label className="flex items-center gap-1 text-text-secondary">
            Plan for
            <select value={accelerator} onChange={event => setAccelerator(event.target.value as '' | Accelerator)} className={inputClass}>
              {ACCELERATORS.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
            </select>
          </label>
          {library && <span className="text-text-muted">detected: {library.hardware.accelerator} ({library.hardware.basis})</span>}
          <select value={modality} onChange={event => setModality(event.target.value)} className={inputClass}>
            <option value="all">All modalities</option>
            <option value="video">Video</option>
            <option value="image">Image</option>
            <option value="audio">Audio</option>
            <option value="text">Text</option>
          </select>
          <input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search models" className={`${inputClass} min-w-40 flex-1`} />
          <label className="flex items-center gap-1 text-text-secondary"><input type="checkbox" checked={freeOnly} onChange={e => setFreeOnly(e.target.checked)} /> Free API</label>
          <label className="flex items-center gap-1 text-text-secondary"><input type="checkbox" checked={portableOnly} onChange={e => setPortableOnly(e.target.checked)} /> Runs on {effective}</label>
          <label className="flex items-center gap-1 text-text-secondary"><input type="checkbox" checked={showHidden} onChange={e => setShowHidden(e.target.checked)} /> Hidden models</label>
          <button type="button" onClick={() => void load()} className="rounded-md p-1.5 text-text-muted hover:bg-bg-hover" title="Refresh"><RefreshCw size={13} /></button>
        </div>
        {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
      </section>

      {jobs.length > 0 && (
        <section className="rounded-xl border border-border bg-bg-secondary p-3">
          <h3 className="mb-2 text-xs font-semibold text-text-primary">Downloads</h3>
          <ul className="grid gap-2">
            {jobs.slice(0, 8).map(job => {
              const pct = job.bytes_total ? Math.min(100, (100 * job.bytes_done) / job.bytes_total) : 0
              return (
                <li key={job.id} className="text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-text-primary">{job.name}</span>
                    <span className={job.state === 'failed' ? 'text-red-400' : job.state === 'succeeded' ? 'text-accent-green' : 'text-amber-400'}>{job.state}</span>
                    <span className="text-text-muted">{gb(job.bytes_done)} / {gb(job.bytes_total)} · {job.files_done}/{job.files_total} files</span>
                    {ACTIVE.has(job.state) && (
                      <button type="button" onClick={() => void cancelDownload(job.id).then(() => load())} className="ml-auto inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 hover:bg-bg-hover">
                        <X size={11} /> Cancel
                      </button>
                    )}
                  </div>
                  {ACTIVE.has(job.state) && (
                    <div className="mt-1 h-1.5 overflow-hidden rounded bg-bg-tertiary"><div className="h-full bg-accent-warm" style={{ width: `${pct}%` }} /></div>
                  )}
                  <div className="text-[11px] text-text-muted">{job.error ? `${job.error.message} [${job.error.code}]` : job.current}</div>
                </li>
              )
            })}
          </ul>
        </section>
      )}

      <div className="text-[11px] text-text-muted">{items.length} models</div>
      <ul className="grid gap-2">
        {items.map(item => {
          const expanded = open === item.id
          const free = item.routes.filter(r => r.kind === 'free_recurring' && r.same_weights && r.confidence !== 'unverified')
          return (
            <li key={item.id} className="rounded-xl border border-border bg-bg-secondary p-3">
              <button type="button" onClick={() => setOpen(expanded ? null : item.id)} className="flex w-full flex-wrap items-center gap-2 text-left">
                {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                <span className="text-sm font-medium text-text-primary">{item.name}</span>
                <span className="rounded bg-bg-tertiary px-1.5 text-[10px] uppercase text-text-muted">{item.modality}</span>
                {free.map(route => (
                  <span key={route.id} className={`rounded border px-1.5 text-[10px] ${KIND_STYLE.free_recurring}`}>free: {library?.routes[route.id]?.label}</span>
                ))}
                {item.routes.some(r => r.kind === 'public_space') && <span className={`rounded border px-1.5 text-[10px] ${KIND_STYLE.public_space}`}>public Space</span>}
                {!item.portable_here && <span className="rounded border border-red-500/40 px-1.5 text-[10px] text-red-400">NVIDIA-only weights</span>}
                {item.territory && <span className="rounded border border-red-500/40 px-1.5 text-[10px] text-red-400">territory restricted</span>}
                <span className="ml-auto text-[11px] text-text-muted">{item.family_label.split('(')[0]}</span>
              </button>
              {expanded && library && (
                <ItemDetails item={item} library={library} accelerator={accelerator || effective} onStarted={job => setJobs(previous => [job, ...previous])} />
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
