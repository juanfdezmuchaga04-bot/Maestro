import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { AlertTriangle, ClipboardList, Loader2, Save, Sparkles } from 'lucide-react'
import { planShortFilmScript } from '../api/client'
import { useStore } from '../stores/useStore'
import { useFilmStore } from '../stores/useFilmStore'
import type { FilmApproach, FilmBrief } from '../types/film'
import { useSyncedDraft } from './useSyncedDraft'

const ASPECT_RATIOS = ['16:9', '9:16', '2.39:1', '21:9', '4:3', '1:1']

const APPROACHES: Array<{ id: FilmApproach; label: string; help: string }> = [
  {
    id: 'faithful',
    label: 'Faithful',
    help: 'Your material is the film: keep the faces, wardrobe, architecture and structure you supplied.',
  },
  {
    id: 'hybrid',
    label: 'Hybrid',
    help: 'Keep the principal references exactly, develop the rest creatively within the brief.',
  },
  {
    id: 'creative',
    label: 'Creative',
    help: 'Your material sets tone and palette; the Director is free to restructure and invent.',
  },
]

const EMPTY_BRIEF: FilmBrief = {
  logline: '',
  synopsis: '',
  goal: '',
  genre: '',
  tone: '',
  visual_style: '',
  language: '',
  audience: '',
  aspect_ratio: '16:9',
  target_duration_sec: 60,
  reference_notes: '',
  must_keep: [],
  avoid: [],
}

function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <label className="block">
      <span className="text-[11px] font-medium uppercase tracking-wide text-text-muted">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] text-text-muted">{hint}</span>}
    </label>
  )
}

const inputClass =
  'mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none'

