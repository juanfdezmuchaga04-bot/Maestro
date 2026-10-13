import { useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  ListOrdered,
  Loader2,
  PlayCircle,
  RefreshCw,
  RotateCcw,
  Square,
  Trash2,
  Wrench,
} from 'lucide-react'
import {
  acknowledgeQueueCancel,
  cancelQueueJob,
  fetchQueue,
  fetchQueueEvents,
  pruneQueue,
  recoverQueue,
  retryQueueJob,
  runQueue,
  startQueueWorker,
  stopQueueWorker,
  updateQueueSettings,
} from '../api/queue'
import type { QueueEvent, QueueJob, QueueJobState, QueueSettings, QueueSnapshot } from '../types/queue'

const STATE_STYLES: Record<QueueJobState, string> = {
  draft: 'border-border bg-bg-tertiary text-text-muted',
  pending: 'border-chip-orange/40 bg-chip-orange/10 text-text-primary',
  preparing: 'border-accent-warm/40 bg-accent-warm/10 text-text-primary',
  running: 'border-accent-warm/50 bg-accent-warm/15 text-text-primary',
  finalizing: 'border-accent-warm/40 bg-accent-warm/10 text-text-primary',
  completed: 'border-accent-green/40 bg-accent-green/10 text-text-primary',
  failed: 'border-chip-red/40 bg-chip-red/10 text-text-primary',
  cancelled: 'border-border bg-bg-tertiary text-text-muted',
}

const TERMINAL: QueueJobState[] = ['completed', 'failed', 'cancelled']
const LIVE: QueueJobState[] = ['preparing', 'running', 'finalizing']

function StateChip({ state }: { state: QueueJobState }) {
  return (
    <span className={`rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${STATE_STYLES[state]}`}>
      {state}
    </span>
  )
}

function formatSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (value < 60) return `${value.toFixed(1)}s`
  const minutes = Math.floor(value / 60)
  return `${minutes}m ${Math.round(value - minutes * 60)}s`
}

/**
 * Progress that only exists when an engine measured it.
 *
 * `percent === null` is not "0%": it means nobody reported a number, so the job
 * is described by its state and its own note instead of a bar we would be
 * inventing. This is the spec's rule about not confusing an HTTP request with
 * generation progress, enforced where the user can see it.
 */
function ProgressView({ job }: { job: QueueJob }) {
  const { progress } = job
  if (progress.percent === null) {
    return (
      <div className="text-[10px] text-text-muted">
        {LIVE.includes(job.state) ? 'working — this engine reports no percentage' : 'no percentage reported'}
        {progress.note ? ` · ${progress.note}` : ''}
      </div>
    )
  }
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-bg-tertiary">
        <div
          className="h-full rounded-full bg-accent-warm"
          style={{ width: `${Math.max(0, Math.min(100, progress.percent))}%` }}
        />
      </div>
      <span className="shrink-0 text-[10px] text-text-secondary">
        {progress.percent}%
        {progress.step !== null && progress.total_steps
          ? ` · step ${progress.step}/${progress.total_steps}`
          : ''}
      </span>
    </div>
  )
}

