import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Film, Loader2, Music, Scissors, Volume2 } from 'lucide-react'
import { useFilmStore } from '../stores/useFilmStore'

const inputClass =
  'w-full rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none'

function ReadinessList({ issues }: { issues: Array<{ code: string; severity: string; message: string }> }) {
  if (issues.length === 0) {
    return <p className="text-[11px] text-accent-green">Ready to assemble.</p>
  }
  return (
    <ul className="space-y-1 text-[11px]">
      {issues.map((issue, index) => (
        <li
          key={`${issue.code}-${index}`}
          className={issue.severity === 'blocking' ? 'text-chip-red' : 'text-indicator-warning'}
        >
          {issue.message}
        </li>
      ))}
    </ul>
  )
}

export function StudioFinishingPanel() {
  const overview = useFilmStore(state => state.overview)
  const assembly = useFilmStore(state => state.assembly)
  const refreshAssembly = useFilmStore(state => state.refreshAssembly)
  const planSoundtrack = useFilmStore(state => state.planSoundtrack)
  const syncCutsToMusic = useFilmStore(state => state.syncCutsToMusic)
  const syncProduction = useFilmStore(state => state.syncProduction)
  const cancelJob = useFilmStore(state => state.cancelJob)
  const assemble = useFilmStore(state => state.assemble)
  const exportFilm = useFilmStore(state => state.exportFilm)
  const busy = useFilmStore(state => state.busy)
  const engine = useFilmStore(state => state.engine)

  const [includeSubtitles, setIncludeSubtitles] = useState(false)
  const [includeTitles, setIncludeTitles] = useState(true)
  const [credits, setCredits] = useState('')
  const [onlyApproved, setOnlyApproved] = useState(false)
  const [queueMode, setQueueMode] = useState<'now' | 'held'>('now')

  useEffect(() => {
    void refreshAssembly()
  }, [refreshAssembly, overview?.project.id])

  const project = overview?.project
  const tracks = overview?.soundtrack.tracks || []
  const sync = overview?.music_sync
  const report = assembly?.report

  const usedAssets = useMemo(() => {
    const map = new Map<string, string>()
    for (const [id, asset] of Object.entries(project?.assets || {})) {
      map.set(id, asset.name)
    }
    return map
  }, [project])

  if (!project) return null

  return (
    <div className="grid gap-4 p-4 xl:grid-cols-2">
      <section className="space-y-3">
        <header className="flex items-center gap-2">
          <Music size={15} className="text-accent-warm" />
          <h2 className="text-sm font-semibold text-text-primary">7. Soundtrack & sound design</h2>
        </header>
        <p className="text-xs text-text-muted">
          Your music is kept as its own track and is never replaced or muted automatically. Each
          lane is exported separately and mixed at the levels below.
        </p>

        <button
          type="button"
          onClick={() => void planSoundtrack()}
          disabled={Boolean(busy)}
          className="inline-flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-xs text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
        >
          <Volume2 size={13} /> Build the mix lanes from my files
        </button>

        {tracks.length > 0 && (
          <ul className="space-y-2">
            {tracks.map(track => (
              <li key={`${track.target}-${track.asset_id}`} className="rounded-xl border border-border bg-bg-secondary p-3">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="rounded bg-bg-tertiary px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-text-secondary">
                    {track.target}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-text-primary">
                    {track.label || usedAssets.get(track.asset_id) || track.asset_id}
                  </span>
                  {track.preserve && (
                    <span className="text-[10px] text-accent-green" title="Your own audio is preserved">
                      preserved
                    </span>
                  )}
                </div>
                <div className="mt-1.5 flex flex-wrap gap-3 text-[11px] text-text-muted">
                  <span>gain {track.gain_db.toFixed(1)} dB</span>
                  <span>fade in {track.fade_in_sec.toFixed(1)}s</span>
                  <span>fade out {track.fade_out_sec.toFixed(1)}s</span>
                  <span>{track.muted ? 'muted' : 'audible'}</span>
                </div>
              </li>
            ))}
          </ul>
        )}

        {overview?.soundtrack.warnings.map((warning, index) => (
          <p key={index} className="text-[11px] text-indicator-warning">
            {warning}
          </p>
        ))}

        <div className="rounded-xl border border-border bg-bg-secondary p-3">
          <div className="flex items-center gap-2">
            <Scissors size={13} className="text-accent-warm" />
            <span className="text-xs font-semibold text-text-primary">Cut to the music</span>
          </div>
          {sync?.available ? (
            <>
              <p className="mt-1 text-[11px] text-text-secondary">
                {sync.bpm ? `${Math.round(sync.bpm)} BPM · ` : ''}
                {sync.boundaries.filter(boundary => boundary.nearest_beat !== null).length} shot
                boundary/boundaries can be aligned to a beat.
              </p>
              <div className="mt-2 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => void syncCutsToMusic(false)}
                  className="rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
                >
                  Preview alignment
                </button>
                <button
                  type="button"
                  onClick={() => void syncCutsToMusic(true)}
                  className="rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
                >
                  Apply to the storyboard
                </button>
              </div>
            </>
          ) : (
            <p className="mt-1 text-[11px] text-text-muted">
              {sync?.reason || 'Analyse the music to align cuts to it.'}
            </p>
          )}
        </div>
      </section>

      <section className="space-y-3">
        <header className="flex items-center gap-2">
          <Film size={15} className="text-accent-warm" />
          <h2 className="text-sm font-semibold text-text-primary">8-10. Assemble, mix and export</h2>
        </header>

        <div className="rounded-xl border border-border bg-bg-secondary p-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-text-muted">
            Assembly readiness
          </div>
          <div className="mt-2">
            <ReadinessList issues={assembly?.readiness || []} />
          </div>
          {report && (
            <ul className="mt-2 space-y-0.5 text-[11px] text-text-secondary">
              <li>
                {report.shots_in_timeline} shot(s) in the cut · {report.duration_sec.toFixed(1)}s ·{' '}
                {report.canvas.width}×{report.canvas.height} @ {report.canvas.fps}fps
              </li>
              <li>Audio lanes: {report.audio_tracks.length ? report.audio_tracks.join(', ') : 'none'}</li>
              {report.shots_missing.length > 0 && (
                <li className="text-indicator-warning">
                  Left out: {report.shots_missing.length} shot(s) without a rendered file
                </li>
              )}
              {!report.music_sync_available && (
                <li className="text-text-muted">Music sync: {report.music_sync_reason}</li>
              )}
            </ul>
          )}
        </div>

        <div className="grid gap-2 rounded-xl border border-border bg-bg-secondary p-3 sm:grid-cols-2">
          <label className="flex items-center gap-2 text-[11px] text-text-secondary">
            <input type="checkbox" checked={includeTitles} onChange={event => setIncludeTitles(event.target.checked)} />
            Title and credit cards
          </label>
          <label className="flex items-center gap-2 text-[11px] text-text-secondary">
            <input
              type="checkbox"
              checked={includeSubtitles}
              onChange={event => setIncludeSubtitles(event.target.checked)}
            />
            Burn dialogue as subtitles
          </label>
          <label className="flex items-center gap-2 text-[11px] text-text-secondary">
            <input
              type="checkbox"
              checked={onlyApproved}
              onChange={event => setOnlyApproved(event.target.checked)}
            />
            Only approved takes
          </label>
          <label className="block text-[11px] text-text-secondary">
            Render queue
            <select
              value={queueMode}
              onChange={event => setQueueMode(event.target.value as 'now' | 'held')}
              className={inputClass}
            >
              <option value="now">Start immediately</option>
              <option value="held">Hold for the queue</option>
            </select>
          </label>
          <label className="block sm:col-span-2">
            <span className="text-[11px] text-text-secondary">Credits text (optional)</span>
            <input
              value={credits}
              onChange={event => setCredits(event.target.value)}
              placeholder="Produced with Maestro"
              className={inputClass}
            />
          </label>
        </div>

        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() =>
              void assemble({ includeSubtitles, includeTitles, creditsText: credits, onlyApproved })
            }
            disabled={Boolean(busy)}
            className="inline-flex items-center gap-2 rounded-lg bg-accent-warm px-3 py-2 text-xs font-medium text-black disabled:opacity-40"
          >
            {busy ? <Loader2 size={13} className="animate-spin" /> : <Scissors size={13} />}
            Assemble the cut in the Editor
          </button>
          <button
            type="button"
            onClick={() =>
              void exportFilm({
                assemble: true,
                queueMode,
                includeSubtitles,
                includeTitles,
                creditsText: credits,
                onlyApproved,
              })
            }
            disabled={Boolean(busy) || !engine?.ffmpeg}
            title={engine?.ffmpeg ? 'Assemble and render an MP4 with FFmpeg' : 'FFmpeg is not installed'}
            className="inline-flex items-center gap-2 rounded-lg bg-accent-blue px-3 py-2 text-xs font-medium text-white disabled:opacity-40"
          >
            <Film size={13} /> Export MP4
          </button>
        </div>

        {!engine?.ffmpeg && (
          <p className="flex items-start gap-1.5 text-[11px] text-indicator-warning">
            <AlertTriangle size={12} className="mt-0.5" /> FFmpeg is not available in this process,
            so the final render cannot run here. Assembly still produces an editable Editor project.
          </p>
        )}

        {project.exports.length > 0 && (
          <div className="rounded-xl border border-border bg-bg-secondary p-3">
            <div className="flex items-center gap-2">
              <span className="text-[11px] font-medium uppercase tracking-wide text-text-muted">
                Exports
              </span>
              {(project.exports.some(entry => entry.status === 'queued' || entry.status === 'running')) && (
                <button
                  type="button"
                  onClick={() => void syncProduction()}
                  className="rounded-md border border-border px-2 py-0.5 text-[10px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
                >
                  Refresh render state
                </button>
              )}
            </div>
            <ul className="mt-2 space-y-2 text-[11px] text-text-secondary">
              {project.exports.map((entry, index) => {
                const pending = entry.status === 'queued' || entry.status === 'running'
                return (
                  <li key={entry.job_id || index} className="flex flex-wrap items-center gap-2">
                    <span
                      className={
                        entry.status === 'completed'
                          ? 'text-accent-green'
                          : entry.status === 'failed'
                            ? 'text-chip-red'
                            : 'text-text-muted'
                      }
                    >
                      {entry.status}
                    </span>
                    <span className="text-text-primary">
                      {entry.filename || (pending ? `rendering (job ${entry.job_id})` : 'no file')}
                    </span>
                    <span className="text-text-muted">
                      {entry.created_at ? new Date(entry.created_at * 1000).toLocaleString() : '—'}
                    </span>
                    {entry.error && <span className="text-chip-red">{entry.error}</span>}
                    {pending && entry.job_id && (
                      <button
                        type="button"
                        onClick={() => void cancelJob(entry.job_id)}
                        disabled={Boolean(busy)}
                        className="rounded-md border border-border px-2 py-0.5 text-[10px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
                      >
                        Cancel render
                      </button>
                    )}
                  </li>
                )
              })}
            </ul>
          </div>
        )}
      </section>
    </div>
  )
}