export function StudioBriefPanel() {
  const project = useFilmStore(state => state.overview?.project)
  const briefBundle = useFilmStore(state => state.briefBundle)
  const refreshBrief = useFilmStore(state => state.refreshBrief)
  const saveBrief = useFilmStore(state => state.saveBrief)
  const importDirectorPlan = useFilmStore(state => state.importDirectorPlan)
  const busy = useFilmStore(state => state.busy)
  const engine = useFilmStore(state => state.engine)
  const modelOptions = useStore(state => state.modelOptions)

  const [planning, setPlanning] = useState(false)
  const [planError, setPlanError] = useState<string | null>(null)

  // The brief is edited locally and only reset when the stored brief changes.
  const synced = useMemo(
    () => ({
      ...EMPTY_BRIEF,
      ...(project?.brief || {}),
      approach: project?.approach || ('hybrid' as FilmApproach),
    }),
    [project],
  )
  const [draft, setDraft] = useSyncedDraft(JSON.stringify(synced), () => synced)
  const approach = draft.approach as FilmApproach

  useEffect(() => {
    void refreshBrief()
  }, [refreshBrief, project?.id])

  const dirty = useMemo(() => JSON.stringify(synced) !== JSON.stringify(draft), [synced, draft])

  const blocking = (briefBundle?.readiness || []).filter(issue => issue.severity === 'blocking')

  const update = <K extends keyof FilmBrief>(key: K, value: FilmBrief[K]) =>
    setDraft(current => ({ ...current, [key]: value }) as typeof current)

  const listToText = (values: string[]) => values.join('\n')
  const textToList = (value: string) =>
    value.split('\n').map(line => line.trim()).filter(Boolean)

  const runDirector = async () => {
    if (!project) return
    setPlanning(true)
    setPlanError(null)
    try {
      if (dirty) {
        await saveBrief({ ...draft, approach })
      }
      // Hand the composed brief to Director Mode's own story planner. The Studio
      // never writes the script itself; it consumes the planner's real output.
      const response = await planShortFilmScript({
        story_description: briefBundle?.brief || draft.logline || draft.synopsis || draft.goal,
        reference_image_path: briefBundle?.reference_inputs.reference_image_path ?? undefined,
        target_duration: Math.round(draft.target_duration_sec || 60),
        fps: modelOptions?.fps ?? 24,
        frames_steps: modelOptions?.frames_steps ?? 4,
        frames_minimum: modelOptions?.frames_minimum ?? 5,
      })
      await importDirectorPlan(response as unknown as Record<string, unknown>, 'short_film_script')
    } catch (error) {
      setPlanError(
        `${error instanceof Error ? error.message : String(error)} — Director planning needs the local LLM. Start it in Settings → Services, then try again.`,
      )
    } finally {
      setPlanning(false)
    }
  }

  if (!project) return null

  return (
    <div className="grid gap-4 p-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <section className="space-y-4">
        <header>
          <h2 className="text-sm font-semibold text-text-primary">2. Describe the idea</h2>
          <p className="mt-1 text-xs text-text-muted">
            Write it the way you would brief a director. Everything here is composed into the
            instruction Director Mode receives.
          </p>
        </header>

        <Field label="Logline">
          <input
            value={draft.logline}
            onChange={event => update('logline', event.target.value)}
            placeholder="A courier crosses a flooded city at night."
            className={inputClass}
          />
        </Field>

        <Field label="Objective" hint="What should this film achieve?">
          <input
            value={draft.goal}
            onChange={event => update('goal', event.target.value)}
            placeholder="A melancholic, realistic nocturnal sequence."
            className={inputClass}
          />
        </Field>

        <Field label="Treatment" hint="Story, structure, key beats.">
          <textarea
            value={draft.synopsis}
            onChange={event => update('synopsis', event.target.value)}
            rows={5}
            className={inputClass}
          />
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Genre">
            <input value={draft.genre} onChange={event => update('genre', event.target.value)} className={inputClass} />
          </Field>
          <Field label="Tone">
            <input value={draft.tone} onChange={event => update('tone', event.target.value)} className={inputClass} />
          </Field>
          <Field label="Aspect ratio">
            <select
              value={draft.aspect_ratio}
              onChange={event => update('aspect_ratio', event.target.value)}
              className={inputClass}
            >
              {ASPECT_RATIOS.map(ratio => (
                <option key={ratio} value={ratio}>{ratio}</option>
              ))}
            </select>
          </Field>
          <Field label="Target duration (seconds)">
            <input
              type="number"
              min={1}
              max={36000}
              value={draft.target_duration_sec}
              onChange={event => update('target_duration_sec', Number(event.target.value))}
              className={inputClass}
            />
          </Field>
          <Field label="Dialogue language">
            <input value={draft.language} onChange={event => update('language', event.target.value)} className={inputClass} />
          </Field>
          <Field label="Audience">
            <input value={draft.audience} onChange={event => update('audience', event.target.value)} className={inputClass} />
          </Field>
        </div>

        <Field label="Visual style" hint="Light, colour, lens, references in words.">
          <textarea
            value={draft.visual_style}
            onChange={event => update('visual_style', event.target.value)}
            rows={3}
            className={inputClass}
          />
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Must keep" hint="One per line.">
            <textarea
              value={listToText(draft.must_keep)}
              onChange={event => update('must_keep', textToList(event.target.value))}
              rows={3}
              className={inputClass}
            />
          </Field>
          <Field label="Avoid" hint="One per line.">
            <textarea
              value={listToText(draft.avoid)}
              onChange={event => update('avoid', textToList(event.target.value))}
              rows={3}
              className={inputClass}
            />
          </Field>
        </div>

        <div>
          <span className="text-[11px] font-medium uppercase tracking-wide text-text-muted">
            4. Creative approach
          </span>
          <div className="mt-2 grid gap-2 sm:grid-cols-3">
            {APPROACHES.map(entry => (
              <button
                key={entry.id}
                type="button"
                onClick={() => setDraft(current => ({ ...current, approach: entry.id }))}
                className={`rounded-lg border px-3 py-2 text-left transition-colors ${
                  approach === entry.id
                    ? 'border-accent-warm bg-bg-hover'
                    : 'border-border bg-bg-secondary hover:bg-bg-hover'
                }`}
              >
                <div className="text-xs font-semibold text-text-primary">{entry.label}</div>
                <div className="mt-0.5 text-[11px] text-text-muted">{entry.help}</div>
              </button>
            ))}
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => void saveBrief({ ...draft, approach })}
            disabled={!dirty || Boolean(busy)}
            className="inline-flex items-center gap-2 rounded-lg bg-accent-warm px-3 py-2 text-xs font-medium text-black disabled:opacity-40"
          >
            <Save size={13} /> {dirty ? 'Save brief' : 'Saved'}
          </button>
          <button
            type="button"
            onClick={() => void runDirector()}
            disabled={planning || Boolean(busy) || blocking.length > 0}
            className="inline-flex items-center gap-2 rounded-lg bg-accent-blue px-3 py-2 text-xs font-medium text-white disabled:opacity-40"
          >
            {planning ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}
            5. Generate script & storyboard with Director
          </button>
        </div>

        {blocking.length > 0 && (
          <div className="rounded-lg border border-indicator-warning/40 bg-indicator-warning/10 p-3 text-[11px] text-text-primary">
            <div className="flex items-center gap-1.5 font-medium">
              <AlertTriangle size={12} /> Finish these first
            </div>
            <ul className="mt-1 list-disc pl-4 text-text-secondary">
              {blocking.map((issue, index) => (
                <li key={`${issue.code}-${index}`}>{issue.message}</li>
              ))}
            </ul>
          </div>
        )}

        {planError && (
          <div className="rounded-lg border border-chip-red/40 bg-chip-red/10 p-3 text-[11px] text-text-primary">
            {planError}
          </div>
        )}

        {!engine?.generation_possible && (
          <p className="text-[11px] text-text-muted">
            No CUDA GPU in this process: you can write the brief and plan the storyboard here, then
            run the production on a machine with an NVIDIA GPU.
          </p>
        )}
      </section>

      <section className="space-y-3">
        <header className="flex items-center gap-2">
          <ClipboardList size={14} className="text-accent-warm" />
          <h2 className="text-sm font-semibold text-text-primary">Exact instruction sent to Director</h2>
        </header>
        <pre className="max-h-[420px] overflow-auto rounded-xl border border-border bg-bg-secondary p-3 text-[11px] leading-relaxed whitespace-pre-wrap text-text-secondary">
          {briefBundle?.brief || 'Fill in the brief to see the composed instruction.'}
        </pre>

        <div className="rounded-xl border border-border bg-bg-secondary p-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-text-muted">
            References matched
          </div>
          {briefBundle ? (
            <ul className="mt-2 space-y-1 text-[11px] text-text-secondary">
              <li>
                Main character reference:{' '}
                <span className="text-text-primary">
                  {briefBundle.reference_inputs.reference_image_path
                    ? briefBundle.reference_inputs.reference_image_path.split(/[\\/]/).pop()
                    : 'none'}
                </span>
              </li>
              <li>
                Other character references: {briefBundle.reference_inputs.character_ref_paths.length}
              </li>
              <li>
                Location / set references: {briefBundle.reference_inputs.location_ref_paths.length}
              </li>
              <li>
                Soundtrack:{' '}
                <span className="text-text-primary">
                  {briefBundle.reference_inputs.audio_file
                    ? briefBundle.reference_inputs.audio_file.split(/[\\/]/).pop()
                    : 'none'}
                </span>
              </li>
              {briefBundle.reference_inputs.unresolved_asset_names.length > 0 && (
                <li className="text-indicator-warning">
                  Missing on disk: {briefBundle.reference_inputs.unresolved_asset_names.join(', ')}
                </li>
              )}
            </ul>
          ) : (
            <p className="mt-2 text-[11px] text-text-muted">Loading…</p>
          )}
        </div>
      </section>
    </div>
  )
}
