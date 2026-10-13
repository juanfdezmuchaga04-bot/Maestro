import { useMemo, useState } from 'react'
import {
  ArrowDown,
  ArrowUp,
  Check,
  Clapperboard,
  Loader2,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
} from 'lucide-react'
import { useFilmStore } from '../stores/useFilmStore'
import type { FilmJob, FilmProductionRequest, FilmStoryboardEntry } from '../types/film'
import { useSyncedDraft } from './useSyncedDraft'

const SHOT_SIZES = [
  '', 'extreme_wide', 'wide', 'full', 'medium_wide', 'medium',
  'medium_close', 'close', 'extreme_close', 'insert', 'over_the_shoulder',
  'two_shot', 'pov', 'aerial',
]

const MOVEMENTS = [
  '', 'static', 'pan_left', 'pan_right', 'tilt_up', 'tilt_down', 'dolly_in',
  'dolly_out', 'tracking', 'crane', 'handheld', 'steadicam', 'orbit',
  'zoom_in', 'zoom_out', 'drone', 'whip_pan',
]

const TRANSITIONS = ['cut', 'dissolve', 'fade_black', 'fade_white', 'whip', 'match_cut']

const inputClass =
  'w-full rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none'

function ShotCard({
  entry,
  request,
  job,
  total,
  imageAssets,
  onProduce,
  onCancel,
  busy,
}: {
  entry: FilmStoryboardEntry
  request?: FilmProductionRequest
  job?: FilmJob
  total: number
  imageAssets: Array<{ id: string; name: string }>
  onProduce: (shotId: string) => void
  onCancel: (jobId: string) => void
  busy: boolean
}) {
  const editShot = useFilmStore(state => state.editShot)
  const removeShot = useFilmStore(state => state.removeShot)
  const moveShot = useFilmStore(state => state.moveShot)
  const setShotReferences = useFilmStore(state => state.setShotReferences)
  const addReferencesToScene = useFilmStore(state => state.addReferencesToScene)
  const [selection, setSelection] = useState<string[]>(() =>
    [entry.start_image_asset_id, ...entry.reference_asset_ids].filter(Boolean),
  )
  const [startAssetId, setStartAssetId] = useState(entry.start_image_asset_id)
  const referencesKey = `${entry.start_image_asset_id}|${entry.reference_asset_ids.join(',')}`
  const [loadedKey, setLoadedKey] = useState(referencesKey)
  if (loadedKey !== referencesKey) {
    setLoadedKey(referencesKey)
    setSelection([entry.start_image_asset_id, ...entry.reference_asset_ids].filter(Boolean))
    setStartAssetId(entry.start_image_asset_id)
  }
  const [open, setOpen] = useState(false)
  const stored = useMemo(
    () => ({
      objective: entry.objective,
      description: entry.description,
      video_prompt: entry.video_prompt,
      duration_sec: entry.duration_sec,
      shot_size: entry.shot_size,
      camera_movement: entry.camera_movement,
      transition: entry.transition,
      dialogue: entry.dialogue,
    }),
    [entry],
  )
  const [draft, setDraft] = useSyncedDraft(JSON.stringify(stored), () => stored)

  const dirty = useMemo(
    () =>
      draft.objective !== entry.objective ||
      draft.description !== entry.description ||
      draft.video_prompt !== entry.video_prompt ||
      draft.duration_sec !== entry.duration_sec ||
      draft.shot_size !== entry.shot_size ||
      draft.camera_movement !== entry.camera_movement ||
      draft.transition !== entry.transition ||
      draft.dialogue !== entry.dialogue,
    [draft, entry],
  )

  const statusColor =
    entry.status === 'approved'
      ? 'text-accent-green'
      : entry.status === 'failed'
        ? 'text-chip-red'
        : entry.take_count > 0
          ? 'text-accent-blue'
          : 'text-text-muted'

  return (
    <div className="rounded-xl border border-border bg-bg-secondary">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <span className="grid h-6 w-6 place-items-center rounded-md bg-bg-tertiary text-[11px] font-semibold text-text-primary">
          {entry.index + 1}
        </span>
        <span className="min-w-0 flex-1 truncate text-xs text-text-primary">{entry.objective || 'Untitled shot'}</span>
        <span className="text-[10px] text-text-muted">{entry.scene_title}</span>
        <span className="text-[10px] text-text-muted">{entry.duration_sec.toFixed(1)}s</span>
        <span className="text-[10px] text-text-muted">
          {entry.start_time_sec.toFixed(1)}–{entry.end_time_sec.toFixed(1)}s
        </span>
        <span className={`text-[10px] ${statusColor}`}>{entry.status}</span>
        {entry.approved && <Check size={12} className="text-accent-green" />}
        <div className="flex items-center gap-1">
          <button
            type="button"
            disabled={entry.index === 0}
            onClick={() => void moveShot(entry.id, entry.index - 1)}
            className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-text-primary disabled:opacity-30"
            title="Move earlier"
          >
            <ArrowUp size={12} />
          </button>
          <button
            type="button"
            disabled={entry.index >= total - 1}
            onClick={() => void moveShot(entry.id, entry.index + 1)}
            className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-text-primary disabled:opacity-30"
            title="Move later"
          >
            <ArrowDown size={12} />
          </button>
          <button
            type="button"
            onClick={() => void removeShot(entry.id)}
            className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-chip-red"
            title="Remove shot"
          >
            <Trash2 size={12} />
          </button>
        </div>
      </div>

      <div className="space-y-2 p-3">
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-text-muted">
          <span>{entry.shot_size || 'shot size —'}</span>
          <span>·</span>
          <span>{entry.camera_movement || 'camera —'}</span>
          <span>·</span>
          <span>transition: {entry.transition}</span>
          {entry.take_count > 0 && <span>· {entry.take_count} take(s)</span>}
          {entry.output_filename && <span>· {entry.output_filename}</span>}
        </div>

        <p className="line-clamp-3 text-[11px] leading-relaxed whitespace-pre-wrap text-text-secondary">
          {entry.video_prompt || entry.description || 'No prompt yet.'}
        </p>

        {request?.skip && (
          <p className="text-[11px] text-indicator-warning">{request.skip_reason}</p>
        )}

        {entry.start_image_asset_id && (
          <p className="text-[11px] text-text-muted">
            Start frame:{' '}
            <span className="text-text-primary">
              {imageAssets.find(asset => asset.id === entry.start_image_asset_id)?.name ||
                entry.start_image_asset_id}
            </span>
          </p>
        )}
        {entry.reference_asset_ids.length > 0 && (
          <p className="text-[11px] text-text-muted">
            References:{' '}
            <span className="text-text-primary">
              {entry.reference_asset_ids
                .map(id => imageAssets.find(asset => asset.id === id)?.name || id)
                .join(', ')}
            </span>
          </p>
        )}

        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => setOpen(value => !value)}
            className="rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
          >
            {open ? 'Close editor' : 'Edit shot'}
          </button>
          <button
            type="button"
            onClick={() => onProduce(entry.id)}
            disabled={busy || (!entry.video_prompt && !entry.image_prompt)}
            className="inline-flex items-center gap-1.5 rounded-lg bg-accent-blue px-2.5 py-1.5 text-[11px] font-medium text-white disabled:opacity-40"
          >
            {busy ? <Loader2 size={11} className="animate-spin" /> : <Sparkles size={11} />}
            {entry.take_count ? 'Regenerate' : 'Produce'}
          </button>
          {job && (
            <button
              type="button"
              onClick={() => onCancel(job.id)}
              disabled={busy}
              className="rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
            >
              Cancel {job.status} job
            </button>
          )}
        </div>

        {open && (
          <div className="space-y-2 rounded-lg border border-border bg-bg-tertiary p-2">
            <div>
              <span className="text-[10px] uppercase tracking-wide text-text-muted">
                Visual references for this shot
              </span>
              {imageAssets.length === 0 ? (
                <p className="mt-1 text-[11px] text-text-muted">
                  Import photographs or illustrations in the Files step to use them here.
                </p>
              ) : (
                <div className="mt-1 flex flex-wrap gap-1">
                  {imageAssets.map(asset => {
                    const selected = selection.includes(asset.id)
                    const isStart = startAssetId === asset.id
                    return (
                      <button
                        key={asset.id}
                        type="button"
                        onClick={() => {
                          setSelection(current =>
                            current.includes(asset.id)
                              ? current.filter(id => id !== asset.id)
                              : [...current, asset.id],
                          )
                          if (isStart && selected) setStartAssetId('')
                        }}
                        onDoubleClick={() => setStartAssetId(asset.id)}
                        title="Click to include, double-click to use as the start frame"
                        className={`rounded-lg border px-2 py-1 text-[11px] ${
                          isStart
                            ? 'border-accent-warm bg-bg-hover text-text-primary'
                            : selected
                              ? 'border-accent-blue bg-bg-hover text-text-primary'
                              : 'border-border bg-bg-secondary text-text-secondary hover:bg-bg-hover'
                        }`}
                      >
                        {isStart ? '★ ' : ''}
                        {asset.name}
                      </button>
                    )
                  })}
                </div>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => void setShotReferences(entry.id, selection, startAssetId)}
                  className="rounded-lg bg-accent-warm px-2.5 py-1.5 text-[11px] font-medium text-black"
                >
                  Save references
                </button>
                <button
                  type="button"
                  disabled={selection.length === 0}
                  onClick={() => void addReferencesToScene(entry.scene_id, selection)}
                  className="rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
                  title="Add these images to every shot of this scene"
                >
                  Add to every shot of “{entry.scene_title || 'this scene'}”
                </button>
                <span className="text-[10px] text-text-muted">
                  ★ = start frame · inspiration vs literal is set per file in Files
                </span>
              </div>
            </div>

            <label className="block">
              <span className="text-[10px] uppercase tracking-wide text-text-muted">Narrative objective</span>
              <input
                value={draft.objective}
                onChange={event => setDraft(current => ({ ...current, objective: event.target.value }))}
                className={inputClass}
              />
            </label>
            <label className="block">
              <span className="text-[10px] uppercase tracking-wide text-text-muted">Visual description</span>
              <textarea
                rows={2}
                value={draft.description}
                onChange={event => setDraft(current => ({ ...current, description: event.target.value }))}
                className={inputClass}
              />
            </label>
            <label className="block">
              <span className="text-[10px] uppercase tracking-wide text-text-muted">Final video prompt</span>
              <textarea
                rows={4}
                value={draft.video_prompt}
                onChange={event => setDraft(current => ({ ...current, video_prompt: event.target.value }))}
                className={inputClass}
              />
            </label>
            <label className="block">
              <span className="text-[10px] uppercase tracking-wide text-text-muted">Dialogue / narration</span>
              <textarea
                rows={2}
                value={draft.dialogue}
                onChange={event => setDraft(current => ({ ...current, dialogue: event.target.value }))}
                className={inputClass}
              />
            </label>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <label className="block">
                <span className="text-[10px] uppercase tracking-wide text-text-muted">Duration (s)</span>
                <input
                  type="number"
                  min={0.5}
                  max={600}
                  step={0.5}
                  value={draft.duration_sec}
                  onChange={event =>
                    setDraft(current => ({ ...current, duration_sec: Number(event.target.value) }))
                  }
                  className={inputClass}
                />
              </label>
              <label className="block">
                <span className="text-[10px] uppercase tracking-wide text-text-muted">Shot size</span>
                <select
                  value={draft.shot_size}
                  onChange={event => setDraft(current => ({ ...current, shot_size: event.target.value }))}
                  className={inputClass}
                >
                  {SHOT_SIZES.map(size => (
                    <option key={size || 'none'} value={size}>{size || '—'}</option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="text-[10px] uppercase tracking-wide text-text-muted">Camera</span>
                <select
                  value={draft.camera_movement}
                  onChange={event =>
                    setDraft(current => ({ ...current, camera_movement: event.target.value }))
                  }
                  className={inputClass}
                >
                  {MOVEMENTS.map(movement => (
                    <option key={movement || 'none'} value={movement}>{movement || '—'}</option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="text-[10px] uppercase tracking-wide text-text-muted">Transition out</span>
                <select
                  value={draft.transition}
                  onChange={event => setDraft(current => ({ ...current, transition: event.target.value }))}
                  className={inputClass}
                >
                  {TRANSITIONS.map(transition => (
                    <option key={transition} value={transition}>{transition}</option>
                  ))}
                </select>
              </label>
            </div>
            <button
              type="button"
              disabled={!dirty}
              onClick={() => void editShot(entry.id, draft)}
              className="inline-flex items-center gap-1.5 rounded-lg bg-accent-warm px-2.5 py-1.5 text-[11px] font-medium text-black disabled:opacity-40"
            >
              Save shot
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

export function StudioStoryboardPanel() {
  const overview = useFilmStore(state => state.overview)
  const addShot = useFilmStore(state => state.addShot)
  const produce = useFilmStore(state => state.produce)
  const syncProduction = useFilmStore(state => state.syncProduction)
  const applyContinuity = useFilmStore(state => state.applyContinuity)
  const cancelJob = useFilmStore(state => state.cancelJob)
  const refreshOverview = useFilmStore(state => state.refreshOverview)
  const busy = useFilmStore(state => state.busy)
  const engine = useFilmStore(state => state.engine)

  const storyboard = overview?.storyboard || []
  const imageAssets = useMemo(
    () =>
      Object.values(overview?.project.assets || {})
        .filter(asset => asset.kind === 'image')
        .map(asset => ({ id: asset.id, name: asset.display_name || asset.name })),
    [overview],
  )
  const liveJobs = useMemo(
    () =>
      (overview?.project.jobs || []).filter(
        job => job.status === 'queued' || job.status === 'running',
      ),
    [overview],
  )
  const jobByShot = useMemo(() => {
    const map = new Map()
    for (const job of liveJobs) {
      if (job.shot_id) map.set(job.shot_id, job)
    }
    return map
  }, [liveJobs])

  const pending = storyboard.filter(entry => entry.take_count === 0 && entry.video_prompt).length
  const rendered = storyboard.filter(entry => entry.take_count > 0).length
  return (
    <div className="space-y-4 p-4">
      <section className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-bg-secondary p-3">
        <Clapperboard size={15} className="text-accent-warm" />
        <div className="text-xs text-text-secondary">
          <span className="text-text-primary">{storyboard.length}</span> shots ·{' '}
          <span className="text-text-primary">{rendered}</span> rendered ·{' '}
          <span className="text-text-primary">{pending}</span> waiting to be produced ·{' '}
          planned length {overview?.storyboard_totals.duration_sec.toFixed(1) || '0'}s
        </div>
        <div className="ml-auto flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => void addShot({ objective: 'New shot' })}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
          >
            <Plus size={12} /> Add shot
          </button>
          <button
            type="button"
            onClick={() => void refreshOverview()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
          >
            <RefreshCw size={12} /> Refresh
          </button>
          <button
            type="button"
            onClick={() => void applyContinuity()}
            disabled={Boolean(busy)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
            title="Give every shot that features a character that character's reference photos"
          >
            <Clapperboard size={12} /> Apply visual continuity
          </button>
          <button
            type="button"
            onClick={() => void syncProduction()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
            title="Reconcile shots with the live job queue"
          >
            Check queue
          </button>
          <button
            type="button"
            onClick={() => void produce()}
            disabled={Boolean(busy) || pending === 0 || !engine?.generation_possible}
            title={
              engine?.generation_possible
                ? engine.generation_possible_here
                  ? 'Submit every shot that has no render yet, using the Studio’s current model settings'
                  : `Submit every shot that has no render yet. This computer cannot generate, so the shots are sent to ${engine.generation_runs_on || 'a peer with a GPU'} and the results come back into this workspace.`
                : 'Generation needs a machine with an NVIDIA GPU: install the engine here or add a peer in Compute & transfer'
            }
            className="inline-flex items-center gap-1.5 rounded-lg bg-accent-blue px-3 py-1.5 text-[11px] font-medium text-white disabled:opacity-40"
          >
            {busy ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
            Produce {pending} shot(s)
          </button>
        </div>
      </section>

      {liveJobs.length > 0 && (
        <section className="space-y-1.5 rounded-xl border border-accent-blue/40 bg-accent-blue/10 p-3 text-[11px] text-text-primary">
          {liveJobs.map(job => (
            <div key={job.id} className="flex flex-wrap items-center gap-2">
              <span>
                {job.kind} job {job.id} · {job.status}
              </span>
              <button
                type="button"
                onClick={() => void cancelJob(job.id)}
                disabled={Boolean(busy)}
                className="rounded-md border border-border px-2 py-0.5 text-[10px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
              >
                Cancel
              </button>
            </div>
          ))}
          <div className="text-text-secondary">
            Use “Check queue” to pull finished (or cancelled) work back into the storyboard.
          </div>
        </section>
      )}

      {storyboard.some(entry => entry.status === 'failed') && (
        <section className="rounded-xl border border-chip-red/40 bg-chip-red/10 p-3 text-[11px] text-text-primary">
          {storyboard.filter(entry => entry.status === 'failed').length} shot(s) failed. The error is kept
          on the shot; press “Regenerate” to submit it again with the current settings.
        </section>
      )}

      {storyboard.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border p-8 text-center text-xs text-text-muted">
          No storyboard yet. Write the brief and press “Generate script & storyboard with Director”,
          or add shots manually.
        </div>
      ) : (
        <div className="space-y-2">
          {storyboard.map(entry => (
            <ShotCard
              key={entry.id}
              entry={entry}
              request={undefined}
              total={storyboard.length}
              imageAssets={imageAssets}
              job={jobByShot.get(entry.id)}
              busy={Boolean(busy)}
              onProduce={shotId => void produce([shotId])}
              onCancel={jobId => void cancelJob(jobId)}
            />
          ))}
        </div>
      )}

      {overview && overview.project.bible.characters.length > 0 && (
        <section className="rounded-xl border border-border bg-bg-secondary p-3">
          <h3 className="text-xs font-semibold text-text-primary">Continuity bible</h3>
          <ul className="mt-2 space-y-2 text-[11px] text-text-secondary">
            {overview.project.bible.characters.map(character => (
              <li key={character.id}>
                <span className="text-text-primary">{character.name}</span>
                {character.wardrobe && <span> — {character.wardrobe}</span>}
                <div className="text-text-muted">
                  {character.reference_asset_ids.length
                    ? `${character.reference_asset_ids.length} reference file(s) locked to this character`
                    : 'No reference photo matched this character yet — assign an image its role to keep their identity.'}
                </div>
              </li>
            ))}
          </ul>
          {overview.project.bible.continuity_rules.length > 0 && (
            <ul className="mt-2 list-disc pl-4 text-[11px] text-text-muted">
              {overview.project.bible.continuity_rules.map((rule, index) => (
                <li key={index}>{rule}</li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  )
}
