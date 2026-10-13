/** Response shapes for the persistent production queue (/api/v1/production/queue). */

export type QueueJobState =
  | 'draft'
  | 'pending'
  | 'preparing'
  | 'running'
  | 'finalizing'
  | 'completed'
  | 'failed'
  | 'cancelled'

/**
 * Progress as the engine reported it. `percent` is `null` — never 0 — when no
 * engine reported a percentage, so the UI shows the job's state instead of a
 * bar nobody measured.
 */
export interface QueueProgress {
  percent: number | null
  step: number | null
  total_steps: number | null
  source: 'reported' | 'none' | string
  note: string
  reported_at: number | null
}

export interface QueueJob {
  job_id: string
  project_id: string
  scene_id: string
  shot_id: string
  task: string
  engine: string
  model: string
  params: Record<string, unknown>
  inputs: string[]
  node_id: string
  state: QueueJobState
  priority: number
  attempts: number
  max_retries: number
  memory_mb: number
  dedupe_key: string
  progress_supported: boolean
  cancel_supported: boolean
  cancel_requested: boolean
  progress: QueueProgress
  created_at: number | null
  updated_at: number | null
  started_at: number | null
  finished_at: number | null
  duration_seconds: number | null
  available_at: number | null
  worker: string
  external_job_id: string
  outputs: string[]
  errors: string[]
  events: Array<{ at: number; kind: string; detail: string }>
}

export interface QueueSummary {
  counts: Record<QueueJobState, number>
  settings: QueueSettings
  running: number
  max_concurrent: number
  declared_memory_running_mb: number
  memory_ceiling_mb: number
  observed_system_memory_mb: number | null
  waiting_seconds: number | null
  progress_note: string
}

export interface QueueSettings {
  max_concurrent: number
  memory_ceiling_mb: number
  max_retries: number
  auto_retry: boolean
  stale_after_seconds: number
  retry_delay_seconds: number
  keep_terminal: number
  event_limit: number
}

export interface QueueWorkerStatus {
  running: boolean
  ticks: number
  last_tick: number
  last_outcome: string
  last_error: string
  interval_seconds: number
  worker: string
  handlers: string[]
}

export interface QueueSnapshot {
  summary: QueueSummary
  jobs: QueueJob[]
  worker: QueueWorkerStatus
  handlers: string[]
  progress_note: string
}

export interface QueueEvent {
  at: number
  kind: string
  job_id: string
  detail: Record<string, unknown> | string
}

export interface QueueCancelResult {
  job: QueueJob
  /** True only when the job really stopped. A request is not a cancellation. */
  cancelled: boolean
  action: 'none' | 'cancelled_before_start' | 'cancelled_while_running' | 'requested' | string
  reason: string
}

export interface QueueRunReport {
  ran: number
  outcomes: Record<string, number>
  reports: Array<{
    claimed: boolean
    outcome: string
    job_id: string
    reason: string
    outputs?: string[]
    state?: string
  }>
}
