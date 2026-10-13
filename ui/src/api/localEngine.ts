// HTTP client for the local, free-per-generation engine (/api/v1/engines/local/*).
//
// This is the path that keeps the app independent of any paid service: it
// measures this computer, plans a self-hosted ComfyUI install, proves it with a
// real clip, and only then registers the engine.
import type {
  FunctionalTestResult,
  InstallResult,
  LocalEngineHardwareResponse,
  LocalQueueJobResult,
  LocalSetupPlan,
} from '../types/localEngine'

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

/** What this computer really is, what fits, and what was evaluated and rejected. */
export function fetchLocalHardware(): Promise<LocalEngineHardwareResponse> {
  return request('/api/v1/engines/local/hardware')
}

/** What installing the local engine would do. Read-only: nothing is executed. */
export function fetchLocalSetup(options: { modelId?: string; comfyRoot?: string } = {}): Promise<LocalSetupPlan> {
  const search = new URLSearchParams()
  if (options.modelId) search.set('model_id', options.modelId)
  if (options.comfyRoot) search.set('comfy_root_path', options.comfyRoot)
  const suffix = search.toString()
  return request(`/api/v1/engines/local/setup${suffix ? `?${suffix}` : ''}`)
}

/**
 * Run the install on this computer. `confirm` is mandatory, and the model
 * weights are only fetched when `downloadModels` is explicitly true.
 */
export function installLocalEngine(options: {
  modelId?: string
  confirm: boolean
  downloadModels?: boolean
  comfyRoot?: string
  port?: number
}): Promise<InstallResult> {
  return request('/api/v1/engines/local/setup/install', {
    method: 'POST',
    body: JSON.stringify({
      model_id: options.modelId,
      confirm: options.confirm,
      download_models: options.downloadModels ?? false,
      comfy_root: options.comfyRoot,
      port: options.port,
    }),
  })
}

/** Generate one deliberately tiny clip and bring the file back. */
export function runLocalFunctionalTest(options: {
  baseUrl?: string
  modelId?: string
  prompt?: string
  pollTimeout?: number
} = {}): Promise<FunctionalTestResult> {
  return request('/api/v1/engines/local/functional-test', {
    method: 'POST',
    body: JSON.stringify({
      base_url: options.baseUrl,
      model_id: options.modelId,
      prompt: options.prompt,
      poll_timeout: options.pollTimeout,
    }),
  })
}

/** Register the engine. The backend refuses unless a real clip came back. */
export function registerLocalEngine(options: { baseUrl?: string; modelId?: string; label?: string } = {}): Promise<{
  registered: boolean
  reason?: string
  functional_test_required?: boolean
  node?: Record<string, unknown>
  verified_by?: Record<string, unknown>
}> {
  return request('/api/v1/engines/local/register', {
    method: 'POST',
    body: JSON.stringify({ base_url: options.baseUrl, model_id: options.modelId, label: options.label }),
  })
}

/** Queue a real generation. Works with the computer switched off: the job waits. */
export function queueLocalClip(options: {
  prompt: string
  modelId?: string
  baseUrl?: string
  nodeId?: string
  projectId?: string
  shotId?: string
  seconds?: number
  width?: number
  height?: number
  steps?: number
  seed?: number
  priority?: number
  dedupeKey?: string
}): Promise<LocalQueueJobResult> {
  return request('/api/v1/engines/local/queue-job', {
    method: 'POST',
    body: JSON.stringify({
      prompt: options.prompt,
      model_id: options.modelId,
      base_url: options.baseUrl,
      node_id: options.nodeId,
      project_id: options.projectId,
      shot_id: options.shotId,
      seconds: options.seconds,
      width: options.width,
      height: options.height,
      steps: options.steps,
      seed: options.seed,
      priority: options.priority,
      dedupe_key: options.dedupeKey,
    }),
  })
}