function JobCard({
  job,
  onChanged,
  onError,
}: {
  job: QueueJob
  onChanged: () => void
  onError: (message: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false)
  const terminal = TERMINAL.includes(job.state)
  const canRetry = job.state === 'failed' || job.state === 'cancelled'

  const act = async (action: () => Promise<unknown>) => {
    setBusy(true)
    try {
      await action()
      onChanged()
    } catch (err) {
      onError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className="rounded-xl border border-border bg-bg-secondary p-3">
      <div className="flex flex-wrap items-center gap-2">
        <StateChip state={job.state} />
        <span className="text-xs font-medium text-text-primary">{job.task}</span>
        <span className="text-[11px] text-text-muted">{job.engine}</span>
        {job.priority !== 0 && (
          <span className="rounded-md border border-border bg-bg-tertiary px-1.5 py-0.5 text-[10px] text-text-secondary">
            priority {job.priority}
          </span>
        )}
        <button
          type="button"
          onClick={() => setOpen(value => !value)}
          className="ml-auto text-[10px] text-text-muted hover:text-text-primary"
        >
          {open ? 'hide detail' : 'detail'}
        </button>
      </div>

      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-text-muted">
        <span>job {job.job_id}</span>
        {job.project_id && <span>project {job.project_id}</span>}
        {job.scene_id && <span>scene {job.scene_id}</span>}
        {job.shot_id && <span>shot {job.shot_id}</span>}
        {job.node_id && <span>node {job.node_id}</span>}
        {job.worker && <span>worker {job.worker}</span>}
        <span>
          attempts {job.attempts}/{job.max_retries}
        </span>
        {job.duration_seconds !== null && <span>took {formatSeconds(job.duration_seconds)}</span>}
        {job.cancel_requested && !terminal && <span className="text-indicator-warning">stop requested</span>}
      </div>

      <div className="mt-2">
        <ProgressView job={job} />
      </div>

      {job.errors.length > 0 && (
        <div className="mt-2 rounded-lg border border-chip-red/40 bg-chip-red/10 p-2 text-[10px] text-text-primary">
          <div className="flex items-start gap-1.5">
            <AlertTriangle size={11} className="mt-0.5 shrink-0 text-chip-red" />
            <span className="min-w-0 flex-1 break-words">{job.errors[job.errors.length - 1]}</span>
          </div>
          {job.errors.length > 1 && (
            <div className="mt-1 text-text-muted">{job.errors.length} errors recorded on this job</div>
          )}
        </div>
      )}

      {job.outputs.length > 0 && (
        <ul className="mt-2 space-y-0.5">
          {job.outputs.map(output => (
            <li key={output} className="flex items-start gap-1.5 text-[10px] text-text-secondary">
              <CheckCircle2 size={11} className="mt-0.5 shrink-0 text-accent-green" />
              <span className="min-w-0 flex-1 break-all">{output}</span>
            </li>
          ))}
        </ul>
      )}

      {open && (
        <dl className="mt-2 grid gap-x-4 gap-y-1 text-[10px] sm:grid-cols-2">
          {[
            ['model', job.model || '—'],
            ['dedupe key', job.dedupe_key || '—'],
            ['engine job id', job.external_job_id || '—'],
            ['declared memory', job.memory_mb ? `${job.memory_mb} MB` : 'not declared'],
            ['reports progress', job.progress_supported ? 'yes' : 'no'],
            ['can be stopped', job.cancel_supported ? 'yes' : 'not declared'],
            ['declared node', job.node_id || '—'],
            ['input files', job.inputs.length ? job.inputs.join(', ') : '—'],
          ].map(([label, value]) => (
            <div key={label} className="flex items-baseline justify-between gap-2 border-b border-border/60 pb-0.5">
              <dt className="text-text-muted">{label}</dt>
              <dd className="min-w-0 truncate text-text-secondary" title={value}>
                {value}
              </dd>
            </div>
          ))}
        </dl>
      )}

      <div className="mt-2 flex flex-wrap gap-2">
        {!terminal && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void act(() => cancelQueueJob(job.job_id))}
            className="flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
          >
            <Square size={11} /> Cancel
          </button>
        )}
        {canRetry && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void act(() => retryQueueJob(job.job_id))}
            className="flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
          >
            <RotateCcw size={11} /> Retry
          </button>
        )}
        {job.cancel_requested && !terminal && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void act(() => acknowledgeQueueCancel(job.job_id))}
            className="flex items-center gap-1.5 rounded-lg border border-indicator-warning/40 px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
          >
            <Wrench size={11} /> Confirm it stopped
          </button>
        )}
        {busy && <Loader2 size={13} className="animate-spin self-center text-text-muted" />}
      </div>
    </li>
  )
}

