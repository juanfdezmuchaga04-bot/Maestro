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
  PublicConnectResult,
  PublicProviderInfo,
  PublicProvidersResponse,
  RemotePeerInfo,
  RemotePeersResponse,
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

// --- Remote peers (another Maestro with a GPU) -----------------------------
// A peer is reached on its own production queue with a bearer token, so its
// capabilities are declared here or measured from its own report — never guessed.
// Jobs keep running on this machine's durable queue while that computer is off.

export function fetchRemotePeers(): Promise<RemotePeersResponse> {
  return request('/api/v1/engines/remote/peers')
}

export function addRemotePeer(params: {
  peerId: string
  label?: string
  baseUrl: string
  token?: string
  engine?: string
  capabilities?: string[]
  notes?: string
}): Promise<{
  peer: RemotePeerInfo
  probe: Record<string, unknown> | null
  online: boolean
  engines: string[]
  registered: string[]
  next_step: string
}> {
  return request('/api/v1/engines/remote/peers', {
    method: 'POST',
    body: JSON.stringify({
      peer_id: params.peerId,
      label: params.label,
      base_url: params.baseUrl,
      token: params.token,
      engine: params.engine,
      capabilities: params.capabilities,
      notes: params.notes,
    }),
  })
}

export function probeRemotePeer(
  peerId: string,
  timeout?: number,
): Promise<{ peer_id: string; online: boolean; probe: Record<string, unknown>; peer: RemotePeerInfo }> {
  return request(`/api/v1/engines/remote/peers/${peerId}/probe`, {
    method: 'POST',
    body: JSON.stringify({ timeout }),
  })
}

export function removeRemotePeer(peerId: string): Promise<{ removed: string; note: string }> {
  return request(`/api/v1/engines/remote/peers/${peerId}`, { method: 'DELETE' })
}

// --- Public compute (a free engine on the internet) ------------------------
// Nothing here needs configuring: Maestro finds a running public Space on its own
// and keeps the connection honest on a timer. These calls are how the panel shows
// what it connected to, and how someone points it at one Space by hand.

export function fetchPublicProviders(): Promise<PublicProvidersResponse> {
  return request('/api/v1/engines/public/providers')
}

/** Look for free public engines now, instead of waiting for the next check. */
export function connectPublicCloud(
  options: { capabilities?: string[]; candidates?: number } = {},
): Promise<PublicConnectResult> {
  return request('/api/v1/engines/public/connect', {
    method: 'POST',
    body: JSON.stringify({
      capabilities: options.capabilities,
      candidates: options.candidates,
    }),
  })
}

export function probePublicProvider(
  providerId: string,
): Promise<{ provider: PublicProviderInfo; probe: Record<string, unknown> }> {
  return request(`/api/v1/engines/public/providers/${encodeURIComponent(providerId)}/probe`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function addPublicSpace(params: {
  space: string
  capability: string
}): Promise<{ provider: PublicProviderInfo; probe: Record<string, unknown> }> {
  return request('/api/v1/engines/public/spaces', {
    method: 'POST',
    body: JSON.stringify({ space: params.space, capability: params.capability }),
  })
}

export function forgetPublicProvider(providerId: string): Promise<{ removed: string; note: string }> {
  return request(`/api/v1/engines/public/providers/${encodeURIComponent(providerId)}`, {
    method: 'DELETE',
  })
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
