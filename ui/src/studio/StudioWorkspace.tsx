import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, Clapperboard, Cpu, Loader2, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { useStore } from '../stores/useStore'
import { useFilmStore } from '../stores/useFilmStore'
import type { FilmEngineStatus } from '../types/film'
import { StudioBriefPanel } from './StudioBriefPanel'
import { StudioMediaPanel } from './StudioMediaPanel'
import { StudioStoryboardPanel } from './StudioStoryboardPanel'
import { StudioFinishingPanel } from './StudioFinishingPanel'
import { StudioComputePanel } from './StudioComputePanel'
import { StudioTransferPanel } from './StudioTransferPanel'
import { StudioQueuePanel } from './StudioQueuePanel'
import { StudioLocalEnginePanel } from './StudioLocalEnginePanel'

type StudioPhase = 'project' | 'idea' | 'files' | 'storyboard' | 'finish' | 'compute'

const PHASES: Array<{ id: StudioPhase; label: string; step: string }> = [
  { id: 'project', label: 'Project', step: '1' },
  { id: 'idea', label: 'Idea & approach', step: '2-4' },
  { id: 'files', label: 'Files', step: '3' },
  { id: 'storyboard', label: 'Script & storyboard', step: '5-8' },
  { id: 'finish', label: 'Mix & export', step: '9-10' },
  { id: 'compute', label: 'Compute & transfer', step: '11' },
]