export function StudioQueuePanel() {
  const [snapshot, setSnapshot] = useState<QueueSnapshot | null>(null)
  const [settings, setSettings] = useState<QueueSettings | null>(null)
  const [events, setEvents] = useState<QueueEvent[]>([])
  const [stateFilter, setStateFilter] = useState<'' | QueueJobState>('')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const data = await fetchQueue({ state: stateFilter || undefined, limit: 100 })
      setSnapshot(data)
      setSettings(data.summary.settings)
      setError('')
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [stateFilter])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // The queue keeps moving while the phone looks at it, so it is polled. This
  // only reads state; nothing here generates a percentage.
  useEffect(() => {
    const timer = window.setInterval(() => void refresh(), 5000)
    return () => window.clearInterval(timer)
  }, [refresh])

  const run = async (label: string, action: () => Promise<unknown>, message?: (result: unknown) => string) => {
    setBusy(label)
    setError('')
    setNotice('')
    try {
      const result = await action()
      setNotice(message ? message(result) : `${label} done`)
      await refresh()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy('')
    }
  }

  const counts = snapshot?.summary.counts
  const jobs = snapshot?.jobs || []
  const live = jobs.filter(job => LIVE.includes(job.state))

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <ListOrdered size={16} className="text-accent-warm" />
        <h2 className="text-sm font-semibold text-text-primary">Job queue</h2>
        <button
          type="button"
          onClick={() => void refresh()}
          className="ml-auto flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
        >
          {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />} Refresh
        </button>
      </div>

      <p className="text-xs text-text-muted">
        Work is recorded here before it runs, so closing the app never loses a job. A percentage is
        shown only when the engine reported one; otherwise the job shows its state.
      </p>

      {error && (
        <p className="flex items-start gap-2 rounded-lg border border-chip-red/40 bg-chip-red/10 p-2.5 text-[11px] text-text-primary">
          <AlertTriangle size={12} className="mt-0.5 shrink-0 text-chip-red" />
          <span className="flex-1">{error}</span>
        </p>
      )}
      {notice && !error && (
        <p className="flex items-start gap-2 rounded-lg border border-accent-green/40 bg-accent-green/10 p-2.5 text-[11px] text-text-primary">
          <CheckCircle2 size={12} className="mt-0.5 shrink-0 text-accent-green" />
          <span className="flex-1">{notice}</span>
        </p>
      )}

      {snapshot && counts && (
        <section className="rounded-xl border border-border bg-bg-secondary p-4">
          <div className="flex flex-wrap gap-1.5">
            {(Object.keys(counts) as QueueJobState[]).map(state => (
              <button
                key={state}
                type="button"
                onClick={() => setStateFilter(current => (current === state ? '' : state))}
                className={`rounded-md border px-2 py-1 text-[10px] ${
                  stateFilter === state ? STATE_STYLES[state] : 'border-border bg-bg-tertiary text-text-muted'
                }`}
              >
                {state} {counts[state]}
              </button>
            ))}
          </div>
          <div className="mt-3 grid gap-1.5 text-[11px] text-text-secondary">
            <div>
              Running now: {snapshot.summary.running} of {snapshot.summary.max_concurrent} allowed
              {snapshot.summary.waiting_seconds !== null
                ? ` · oldest waiting ${formatSeconds(snapshot.summary.waiting_seconds)}`
                : ''}
            </div>
            <div>
              Declared memory of running jobs: {snapshot.summary.declared_memory_running_mb} MB
              {snapshot.summary.memory_ceiling_mb
                ? ` of ${snapshot.summary.memory_ceiling_mb} MB ceiling`
                : ' (no ceiling set)'}
              {snapshot.summary.observed_system_memory_mb
                ? ` · this machine reports ${snapshot.summary.observed_system_memory_mb} MB`
                : ''}
            </div>
            <div className="text-text-muted">{snapshot.summary.progress_note}</div>
          </div>
        </section>
      )}

      <section className="rounded-xl border border-border bg-bg-secondary p-4">
        <h3 className="text-xs font-semibold text-text-primary">Runner</h3>
        <p className="mt-1 text-[11px] text-text-muted">
          A job runs only if a handler is registered for its engine on this machine. The runner is
          started by hand: nothing generates in the background unless you ask.
        </p>
        <div className="mt-2 text-[11px] text-text-secondary">
          {snapshot?.worker.running ? (
            <>
              background runner active · {snapshot.worker.ticks} tick(s) · last outcome{' '}
              {snapshot.worker.last_outcome || '—'}
            </>
          ) : (
            'background runner stopped'
          )}
          {snapshot && snapshot.handlers.length > 0
            ? ` · engines with a runner: ${snapshot.handlers.join(', ')}`
            : ' · no engine has a runner registered in this process'}
        </div>
        <div className="mt-2 flex flex-wrap gap-2">
          <button
            type="button"
            disabled={busy !== '' || Boolean(snapshot?.worker.running)}
            onClick={() =>
              void run('Starting the runner', () => startQueueWorker(2), result => {
                const started = (result as { started: boolean }).started
                return started ? 'Background runner started' : 'The runner was already running'
              })
            }
            className="flex items-center gap-1.5 rounded-lg bg-accent-warm px-2.5 py-1.5 text-[11px] font-medium text-black disabled:opacity-40"
          >
            <PlayCircle size={12} /> Start runner
          </button>
          <button
            type="button"
            disabled={busy !== '' || !snapshot?.worker.running}
            onClick={() => void run('Stopping the runner', () => stopQueueWorker())}
            className="flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
          >
            <Square size={11} /> Stop runner
          </button>
          <button
            type="button"
            disabled={busy !== ''}
            onClick={() =>
              void run('Running the queue', () => runQueue(5), result => {
                const report = result as { ran: number; outcomes: Record<string, number> }
                if (report.ran === 0) return 'Nothing was runnable'
                return `${report.ran} job(s): ${Object.entries(report.outcomes)
                  .map(([outcome, count]) => `${count} ${outcome}`)
                  .join(', ')}`
              })
            }
            className="flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
          >
            <PlayCircle size={12} /> Run queued work now
          </button>
          <button
            type="button"
            disabled={busy !== ''}
            onClick={() =>
              void run('Recovering', () => recoverQueue(), result => {
                const recovery = result as { requeued: string[]; failed: string[] }
                return recovery.requeued.length + recovery.failed.length === 0
                  ? 'Nothing was interrupted'
                  : `${recovery.requeued.length} job(s) returned to the queue, ${recovery.failed.length} out of retries`
              })
            }
            className="flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
          >
            <RotateCcw size={11} /> Recover interrupted work
          </button>
          <button
            type="button"
            disabled={busy !== ''}
            onClick={() =>
              void run('Cleaning history', () => pruneQueue(), result => {
                const pruned = result as { removed: string[] }
                return pruned.removed.length === 0
                  ? 'Nothing to clean'
                  : `${pruned.removed.length} finished job(s) removed from history`
              })
            }
            className="flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
          >
            <Trash2 size={11} /> Clean history
          </button>
          {busy && (
            <span className="flex items-center gap-1.5 self-center text-[11px] text-text-muted">
              <Loader2 size={12} className="animate-spin" /> {busy}
            </span>
          )}
        </div>
      </section>

      {settings && (
        <section className="rounded-xl border border-border bg-bg-secondary p-4">
          <h3 className="text-xs font-semibold text-text-primary">Limits</h3>
          <p className="mt-1 text-[11px] text-text-muted">
            Concurrency and the memory ceiling decide what may run at the same time. Memory is
            declared per job by the caller; it is not measured for you.
          </p>
          <form
            className="mt-2 grid gap-2 sm:grid-cols-2"
            onSubmit={event => {
              event.preventDefault()
              void run('Saving limits', () => updateQueueSettings(settings).then(result => {
                setSettings(result)
                return result
              }))
            }}
          >
            {(
              [
                ['max_concurrent', 'Concurrent jobs'],
                ['memory_ceiling_mb', 'Memory ceiling (MB, 0 = none)'],
                ['max_retries', 'Retries per job'],
                ['stale_after_seconds', 'Abandoned after (s)'],
                ['keep_terminal', 'Finished jobs kept'],
                ['event_limit', 'Event log size'],
              ] as Array<[keyof QueueSettings, string]>
            ).map(([key, label]) => (
              <label key={key} className="flex flex-col gap-1">
                <span className="text-[10px] text-text-muted">{label}</span>
                <input
                  type="number"
                  min={key === 'max_concurrent' ? 1 : 0}
                  step={key === 'stale_after_seconds' ? '30' : '1'}
                  value={settings[key] as number}
                  onChange={event =>
                    setSettings(current =>
                      current ? { ...current, [key]: Number(event.target.value) } : current,
                    )
                  }
                  className="rounded-lg border border-border bg-bg-tertiary px-2.5 py-2 text-xs text-text-primary focus:border-accent-warm focus:outline-none"
                />
              </label>
            ))}
            <button
              type="submit"
              disabled={busy !== ''}
              className="self-end rounded-lg border border-border px-3 py-2 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40 sm:col-span-2"
            >
              Save limits
            </button>
          </form>
        </section>
      )}

      <section className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-semibold text-text-primary">
            Jobs {stateFilter ? `· ${stateFilter}` : ''}
          </h3>
          {live.length > 0 && (
            <span className="text-[10px] text-text-muted">{live.length} in progress</span>
          )}
          {stateFilter && (
            <button
              type="button"
              onClick={() => setStateFilter('')}
              className="ml-auto text-[10px] text-text-muted hover:text-text-primary"
            >
              clear filter
            </button>
          )}
        </div>
        {jobs.length === 0 ? (
          <p className="rounded-xl border border-border bg-bg-secondary p-4 text-[11px] text-text-muted">
            No jobs queued. Shots, exports and processing appear here as soon as they are submitted.
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {jobs.map(job => (
              <JobCard
                key={job.job_id}
                job={job}
                onChanged={() => void refresh()}
                onError={message => setError(message)}
              />
            ))}
          </ul>
        )}
      </section>

      <section className="rounded-xl border border-border bg-bg-secondary p-4">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-semibold text-text-primary">Event log</h3>
          <button
            type="button"
            onClick={() =>
              void run('Loading the event log', () =>
                fetchQueueEvents('', 40).then(result => {
                  setEvents(result.events.slice().reverse())
                  return result
                }),
              )
            }
            className="ml-auto flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
          >
            <RefreshCw size={11} /> Load
          </button>
        </div>
        {events.length === 0 ? (
          <p className="mt-2 text-[11px] text-text-muted">
            Nothing loaded yet. The log records submissions, claims, state moves, retries, failures and
            cancellations — the queue's own trail.
          </p>
        ) : (
          <ul className="mt-2 max-h-64 space-y-1 overflow-y-auto">
            {events.map((event, index) => (
              <li key={`${event.at}-${index}`} className="flex gap-2 text-[10px]">
                <span className="shrink-0 text-text-muted">
                  {new Date(event.at * 1000).toLocaleTimeString()}
                </span>
                <span className="shrink-0 text-text-secondary">{event.kind}</span>
                <span className="min-w-0 flex-1 truncate text-text-muted" title={String(event.detail)}>
                  {event.job_id !== '*' ? `${event.job_id} · ` : ''}
                  {typeof event.detail === 'string' ? event.detail : JSON.stringify(event.detail)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
