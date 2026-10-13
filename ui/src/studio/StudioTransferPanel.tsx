import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  FileUp,
  Globe,
  Link2,
  Loader2,
  PauseCircle,
  RotateCcw,
  X,
} from 'lucide-react'
import { fetchUploadLimits, importReferenceUrl, validateReferenceUrl } from '../api/engines'
import type { UploadLimits, UploadedReference } from '../types/engines'
import { useFilmStore } from '../stores/useFilmStore'
import { useChunkedUpload } from './useChunkedUpload'

const MB = 1024 * 1024

function formatBytes(bytes: number): string {
  if (!bytes) return '0 MB'
  if (bytes >= 1024 * MB) return `${(bytes / (1024 * MB)).toFixed(2)} GB`
  return `${(bytes / MB).toFixed(1)} MB`
}

/** Large files arrive in chunks, and a cancelled transfer can be resumed. */
function ChunkedUploadCard({ limits }: { limits: UploadLimits | null }) {
  const upload = useChunkedUpload()
  const activeProjectId = useFilmStore(state => state.activeProjectId)
  const refreshOverview = useFilmStore(state => state.refreshOverview)
  const inputRef = useRef<HTMLInputElement>(null)
  const [lastFile, setLastFile] = useState<File | null>(null)
  const [sessionToResume, setSessionToResume] = useState('')

  const percent =
    upload.totalBytes > 0 ? Math.round((upload.sentBytes / upload.totalBytes) * 100) : 0

  return (
    <section className="rounded-xl border border-border bg-bg-secondary p-4">
      <div className="flex items-center gap-2">
        <FileUp size={15} className="text-accent-warm" />
        <h2 className="text-sm font-semibold text-text-primary">Large files</h2>
      </div>
      <p className="mt-1 text-xs text-text-muted">
        Sent in {limits ? formatBytes(limits.chunk_size) : '8 MB'} pieces and assembled on disk, so a
        long video never has to fit in memory. Interrupting it does not lose the parts already sent.
      </p>

      <input
        ref={inputRef}
        type="file"
        className="hidden"
        onChange={event => {
          const file = event.target.files?.[0]
          if (!file) return
          setLastFile(file)
          void upload.run(file)
          event.target.value = ''
        }}
      />

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={upload.status === 'uploading' || upload.status === 'assembling'}
          className="flex-1 rounded-lg bg-accent-warm px-3 py-2.5 text-xs font-medium text-black disabled:opacity-40 sm:flex-none"
        >
          Choose a file
        </button>
        {(upload.status === 'uploading' || upload.status === 'assembling') && (
          <button
            type="button"
            onClick={() => void upload.cancel()}
            className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-2.5 text-xs text-text-secondary hover:bg-bg-hover hover:text-text-primary"
          >
            <PauseCircle size={13} /> Cancel
          </button>
        )}
        {upload.status === 'cancelled' && lastFile && (
          <button
            type="button"
            onClick={() => void upload.resume(lastFile, upload.sessionId || '')}
            className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-2.5 text-xs text-text-secondary hover:bg-bg-hover hover:text-text-primary"
          >
            <RotateCcw size={13} /> Resume
          </button>
        )}
      </div>

      {upload.status === 'preparing' && (
        <p className="mt-3 flex items-center gap-2 text-xs text-text-secondary">
          <Loader2 size={13} className="animate-spin" /> Opening the session…
        </p>
      )}
      {upload.status === 'uploading' && (
        <div className="mt-3">
          <div className="h-1.5 overflow-hidden rounded-full bg-bg-tertiary">
            <div className="h-full bg-accent-warm transition-all" style={{ width: `${percent}%` }} />
          </div>
          <p className="mt-1.5 text-[11px] text-text-muted">
            {upload.filename} — chunk {upload.chunkIndex} of {upload.chunkCount} ·{' '}
            {formatBytes(upload.sentBytes)} of {formatBytes(upload.totalBytes)}
          </p>
        </div>
      )}
      {upload.status === 'assembling' && (
        <p className="mt-3 flex items-center gap-2 text-xs text-text-secondary">
          <Loader2 size={13} className="animate-spin" /> Verifying and assembling…
        </p>
      )}
      {upload.status === 'done' && upload.result && (
        <div className="mt-3 rounded-lg border border-accent-green/40 bg-accent-green/10 p-3 text-xs text-text-primary">
          <div className="flex items-center gap-1.5 font-medium">
            <CheckCircle2 size={13} className="text-accent-green" /> {upload.result.final_filename}
          </div>
          <p className="mt-1 text-text-secondary">
            {formatBytes(upload.result.received_bytes)} · SHA-256 {upload.result.sha256?.slice(0, 12)}…
            {upload.result.deduplicated ? ' · identical file already stored, reused' : ''}
          </p>
          {activeProjectId && (
            <button
              type="button"
              onClick={() => void refreshOverview()}
              className="mt-2 rounded-md border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
            >
              Refresh the production
            </button>
          )}
        </div>
      )}
      {upload.status === 'error' && (
        <div className="mt-3 flex items-start gap-2 rounded-lg border border-chip-red/40 bg-chip-red/10 p-3 text-xs text-text-primary">
          <AlertTriangle size={13} className="mt-0.5 shrink-0 text-chip-red" />
          <span className="flex-1">{upload.error}</span>
        </div>
      )}

      <details className="mt-3 rounded-lg border border-border bg-bg-tertiary p-3">
        <summary className="cursor-pointer text-[11px] text-text-secondary">
          Resume a transfer by session id
        </summary>
        <div className="mt-2 flex gap-2">
          <input
            value={sessionToResume}
            onChange={event => setSessionToResume(event.target.value)}
            placeholder="session id"
            className="min-w-0 flex-1 rounded-lg border border-border bg-bg-secondary px-2.5 py-2 text-xs text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none"
          />
          <button
            type="button"
            disabled={!sessionToResume || !lastFile}
            onClick={() => void upload.resume(lastFile as File, sessionToResume)}
            className="rounded-lg border border-border px-3 py-2 text-xs text-text-secondary disabled:opacity-40"
          >
            Resume
          </button>
        </div>
        {!lastFile && (
          <p className="mt-1.5 text-[10px] text-text-muted">
            Choose the same file first: only the missing chunks are re-sent.
          </p>
        )}
      </details>
    </section>
  )
}