function PhaseButton({
  active,
  done,
  label,
  step,
  onClick,
}: {
  active: boolean
  done: boolean
  label: string
  step: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex shrink-0 items-center gap-2 rounded-lg border px-3 py-2 text-left transition-colors ${
        active
          ? 'border-accent-warm bg-bg-hover text-text-primary'
          : 'border-border bg-bg-secondary text-text-secondary hover:bg-bg-hover hover:text-text-primary'
      }`}
    >
      <span
        className={`grid h-6 w-6 shrink-0 place-items-center rounded-full text-[10px] font-semibold ${
          done ? 'bg-accent-green text-black' : active ? 'bg-accent-warm text-black' : 'bg-bg-tertiary text-text-muted'
        }`}
      >
        {done ? <CheckCircle2 size={13} /> : step}
      </span>
      <span className="text-xs font-medium whitespace-nowrap">{label}</span>
    </button>
  )
}

function ProjectRail() {
  const projects = useFilmStore(state => state.projects)
  const activeProjectId = useFilmStore(state => state.activeProjectId)
  const openProject = useFilmStore(state => state.openProject)
  const createProject = useFilmStore(state => state.createProject)
  const deleteProject = useFilmStore(state => state.deleteProject)
  const refreshProjects = useFilmStore(state => state.refreshProjects)
  const [name, setName] = useState('')

  return (
    <div className="flex h-full flex-col gap-4 overflow-y-auto p-4">
      <div>
        <h2 className="text-sm font-semibold text-text-primary">Productions</h2>
        <p className="mt-1 text-xs text-text-muted">
          A production keeps the brief, the imported files, the storyboard, the soundtrack and the
          final cut together. Everything is stored inside your Maestro workspace.
        </p>
      </div>

      <form
        onSubmit={event => {
          event.preventDefault()
          if (!name.trim()) return
          void createProject(name.trim())
          setName('')
        }}
        className="flex gap-2"
      >
        <input
          value={name}
          onChange={event => setName(event.target.value)}
          placeholder="New production name"
          className="min-w-0 flex-1 rounded-lg border border-border bg-bg-tertiary px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-warm focus:outline-none"
        />
        <button
          type="submit"
          disabled={!name.trim()}
          className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-accent-warm text-black disabled:opacity-40"
          title="Create production"
        >
          <Plus size={16} />
        </button>
      </form>

      <div className="flex items-center justify-between">
        <span className="text-[11px] uppercase tracking-wide text-text-muted">
          {projects.length} saved
        </span>
        <button
          type="button"
          onClick={() => void refreshProjects()}
          className="rounded-md p-1.5 text-text-muted hover:bg-bg-hover hover:text-text-primary"
          title="Refresh"
        >
          <RefreshCw size={14} />
        </button>
      </div>

      <ul className="flex flex-col gap-2">
        {projects.map(project => (
          <li key={project.id}>
            <div
              className={`group rounded-lg border px-3 py-2 transition-colors ${
                project.id === activeProjectId
                  ? 'border-accent-warm bg-bg-hover'
                  : 'border-border bg-bg-secondary hover:bg-bg-hover'
              }`}
            >
              <button
                type="button"
                onClick={() => void openProject(project.id)}
                className="w-full text-left"
              >
                <div className="flex items-center gap-2">
                  <Clapperboard size={13} className="shrink-0 text-accent-warm" />
                  <span className="truncate text-sm font-medium text-text-primary">{project.name}</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-text-muted">
                  <span>{project.status}</span>
                  {project.genre && <span>{project.genre}</span>}
                  <span>{project.shot_count} shots</span>
                  <span>{project.generated_shot_count} rendered</span>
                  <span>{Math.round(project.planned_duration_sec)}s</span>
                </div>
              </button>
              <button
                type="button"
                onClick={() => void deleteProject(project.id)}
                className="mt-1 inline-flex items-center gap-1 text-[11px] text-text-muted opacity-0 transition-opacity group-hover:opacity-100 hover:text-chip-red"
              >
                <Trash2 size={11} /> Delete
              </button>
            </div>
          </li>
        ))}
        {projects.length === 0 && (
          <li className="rounded-lg border border-dashed border-border px-3 py-6 text-center text-xs text-text-muted">
            No productions yet. Create one to begin.
          </li>
        )}
      </ul>
    </div>
  )
}

export function StudioWorkspace() {
  const workspace = useStore(state => state.activeWorkspace)
  const setSidebarMode = useStore(state => state.setSidebarMode)
  const initialize = useFilmStore(state => state.initialize)
  const overview = useFilmStore(state => state.overview)
  const engine = useFilmStore(state => state.engine)
  const activeProjectId = useFilmStore(state => state.activeProjectId)
  const loading = useFilmStore(state => state.loading)
  const busy = useFilmStore(state => state.busy)
  const error = useFilmStore(state => state.error)
  const notice = useFilmStore(state => state.notice)
  const clearNotice = useFilmStore(state => state.clearNotice)
  const refreshOverview = useFilmStore(state => state.refreshOverview)
  const [phase, setPhase] = useState<StudioPhase>('project')
  const [railOpen, setRailOpen] = useState(false)

  useEffect(() => {
    void initialize(workspace || 'default')
  }, [initialize, workspace])

  const totals = overview?.storyboard_totals
  const phaseDone = useMemo(
    () => ({
      project: Boolean(activeProjectId),
      idea: Boolean(overview?.project.brief.logline || overview?.project.brief.goal),
      files: Boolean(overview && Object.keys(overview.project.assets).length > 0),
      storyboard: Boolean(totals && totals.shot_count > 0),
      finish: Boolean(overview?.project.exports.length),
      // Compute has no "done" state: it is configuration, not a step of the film.
      compute: false,
    }),
    [activeProjectId, overview, totals],
  )

  // Closing a production returns to the project list without an effect:
  // the phase is simply not meaningful until a project is open again. Compute
  // is the exception: a node is configured before a production exists.
  const activePhase: StudioPhase = activeProjectId || phase === 'compute' ? phase : 'project'

  const blocking = (overview?.readiness || []).filter(issue => issue.severity === 'blocking')

  return (
    <div className="flex h-full w-full flex-col bg-bg-primary">
      <header className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border bg-bg-secondary px-3 py-2">
        <div className="flex items-center gap-2">
          <ClapperMark />
          <div className="leading-tight">
            <div className="text-sm font-semibold text-text-primary">Films</div>
            <div className="text-[10px] text-text-muted">AI production studio</div>
          </div>
        </div>

        <button
          type="button"
          onClick={() => setRailOpen(value => !value)}
          className="ml-auto rounded-lg border border-border px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover hover:text-text-primary md:hidden"
        >
          Productions
        </button>

        <div className="ml-auto hidden items-center gap-2 md:flex">
          <EngineBadge engine={engine} />
          <button
            type="button"
            onClick={() => void refreshOverview()}
            disabled={!activeProjectId}
            className="rounded-lg border border-border px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
          >
            Refresh
          </button>
          <button
            type="button"
            onClick={() => setSidebarMode('editor')}
            className="rounded-lg border border-border px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover hover:text-text-primary"
          >
            Open Editor
          </button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <aside
          className={`${railOpen ? 'block' : 'hidden'} w-full shrink-0 border-r border-border bg-bg-secondary md:block md:w-72`}
        >
          <ProjectRail />
        </aside>

        <main className="flex min-w-0 flex-1 flex-col">
          <nav className="flex shrink-0 gap-2 overflow-x-auto border-b border-border px-3 py-2">
            {PHASES.map(entry => (
              <PhaseButton
                key={entry.id}
                active={activePhase === entry.id}
                done={phaseDone[entry.id]}
                label={`${entry.label}`}
                step={entry.step}
                onClick={() => setPhase(entry.id)}
              />
            ))}
          </nav>

          {(error || notice || busy || blocking.length > 0) && (
            <div className="flex shrink-0 flex-col gap-1 border-b border-border px-3 py-2">
              {busy && (
                <div className="flex items-center gap-2 text-xs text-text-secondary">
                  <Loader2 size={13} className="animate-spin" /> {busy}
                </div>
              )}
              {error && (
                <div className="flex items-start gap-2 rounded-lg border border-chip-red/40 bg-chip-red/10 px-2.5 py-1.5 text-xs text-text-primary">
                  <AlertTriangle size={13} className="mt-0.5 shrink-0 text-chip-red" />
                  <span className="flex-1">{error}</span>
                  <button type="button" onClick={clearNotice} className="text-text-muted hover:text-text-primary">
                    <X size={12} />
                  </button>
                </div>
              )}
              {notice && !error && (
                <div className="flex items-start gap-2 rounded-lg border border-accent-green/40 bg-accent-green/10 px-2.5 py-1.5 text-xs text-text-primary">
                  <CheckCircle2 size={13} className="mt-0.5 shrink-0 text-accent-green" />
                  <span className="flex-1">{notice}</span>
                  <button type="button" onClick={clearNotice} className="text-text-muted hover:text-text-primary">
                    <X size={12} />
                  </button>
                </div>
              )}
              {!error && blocking.length > 0 && (
                <div className="flex items-start gap-2 rounded-lg border border-indicator-warning/40 bg-indicator-warning/10 px-2.5 py-1.5 text-xs text-text-primary">
                  <AlertTriangle size={13} className="mt-0.5 shrink-0 text-indicator-warning" />
                  <div className="flex-1">
                    <div className="font-medium">Pending requirements</div>
                    <ul className="mt-0.5 list-disc pl-4 text-text-secondary">
                      {blocking.slice(0, 4).map((issue, index) => (
                        <li key={`${issue.code}-${index}`}>{issue.message}</li>
                      ))}
                    </ul>
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="min-h-0 flex-1 overflow-y-auto">
            {loading && !overview && (
              <div className="grid h-full place-items-center text-sm text-text-muted">
                <Loader2 size={18} className="animate-spin" />
              </div>
            )}
            {!activeProjectId && activePhase === 'project' && (
              <div className="grid h-full place-items-center p-6 text-center">
                <div className="max-w-md">
                  <Clapperboard size={28} className="mx-auto text-accent-warm" />
                  <h2 className="mt-3 text-lg font-semibold text-text-primary">
                    Describe an idea, bring your files, get a finished cut
                  </h2>
                  <p className="mt-2 text-sm text-text-secondary">
                    Create a production on the left. You will add your photographs, videos, music,
                    voices and scripts, tell the Director what you want, review the storyboard, and
                    render the film with your local models.
                  </p>
                  <p className="mt-3 text-xs text-text-muted">
                    Everything runs on this machine. Nothing is uploaded to a third party.
                  </p>
                </div>
              </div>
            )}
            {activeProjectId && activePhase === 'project' && (
              <div className="p-4">
                <ProjectSummaryCard />
              </div>
            )}
            {activeProjectId && activePhase === 'idea' && <StudioBriefPanel key={activeProjectId} />}
            {activeProjectId && activePhase === 'files' && <StudioMediaPanel key={activeProjectId} />}
            {activeProjectId && activePhase === 'storyboard' && (
              <StudioStoryboardPanel key={activeProjectId} />
            )}
            {activeProjectId && activePhase === 'finish' && <StudioFinishingPanel key={activeProjectId} />}
            {activePhase === 'compute' && (
              <div className="flex flex-col gap-4">
                <StudioLocalEnginePanel />
                <StudioQueuePanel />
                <StudioComputePanel />
                <StudioTransferPanel />
              </div>
            )}
          </div>
        </main>
      </div>
    </div>
  )
}

function ClapperMark() {
  return (
    <span className="grid h-8 w-8 place-items-center rounded-lg bg-gradient-to-br from-accent-warm to-chip-orange text-black">
      <Clapperboard size={17} />
    </span>
  )
}

function EngineBadge({ engine }: { engine: FilmEngineStatus | null }) {
  if (!engine) return null
  const ready = engine.generation_possible
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] ${
        ready
          ? 'border-accent-green/40 bg-accent-green/10 text-text-primary'
          : 'border-indicator-warning/40 bg-indicator-warning/10 text-text-primary'
      }`}
      title={
        ready
          ? 'Local GPU detected: generation, assembly and export can run here.'
          : 'No CUDA GPU detected in this process. Planning, media and assembly are available; generation must run on a machine with an NVIDIA GPU.'
      }
    >
      <Cpu size={12} />
      {ready ? 'GPU ready' : 'GPU unavailable'}
      {!engine.ffmpeg && <span className="text-indicator-warning">· no FFmpeg</span>}
    </span>
  )
}

