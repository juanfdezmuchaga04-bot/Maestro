import { useEffect, useState } from 'react'
import { Clapperboard, ImagePlus, Loader2, Play, Square, Trash2 } from 'lucide-react'
import { uploadImage } from '../api/client'

type Role = 'reference' | 'character' | 'location'

interface Picked {
  path: string
  url: string
  name: string
  role: Role
}

interface Production {
  id: string
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  stage: string
  created_at: number
  request: { description: string; resolution: string; duration_seconds: number }
  recipe: { render_size: string; in_engine_upscale: string; temporal_upsampling: string; finish_resolution: string }
  progress: { percent: number | null; message: string; phase?: string }
  error: string
  result: string
  result_name: string
  log: Array<{ at: number; text: string }>
}

interface Compute {
  local_vram_gb?: number
  nodes?: Array<{ peer_id: string; label: string; online: boolean; vram_gb: number }>
  error?: string
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail || res.status))
  }
  return res.json() as Promise<T>
}

const STAGES: Record<string, string> = {
  directing: 'Director: planning, images and clips',
  finishing: 'SeedVR2 4K finishing',
  done: 'Done',
}

export function AutoProduction() {
  const [description, setDescription] = useState('')
  const [style, setStyle] = useState('')
  const [duration, setDuration] = useState(30)
  const [resolution, setResolution] = useState('1080p')
  const [aspect, setAspect] = useState('16:9')
  const [smooth, setSmooth] = useState(false)
  const [images, setImages] = useState<Picked[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [productions, setProductions] = useState<Production[]>([])
  const [compute, setCompute] = useState<Compute | null>(null)
  const [tick, setTick] = useState(0)

  const running = productions.some(p => p.status === 'running')

  useEffect(() => {
    let alive = true
    call<{ productions: Production[]; compute: Compute }>('/api/v1/auto-production')
      .then(data => {
        if (!alive) return
        setProductions(data.productions)
        setCompute(data.compute)
      })
      .catch(err => alive && setError(err instanceof Error ? err.message : String(err)))
    return () => {
      alive = false
    }
  }, [tick])

  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => setTick(value => value + 1), 4000)
    return () => window.clearInterval(timer)
  }, [running])

  const addImages = async (files: FileList | null) => {
    if (!files) return
    setError('')
    try {
      const uploaded = await Promise.all(Array.from(files).map(file => uploadImage(file, { reuseIdentical: true })
        .then(result => ({ path: result.path, url: result.url, name: file.name }))))
      setImages(current => [
        ...current,
        ...uploaded.map((item, index) => ({
          ...item,
          role: (current.length === 0 && index === 0 ? 'reference' : 'character') as Role,
        })),
      ])
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const setRole = (index: number, role: Role) => setImages(current => current.map((image, i) => {
    if (i === index) return { ...image, role }
    // Only one main reference: the previous one becomes a character reference.
    return role === 'reference' && image.role === 'reference' ? { ...image, role: 'character' } : image
  }))

  const start = async () => {
    setBusy(true)
    setError('')
    try {
      await call<Production>('/api/v1/auto-production', {
        method: 'POST',
        body: JSON.stringify({
          description, style, duration_seconds: duration, resolution, aspect_ratio: aspect, smooth_motion: smooth,
          images: images.map(image => ({ path: image.path, role: image.role, label: image.name.replace(/\.[^.]+$/, '') })),
        }),
      })
      setTick(value => value + 1)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const cancel = async (id: string) => {
    try {
      await call(`/api/v1/auto-production/${id}/cancel`, { method: 'POST' })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
    setTick(value => value + 1)
  }

  const nodes = (compute?.nodes || []).filter(node => node.online)
  const gpuText = compute?.error
    ? compute.error
    : (compute?.local_vram_gb || 0) > 0
      ? `This computer's NVIDIA GPU${nodes.length ? ` + ${nodes.length} node(s)` : ''}`
      : nodes.length
        ? nodes.map(node => `${node.label}${node.vram_gb ? ` (${node.vram_gb} GB)` : ''}`).join(' · ')
        : 'No NVIDIA GPU: connect a free node in the "Free NVIDIA GPU" tab first.'

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4 text-xs text-text-secondary">
      <section className="rounded-xl border border-border bg-bg-secondary p-4">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-text-primary"><Clapperboard size={15} /> Automatic production</h2>
        <p className="mt-1 text-[11px] text-text-muted">
          Describe the piece and add images. The Director plans scenes and shots, generates the images, renders every clip
          on your NVIDIA GPU or connected nodes, upscales (FlashVSR, and SeedVR2 for 4K), and joins the final video — no
          step-by-step review. You can open the project in the Director afterwards to change any shot.
        </p>
        <p className={`mt-2 ${nodes.length || (compute?.local_vram_gb || 0) > 0 ? 'text-accent-green' : 'text-amber-400'}`}>GPU: {gpuText}</p>

        <textarea
          value={description}
          onChange={event => setDescription(event.target.value)}
          rows={5}
          placeholder="What happens, who appears, where, the mood… e.g. A lighthouse keeper finds a message in a bottle during a storm and…"
          className="mt-3 w-full rounded-lg border border-border bg-bg-tertiary px-3 py-2 text-xs text-text-primary focus:border-accent-warm focus:outline-none"
        />
        <input
          value={style}
          onChange={event => setStyle(event.target.value)}
          placeholder="Visual style (optional): 35mm film, warm light, anamorphic…"
          className="mt-2 w-full rounded-lg border border-border bg-bg-tertiary px-3 py-1.5 text-xs text-text-primary focus:border-accent-warm focus:outline-none"
        />

        <div className="mt-3 flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-1">Duration
            <input type="number" min={5} max={600} value={duration} onChange={event => setDuration(Number(event.target.value))}
              className="w-16 rounded border border-border bg-bg-tertiary px-1 py-0.5 text-text-primary" /> s
          </label>
          <label className="flex items-center gap-1">Resolution
            <select value={resolution} onChange={event => setResolution(event.target.value)} className="rounded border border-border bg-bg-tertiary px-1 py-0.5 text-text-primary">
              <option value="720p">720p</option>
              <option value="1080p">1080p (FlashVSR)</option>
              <option value="1440p">1440p (FlashVSR)</option>
              <option value="4k">4K (FlashVSR + SeedVR2)</option>
            </select>
          </label>
          <label className="flex items-center gap-1">Format
            <select value={aspect} onChange={event => setAspect(event.target.value)} className="rounded border border-border bg-bg-tertiary px-1 py-0.5 text-text-primary">
              <option value="16:9">16:9</option>
              <option value="9:16">9:16</option>
              <option value="1:1">1:1</option>
            </select>
          </label>
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={smooth} onChange={event => setSmooth(event.target.checked)} /> Smooth motion (RIFE ×2 frames)
          </label>
        </div>

        <div className="mt-3">
          <label className="inline-flex cursor-pointer items-center gap-1 rounded-lg border border-border px-2 py-1 hover:bg-bg-hover">
            <ImagePlus size={12} /> Add images
            <input type="file" accept="image/*" multiple className="hidden" onChange={event => void addImages(event.target.files)} />
          </label>
          {images.length > 0 && (
            <ul className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
              {images.map((image, index) => (
                <li key={image.path} className="rounded-lg border border-border bg-bg-tertiary p-1">
                  <img src={image.url} alt={image.name} className="h-20 w-full rounded object-cover" />
                  <div className="mt-1 flex items-center gap-1">
                    <select value={image.role} onChange={event => setRole(index, event.target.value as Role)} className="min-w-0 flex-1 rounded border border-border bg-bg-secondary px-1 text-[10px] text-text-primary">
                      <option value="reference">Main reference</option>
                      <option value="character">Character</option>
                      <option value="location">Location</option>
                    </select>
                    <button type="button" title="Remove" onClick={() => setImages(current => current.filter((_, i) => i !== index))} className="rounded p-0.5 text-text-muted hover:text-red-400">
                      <Trash2 size={11} />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <button
          type="button"
          onClick={() => void start()}
          disabled={busy || description.trim().length < 10}
          className="mt-3 inline-flex items-center gap-1 rounded-lg bg-accent-warm px-3 py-1.5 font-semibold text-black disabled:opacity-40"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />} Produce
        </button>
        {error && <p className="mt-2 text-red-400">{error}</p>}
      </section>

      {productions.length > 0 && (
        <section className="rounded-xl border border-border bg-bg-secondary p-4">
          <h3 className="mb-2 text-xs font-semibold text-text-primary">Productions</h3>
          <ul className="grid gap-2">
            {productions.map(production => (
              <li key={production.id} className="rounded-lg border border-border bg-bg-tertiary p-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-text-primary">{production.request.description.slice(0, 80)}</span>
                  <span className="text-[10px] text-text-muted">{production.request.resolution} · {production.request.duration_seconds}s</span>
                  <span className={`ml-auto ${production.status === 'completed' ? 'text-accent-green' : production.status === 'failed' ? 'text-red-400' : production.status === 'running' ? 'text-accent-warm' : 'text-text-muted'}`}>
                    {production.status === 'running' ? (STAGES[production.stage] || production.stage) : production.status}
                  </span>
                  {production.status === 'running' && (
                    <button type="button" onClick={() => void cancel(production.id)} className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 hover:bg-bg-hover">
                      <Square size={10} /> Cancel
                    </button>
                  )}
                </div>
                {production.status === 'running' && (
                  <div className="mt-1 text-[11px]">
                    {production.progress.percent != null && <span>{production.progress.percent}% · </span>}
                    {production.progress.phase && <span>{production.progress.phase} · </span>}
                    {production.progress.message}
                  </div>
                )}
                {production.error && <p className="mt-1 text-red-400">{production.error}</p>}
                {production.status === 'completed' && (
                  <video controls src={`/api/v1/auto-production/${production.id}/video`} className="mt-2 max-h-72 w-full rounded bg-black" />
                )}
                {production.result_name && <p className="mt-1 text-[10px] text-text-muted">{production.result_name}</p>}
                <details className="mt-1">
                  <summary className="cursor-pointer text-[10px] text-text-muted">Log</summary>
                  <ul className="mt-1 space-y-0.5 text-[10px] text-text-muted">
                    {production.log.map(entry => <li key={entry.at}>{new Date(entry.at * 1000).toLocaleTimeString()} — {entry.text}</li>)}
                  </ul>
                </details>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
