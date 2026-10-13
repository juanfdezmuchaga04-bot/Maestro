import { useMemo, useRef, useState } from 'react'
import { FileAudio, FileText, FileVideo, Image as ImageIcon, Info, Loader2, Music, Trash2, Upload, Wand2 } from 'lucide-react'
import { useFilmStore } from '../stores/useFilmStore'
import type { FilmAsset, FilmCreativeRole, FilmMediaKind } from '../types/film'

const KIND_ICON: Record<FilmMediaKind, typeof ImageIcon> = {
  image: ImageIcon,
  video: FileVideo,
  audio: FileAudio,
  document: FileText,
  other: Info,
}

const ROLE_LABEL: Record<FilmCreativeRole, string> = {
  character_reference: 'Character reference',
  location_reference: 'Location reference',
  set_design: 'Set design',
  wardrobe: 'Wardrobe',
  lighting: 'Lighting',
  palette: 'Colour palette',
  camera_movement: 'Camera movement',
  style_reference: 'Visual style',
  prop: 'Prop',
  soundtrack: 'Soundtrack',
  source_music_keep: 'Music to preserve',
  character_voice: 'Character voice',
  narration_voice: 'Narration voice',
  dialogue_audio: 'Recorded dialogue',
  sfx: 'Sound effect',
  ambience: 'Ambience',
  source_video_keep: 'Video to preserve',
  source_video_edit: 'Video to edit',
  script_document: 'Script / document',
  other: 'No role',
}

const ACCEPTED = [
  'image/*', 'video/*', 'audio/*',
  '.pdf', '.txt', '.md', '.fountain', '.srt', '.vtt', '.json', '.csv',
].join(',')

