// HTTP client for the engine registry, compute nodes, reference URLs and
// chunked uploads. The studio never calls an inference engine directly: it asks
// this registry which capability is available, and by whom.
import type {
  ComputeAssignments,
  ComputeNodeInfo,
  ComputeNodeRegistration,
  ComfyProbeResult,
  ComfyTemplateDetail,
  EnginesStatus,
  UploadLimits,
  UploadSessionState,
  UploadedReference,
} from '../types/engines'

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

// --- Engines ---------------------------------------------------------------

export function fetchEngines(): Promise<EnginesStatus> {
  return request('/api/v1/engines')
}

export function probeComfyUi(baseUrl: string, timeout?: number): Promise<ComfyProbeResult> {
  return request('/api/v1/engines/comfyui/probe', {
    method: 'POST',
    body: JSON.stringify({ base_url: baseUrl, timeout }),
  })
}

export function fetchComfyTemplates(): Promise<{
  templates_dir: string
  templates: ComfyTemplateDetail[]
}> {
  return request('/api/v1/engines/comfyui/templates')
}

export interface ComfyRunState {
  prompt_id: string
  state: string
  completed: boolean
  results?: {
    files: Array<{ node_id: string; kind: string; filename: string; subfolder: string; type: string }>
    status: string
    completed: boolean
    view_url: string
  }
}

/** Submit one versioned template to a ComfyUI server. */
export function runComfyTemplate(
  baseUrl: string,
  templateId: string,
  params: Record<string, unknown>,
): Promise<{ prompt_id: string; template_id: string; template_version: string; capabilities: string[] }> {
  return request('/api/v1/engines/comfyui/run', {
    method: 'POST',
    body: JSON.stringify({ base_url: baseUrl, template_id: templateId, params }),
  })
}

/** Real state, read from ComfyUI's own queue and history. */
export function fetchComfyRun(baseUrl: string, promptId: string): Promise<ComfyRunState> {
  const search = new URLSearchParams({ base_url: baseUrl })
  return request(`/api/v1/engines/comfyui/run/${promptId}?${search.toString()}`)
}

export function cancelComfyRun(
  baseUrl: string,
  promptId: string,
): Promise<{ action: string; state_after: string; cancelled: boolean; note?: string }> {
  return request(`/api/v1/engines/comfyui/run/${promptId}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ base_url: baseUrl }),
  })
}

// --- Compute nodes ---------------------------------------------------------

export function fetchComputeNodes(): Promise<{
  nodes: ComputeNodeInfo[]
  summary: ComputeAssignments['summary']
}> {
  return request('/api/v1/compute/nodes')
}

export function registerComputeNode(params: {
  nodeId: string
  label?: string
  kind?: string
  capabilities: string[]
  models?: string[]
  address?: string
  notes?: string
  maxConcurrentJobs?: number
  local?: boolean
}): Promise<ComputeNodeRegistration> {
  return request('/api/v1/compute/nodes', {
    method: 'POST',
    body: JSON.stringify({
      node_id: params.nodeId,
      label: params.label,
      kind: params.kind,
      capabilities: params.capabilities,
      models: params.models,
      address: params.address,
      notes: params.notes,
      max_concurrent_jobs: params.maxConcurrentJobs,
      local: params.local,
    }),
  })
}

export function updateComputeNode(
  nodeId: string,
  patch: { capabilities?: string[]; models?: string[]; label?: string; notes?: string; address?: string },
): Promise<ComputeNodeInfo> {
  return request(`/api/v1/compute/nodes/${nodeId}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })
}

export function removeComputeNode(nodeId: string): Promise<{ removed: string; parked_jobs: number }> {
  return request(`/api/v1/compute/nodes/${nodeId}`, { method: 'DELETE' })
}

export function recoverComputeNode(
  nodeId: string,
): Promise<{ node_id: string; count: number; resumed: Array<Record<string, unknown>>; detail: string }> {
  return request(`/api/v1/compute/nodes/${nodeId}/recover`, { method: 'POST' })
}

export function fetchComputeAssignments(): Promise<ComputeAssignments> {
  return request('/api/v1/compute/assignments')
}

// --- Reference URLs --------------------------------------------------------

export function validateReferenceUrl(
  url: string,
  allowPrivate = false,
): Promise<{ url: string; host: string; port: number; scheme: string; addresses: string[]; importable: boolean }> {
  return request('/api/v1/reference/url/validate', {
    method: 'POST',
    body: JSON.stringify({ url, allow_private: allowPrivate }),
  })
}

export function importReferenceUrl(params: {
  url: string
  projectId?: string
  role?: string
  name?: string
  workspace?: string
  analyze?: boolean
  allowPrivate?: boolean
}): Promise<{ reference: UploadedReference; asset: Record<string, unknown> | null }> {
  return request('/api/v1/reference/url/import', {
    method: 'POST',
    body: JSON.stringify({
      url: params.url,
      project_id: params.projectId,
      role: params.role,
      name: params.name,
      workspace: params.workspace,
      analyze: params.analyze,
      allow_private: params.allowPrivate,
    }),
  })
}

// --- Chunked uploads -------------------------------------------------------

export function fetchUploadLimits(): Promise<UploadLimits> {
  return request('/api/v1/uploads/limits')
}

export function createUploadSession(params: {
  filename: string
  totalBytes: number
  contentType?: string
  sha256?: string
  chunkSize?: number
}): Promise<UploadSessionState> {
  return request('/api/v1/uploads/sessions', {
    method: 'POST',
    body: JSON.stringify({
      filename: params.filename,
      total_bytes: params.totalBytes,
      content_type: params.contentType,
      sha256: params.sha256,
      chunk_size: params.chunkSize,
    }),
  })
}

export function fetchUploadSession(sessionId: string): Promise<UploadSessionState> {
  return request(`/api/v1/uploads/sessions/${sessionId}`)
}

/**
 * Send one chunk. The body is raw bytes, so this bypasses the JSON helper and
 * the server enforces the per-session size cap while streaming.
 */
export async function uploadChunk(
  sessionId: string,
  index: number,
  data: Blob,
  options: { sha256?: string; signal?: AbortSignal } = {},
): Promise<UploadSessionState> {
  const search = new URLSearchParams({ index: String(index) })
  if (options.sha256) search.set('sha256', options.sha256)
  const res = await fetch(`${BASE}/api/v1/uploads/sessions/${sessionId}/chunks?${search.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: data,
    signal: options.signal,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || `Chunk upload failed (${res.status})`)
  }
  return res.json() as Promise<UploadSessionState>
}

export function completeUploadSession(
  sessionId: string,
  dedup = true,
): Promise<UploadSessionState> {
  return request(`/api/v1/uploads/sessions/${sessionId}/complete`, {
    method: 'POST',
    body: JSON.stringify({ dedup }),
  })
}

export function cancelUploadSession(
  sessionId: string,
  keepRecord = true,
): Promise<UploadSessionState> {
  return request(`/api/v1/uploads/sessions/${sessionId}?keep_record=${keepRecord}`, {
    method: 'DELETE',
  })
}

export function cleanupUploadSessions(
  options: { olderThan?: number; includeOpen?: boolean } = {},
): Promise<{ removed: string[]; freed_bytes: number }> {
  return request('/api/v1/uploads/sessions/cleanup', {
    method: 'POST',
    body: JSON.stringify({ older_than: options.olderThan, include_open: options.includeOpen }),
  })
}