/** Import a public reference URL, with the SSRF guard's verdict shown up front. */
function ReferenceUrlCard() {
  const activeProjectId = useFilmStore(state => state.activeProjectId)
  const refreshOverview = useFilmStore(state => state.refreshOverview)
  const [url, setUrl] = useState('')
  const [role, setRole] = useState('reference_image')
  const [checking, setChecking] = useState(false)
  const [importing, setImporting] = useState(false)
  const [verdict, setVerdict] = useState<{ ok: boolean; message: string } | null>(null)
  const [imported, setImported] = useState<UploadedReference | null>(null)
  const [error, setError] = useState('')

  const check = useCallback(async () => {
    setChecking(true)
    setError('')
    setImported(null)
    try {
      const result = await validateReferenceUrl(url)
      setVerdict({
        ok: true,
        message: `Allowed — ${result.host}:${result.port} (${result.addresses.join(', ')})`,
      })
    } catch (err) {
      setVerdict({ ok: false, message: (err as Error).message })
    } finally {
      setChecking(false)
    }
  }, [url])

  const runImport = useCallback(async () => {
    setImporting(true)
    setError('')
    try {
      const result = await importReferenceUrl({
        url,
        projectId: activeProjectId || undefined,
        role: activeProjectId ? role : undefined,
        analyze: true,
      })
      setImported(result.reference)
      if (activeProjectId) await refreshOverview()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setImporting(false)
    }
  }, [url, role, activeProjectId, refreshOverview])

  return (
    <section className="rounded-xl border border-border bg-bg-secondary p-4">
      <div className="flex items-center gap-2">
        <Globe size={15} className="text-accent-warm" />
        <h2 className="text-sm font-semibold text-text-primary">Reference from a URL</h2>
      </div>
      <p className="mt-1 text-xs text-text-muted">
        Only a direct link to a file. Loopback, private and cloud-metadata addresses are refused, and
        platforms that gate their media behind a login are reported rather than bypassed.
      </p>

      <input
        value={url}
        onChange={event => {
          setUrl(event.target.value)
          setVerdict(null)
        }}
        inputMode="url"
        placeholder="https://example.com/still.png"
        className="mt-3 w-full rounded-lg border border-border bg-bg-tertiary px-3 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none"
      />

      {activeProjectId && (
        <label className="mt-2 block text-[11px] text-text-muted">
          Add to this production as
          <select
            value={role}
            onChange={event => setRole(event.target.value)}
            className="mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-2.5 py-2 text-xs text-text-primary focus:border-accent-warm focus:outline-none"
          >
            <option value="reference_image">Reference image</option>
            <option value="character_reference">Character reference</option>
            <option value="location_reference">Location reference</option>
            <option value="source_video_edit">Video to edit</option>
            <option value="soundtrack">Soundtrack</option>
            <option value="script_document">Script document</option>
          </select>
        </label>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          disabled={!url || checking}
          onClick={() => void check()}
          className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-2.5 text-xs text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
        >
          {checking ? <Loader2 size={13} className="animate-spin" /> : <Link2 size={13} />} Check
        </button>
        <button
          type="button"
          disabled={!url || importing}
          onClick={() => void runImport()}
          className="flex-1 rounded-lg bg-accent-warm px-3 py-2.5 text-xs font-medium text-black disabled:opacity-40 sm:flex-none"
        >
          {importing ? 'Importing…' : 'Import'}
        </button>
      </div>

      {verdict && (
        <p
          className={`mt-2.5 flex items-start gap-2 rounded-lg border p-2.5 text-[11px] ${
            verdict.ok
              ? 'border-accent-green/40 bg-accent-green/10 text-text-primary'
              : 'border-chip-red/40 bg-chip-red/10 text-text-primary'
          }`}
        >
          {verdict.ok ? (
            <CheckCircle2 size={12} className="mt-0.5 shrink-0 text-accent-green" />
          ) : (
            <AlertTriangle size={12} className="mt-0.5 shrink-0 text-chip-red" />
          )}
          <span className="flex-1">{verdict.message}</span>
        </p>
      )}
      {error && (
        <p className="mt-2 flex items-start gap-2 rounded-lg border border-chip-red/40 bg-chip-red/10 p-2.5 text-[11px] text-text-primary">
          <X size={12} className="mt-0.5 shrink-0 text-chip-red" />
          <span className="flex-1">{error}</span>
        </p>
      )}
      {imported && (
        <p className="mt-2 rounded-lg border border-border bg-bg-tertiary p-2.5 text-[11px] text-text-secondary">
          Stored {imported.stored_name} ({formatBytes(imported.bytes)}, {imported.kind})
          {imported.deduplicated ? ' — identical content already imported' : ''}
        </p>
      )}
    </section>
  )
}

export function StudioTransferPanel() {
  const [limits, setLimits] = useState<UploadLimits | null>(null)

  useEffect(() => {
    let cancelled = false
    void fetchUploadLimits()
      .then(value => {
        if (!cancelled) setLimits(value)
      })
      .catch(() => {
        /* The panel still works; limits are informational. */
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="grid gap-4 p-4 lg:grid-cols-2">
      <ChunkedUploadCard limits={limits} />
      <ReferenceUrlCard />
      {limits && (
        <p className="text-[11px] leading-relaxed text-text-muted lg:col-span-2">
          Limits per file kind — image {formatBytes(limits.limits.image)}, audio{' '}
          {formatBytes(limits.limits.audio)}, video {formatBytes(limits.limits.video)}, document{' '}
          {formatBytes(limits.limits.document)}. {limits.note}
        </p>
      )}
    </div>
  )
}