function formatBytes(size: number): string {
  if (!size) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = size
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`
}

function formatDuration(seconds: number): string {
  if (!seconds) return '—'
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds - minutes * 60)
  return minutes ? `${minutes}m ${rest}s` : `${rest}s`
}

function AssetCard({ asset, availableRoles }: { asset: FilmAsset; availableRoles: FilmCreativeRole[] }) {
  const setAssetRole = useFilmStore(state => state.setAssetRole)
  const removeAsset = useFilmStore(state => state.removeAsset)
  const analyzeAssets = useFilmStore(state => state.analyzeAssets)
  const Icon = KIND_ICON[asset.kind]
  const [notes, setNotes] = useState(asset.notes)
  const conversion = asset.conversions[0]

  return (
    <div className="rounded-xl border border-border bg-bg-secondary p-3">
      <div className="flex items-start gap-2">
        <Icon size={15} className="mt-0.5 shrink-0 text-accent-warm" />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm text-text-primary" title={asset.name}>
            {asset.name}
          </div>
          <div className="mt-0.5 flex flex-wrap gap-x-3 text-[11px] text-text-muted">
            <span>{asset.kind}</span>
            {asset.analysis.duration > 0 && <span>{formatDuration(asset.analysis.duration)}</span>}
            {asset.analysis.width > 0 && (
              <span>
                {asset.analysis.width}×{asset.analysis.height}
              </span>
            )}
            {asset.analysis.bpm > 0 && <span>{Math.round(asset.analysis.bpm)} BPM</span>}
            <span>{formatBytes(asset.analysis.size)}</span>
          </div>
        </div>
        <button
          type="button"
          onClick={() => void removeAsset(asset.id)}
          className="rounded-md p-1 text-text-muted hover:bg-bg-hover hover:text-chip-red"
          title="Remove from production"
        >
          <Trash2 size={13} />
        </button>
      </div>

      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <label className="block">
          <span className="text-[10px] uppercase tracking-wide text-text-muted">Creative role</span>
          <select
            value={asset.role}
            onChange={event =>
              void setAssetRole(asset.id, event.target.value, {
                reproduceLiterally: asset.reproduce_literally,
                notes,
              })
            }
            className="mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary focus:border-accent-warm focus:outline-none"
          >
            {availableRoles.map(role => (
              <option key={role} value={role}>
                {ROLE_LABEL[role]}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col justify-end">
          <span className="text-[10px] uppercase tracking-wide text-text-muted">
            How should the Director treat it?
          </span>
          <button
            type="button"
            onClick={() =>
              void setAssetRole(asset.id, asset.role, {
                reproduceLiterally: !asset.reproduce_literally,
                notes,
              })
            }
            className={`mt-1 rounded-lg border px-2 py-1.5 text-xs ${
              asset.reproduce_literally
                ? 'border-accent-warm bg-bg-hover text-text-primary'
                : 'border-border bg-bg-tertiary text-text-secondary hover:bg-bg-hover'
            }`}
          >
            {asset.reproduce_literally ? 'Reproduce literally' : 'Inspiration only'}
          </button>
        </label>
      </div>

      {!asset.role_locked && (
        <p className="mt-1.5 text-[11px] text-text-muted">
          Suggested from the file name ({Math.round(asset.role_confidence * 100)}%): {asset.role_reason}
        </p>
      )}

      <div className="mt-2 flex items-center gap-2">
        <input
          value={notes}
          onChange={event => setNotes(event.target.value)}
          onBlur={() => {
            if (notes !== asset.notes) void setAssetRole(asset.id, asset.role, { notes, reproduceLiterally: asset.reproduce_literally })
          }}
          placeholder="Notes for the Director (optional)"
          className="min-w-0 flex-1 rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-[11px] text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none"
        />
        <button
          type="button"
          onClick={() => void analyzeAssets([asset.id], asset.kind === 'audio' && !asset.analysis.available)}
          className="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
        >
          <Wand2 size={11} /> Analyse
        </button>
      </div>

      <div className="mt-2 space-y-1">
        {asset.analysis.status === 'unavailable' && (
          <p className="text-[11px] text-indicator-warning">
            Not analysed: {asset.analysis.limitations[0] || 'no local tool could read this file'}
          </p>
        )}
        {asset.analysis.status === 'partial' && (
          <p className="text-[11px] text-text-muted">
            Partially analysed: {asset.analysis.limitations[0] || 'some properties were not available'}
          </p>
        )}
        {conversion && conversion.status === 'suggested' && (
          <p className="text-[11px] text-indicator-warning">
            Needs converting to {conversion.target_format}: {conversion.reason}
            {conversion.command && (
              <code className="mt-1 block overflow-x-auto rounded bg-bg-tertiary px-2 py-1 text-[10px] text-text-secondary">
                {conversion.command}
              </code>
            )}
          </p>
        )}
        {asset.analysis.palette.length > 0 && (
          <div className="flex items-center gap-1">
            {asset.analysis.palette.map(color => (
              <span
                key={color}
                title={color}
                className="h-3 w-3 rounded-sm border border-border"
                style={{ backgroundColor: color }}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export function StudioMediaPanel() {
  const overview = useFilmStore(state => state.overview)
  const importFiles = useFilmStore(state => state.importFiles)
  const analyzeAssets = useFilmStore(state => state.analyzeAssets)
  const busy = useFilmStore(state => state.busy)
  const uploadProgress = useFilmStore(state => state.uploadProgress)
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const assets = useMemo(
    () => Object.values(overview?.project.assets || {}).sort((a, b) => a.added_at - b.added_at),
    [overview],
  )
  const unanalyzed = assets.filter(asset => asset.analysis.status === 'unknown').map(asset => asset.id)
  const audioAssets = assets.filter(asset => asset.kind === 'audio')

  const onFiles = (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return
    void importFiles(Array.from(fileList), true)
  }

  return (
    <div className="space-y-4 p-4">
      <section
        onDragOver={event => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={event => {
          event.preventDefault()
          setDragging(false)
          onFiles(event.dataTransfer.files)
        }}
        className={`rounded-xl border-2 border-dashed p-6 text-center transition-colors ${
          dragging ? 'border-accent-warm bg-bg-hover' : 'border-border bg-bg-secondary'
        }`}
      >
        <Upload size={22} className="mx-auto text-accent-warm" />
        <h2 className="mt-2 text-sm font-semibold text-text-primary">3. Import your material</h2>
        <p className="mx-auto mt-1 max-w-xl text-xs text-text-secondary">
          Photographs, video, your own music, voices, sound effects, scripts and PDFs. Files stay on
          this machine; the Studio catalogues them locally and suggests what each one could be for.
        </p>
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={Boolean(busy)}
          className="mt-3 inline-flex items-center gap-2 rounded-lg bg-accent-warm px-3 py-2 text-xs font-medium text-black disabled:opacity-40"
        >
          {busy ? <Loader2 size={13} className="animate-spin" /> : <Upload size={13} />} Choose files
        </button>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ACCEPTED}
          className="hidden"
          onChange={event => {
            onFiles(event.target.files)
            event.target.value = ''
          }}
        />
        {uploadProgress && (
          <p className="mt-2 text-[11px] text-text-muted">
            Uploading {uploadProgress.done}/{uploadProgress.total}…
          </p>
        )}
      </section>

      {assets.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void analyzeAssets(unanalyzed.length ? unanalyzed : undefined, false)}
            disabled={Boolean(busy)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
          >
            <Wand2 size={12} /> Analyse {unanalyzed.length ? `${unanalyzed.length} pending` : 'all'} files
          </button>
          {audioAssets.length > 0 && (
            <button
              type="button"
              onClick={() => void analyzeAssets(audioAssets.map(asset => asset.id), true)}
              disabled={Boolean(busy)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
              title="Tempo, beats and sections so scenes can be cut to the music. Loads librosa locally."
            >
              <Music size={12} /> Analyse music (tempo & sections)
            </button>
          )}
          <span className="text-[11px] text-text-muted">
            {assets.length} file(s) in this production
          </span>
        </div>
      )}

      <div className="grid gap-3 lg:grid-cols-2">
        {assets.map(asset => (
          <AssetCard
            key={asset.id}
            asset={asset}
            availableRoles={(overview?.asset_roles[asset.kind] || ['other']) as FilmCreativeRole[]}
          />
        ))}
      </div>

      {assets.length === 0 && (
        <p className="text-center text-xs text-text-muted">
          Nothing imported yet. Even one photograph or your own music changes what the Director can do.
        </p>
      )}

      {overview && overview.requirements.length > 0 && (
        <section className="rounded-xl border border-border bg-bg-secondary p-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-text-muted">
            Local tools missing
          </div>
          <ul className="mt-2 space-y-1.5 text-[11px] text-text-secondary">
            {overview.requirements.map(entry => (
              <li key={entry.tool}>
                <span className="text-text-primary">{entry.tool}</span> — {entry.why}
                <div className="text-text-muted">{entry.install}</div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
