// HTTP client for the persistent production queue (/api/v1/production/queue).
//
// The queue is the record of what will run and what really happened; the
// backend decides both. This client never derives progress: a percentage only
// travels to the UI when an engine reported one.
import type {
  QueueCancelResult,
  QueueEvent,
  QueueJob,
  QueueRunReport,
  QueueSettings,
  QueueSnapshot,
} from '../types/queue'

const BASE = ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || `Request failed (${res.status})`)
  }
  return res.json() as Promise<T>
}

export function fetchQueue(options: {
  state?: string
  projectId?: string
  includeTerminal?: boolean
  limit?: number
} = {}): Promise<QueueSnapshot> {
  const search = new URLSearchParams()
  if (options.state) search.set('state', options.state)
  if (options.projectId) search.set('project_id', options.projectId)
  if (options.includeTerminal === false) search.set('include_terminal', 'false')
  if (options.limit) search.set('limit', String(options.limit))
  const suffix = search.toString()
  return request(`/api/v1/production/queue${suffix ? `?${suffix}` : ''}`)
}

export function fetchQueueSettings(): Promise<{
  settings: QueueSettings
  defaults: QueueSettings
  observed_system_memory_mb: number | null
}> {
  return request('/api/v1/production/queue/settings')
}

export function updateQueueSettings(patch: Partial<QueueSettings>): Promise<QueueSettings> {
  return request('/api/v1/production/queue/settings', {
    method: 'PUT',
    body: JSON.stringify(patch),
  })
}

export function submitQueueJob(job: {
  jobId?: string
  projectId?: string
  sceneId?: string
  shotId?: string
  task: string
  engine: string
  model?: string
  params?: Record<string, unknown>
  inputs?: string[]
  nodeId?: string
  priority?: number
  maxRetries?: number
  memoryMb?: number
  dedupeKey?: string
  progressSupported?: boolean
  cancelSupported?: boolean
  start?: 'now' | 'held'
}): Promise<{ created: boolean; job: QueueJob; reason: string }> {
  return request('/api/v1/production/queue/jobs', {
    method: 'POST',
    body: JSON.stringify({
      job_id: job.jobId,
      project_id: job.projectId,
      scene_id: job.sceneId,
      shot_id: job.shotId,
      task: job.task,
      engine: job.engine,
      model: job.model,
      params: job.params,
      inputs: job.inputs,
      node_id: job.nodeId,
      priority: job.priority,
      max_retries: job.maxRetries,
      memory_mb: job.memoryMb,
      dedupe_key: job.dedupeKey,
      progress_supported: job.progressSupported,
      cancel_supported: job.cancelSupported,
      start: job.start,
    }),
  })
}

export function fetchQueueJob(jobId: string, events = false): Promise<QueueJob> {
  return request(`/api/v1/production/queue/jobs/${jobId}${events ? '?events=true' : ''}`)
}

export function cancelQueueJob(jobId: string, reason = ''): Promise<QueueCancelResult> {
  return request(`/api/v1/production/queue/jobs/${jobId}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function acknowledgeQueueCancel(jobId: string): Promise<QueueJob> {
  return request(`/api/v1/production/queue/jobs/${jobId}/acknowledge-cancel`, { method: 'POST' })
}

export function retryQueueJob(jobId: string): Promise<QueueJob> {
  return request(`/api/v1/production/queue/jobs/${jobId}/retry`, { method: 'POST' })
}

/** Run what is claimable now, through the handlers registered on the backend. */
export function runQueue(limit = 1, worker = 'local'): Promise<QueueRunReport> {
  return request('/api/v1/production/queue/run', {
    method: 'POST',
    body: JSON.stringify({ limit, worker }),
  })
}

export function claimQueueJob(worker = 'local'): Promise<{
  claimed: QueueJob | null
  reason: string
  waiting: number | null
}> {
  return request('/api/v1/production/queue/claim', {
    method: 'POST',
    body: JSON.stringify({ worker }),
  })
}

export function recoverQueue(): Promise<{
  requeued: string[]
  failed: string[]
  abandoned_drafts: string[]
  detail: string
}> {
  return request('/api/v1/production/queue/recover', { method: 'POST' })
}

export function pruneQueue(): Promise<{ removed: string[]; kept: number }> {
  return request('/api/v1/production/queue/prune', { method: 'POST' })
}

export function fetchQueueEvents(
  jobId = '',
  limit = 100,
): Promise<{ events: QueueEvent[]; handlers: string[] }> {
  const search = new URLSearchParams({ limit: String(limit) })
  if (jobId) search.set('job_id', jobId)
  return request(`/api/v1/production/queue/events?${search.toString()}`)
}

export function startQueueWorker(interval = 2): Promise<{
  started: boolean
  running: boolean
  reason?: string
  handlers: string[]
}> {
  return request('/api/v1/production/queue/worker/start', {
    method: 'POST',
    body: JSON.stringify({ interval }),
  })
}

export function stopQueueWorker(): Promise<{ stopped: boolean; running: boolean; reason?: string }> {
  return request('/api/v1/production/queue/worker/stop', { method: 'POST' })
}