function ProjectSummaryCard() {
  const overview = useFilmStore(state => state.overview)
  const engine = useFilmStore(state => state.engine)
  if (!overview) return null
  const { summary, project, requirements } = overview
  const rows: Array<[string, string]> = [
    ['Status', project.status],
    ['Approach', project.approach],
    ['Genre / style', [project.brief.genre, project.brief.visual_style].filter(Boolean).join(' · ') || '—'],
    ['Files', String(summary.asset_count)],
    ['Scenes / shots', `${summary.scene_count} / ${summary.shot_count}`],
    ['Rendered shots', `${summary.generated_shot_count} of ${summary.shot_count}`],
    ['Planned length', `${summary.planned_duration_sec.toFixed(1)}s`],
    ['Editor project', summary.editor_project_id || '—'],
    ['Exports', String(summary.export_count)],
  ]
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <section className="rounded-xl border border-border bg-bg-secondary p-4">
        <h2 className="text-sm font-semibold text-text-primary">Production status</h2>
        <dl className="mt-3 grid gap-x-6 gap-y-2 sm:grid-cols-2">
          {rows.map(([label, value]) => (
            <div key={label} className="flex items-baseline justify-between gap-3 border-b border-border/60 pb-1">
              <dt className="text-xs text-text-muted">{label}</dt>
              <dd className="truncate text-xs text-text-primary">{value}</dd>
            </div>
          ))}
        </dl>
      </section>
      <section className="rounded-xl border border-border bg-bg-secondary p-4">
        <h2 className="text-sm font-semibold text-text-primary">Local engine</h2>
        <ul className="mt-3 space-y-1.5 text-xs text-text-secondary">
          <li>GPU (CUDA): {engine?.gpu ? 'available' : 'not available in this process'}</li>
          <li>FFmpeg: {engine?.ffmpeg ? 'available' : 'missing — final render unavailable'}</li>
          <li>FFprobe: {engine?.ffprobe ? 'available' : 'missing — media metadata unavailable'}</li>
          <li>Audio analysis (librosa): {engine?.audio_analysis ? 'available' : 'missing'}</li>
          <li>Image analysis (Pillow): {engine?.image_analysis ? 'available' : 'missing'}</li>
          <li>PDF text (pypdf): {engine?.pdf_text ? 'available' : 'missing'}</li>
        </ul>
        {requirements.length > 0 && (
          <div className="mt-3 rounded-lg border border-border bg-bg-tertiary p-3">
            <div className="text-[11px] font-medium text-text-primary">To install</div>
            <ul className="mt-1 space-y-1.5 text-[11px] text-text-muted">
              {requirements.map(entry => (
                <li key={entry.tool}>
                  <span className="text-text-secondary">{entry.tool}</span> — {entry.why}
                  <div className="text-text-muted">{entry.install}</div>
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>
    </div>
  )
}
