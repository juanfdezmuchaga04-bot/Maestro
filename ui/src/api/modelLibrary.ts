// Model library: every model Maestro ships, every way to reach it, and downloads that
// work on any computer (see app/services/model_downloads.py).

export type Accelerator = 'cuda' | 'rocm' | 'mps' | 'directml' | 'vulkan' | 'cpu'

export interface LibraryRouteRef {
  id: string
  kind: string
  note: string
  confidence: 'official' | 'secondary' | 'unverified'
  same_weights: boolean
}

export interface LibraryItem {
  id: string
  kind: 'model' | 'llm' | 'extra'
  name: string
  modality: 'video' | 'image' | 'audio' | 'text'
  family: string
  family_label: string
  description: string
  visible: boolean
  run_here: string
  portable_here: boolean
  license_ack_required: string[]
  license: string
  license_url: string
  territory: string
  max_output: string
  free_api: boolean
  routes: LibraryRouteRef[]
  local_backends: string[]
  feasibility: { params_b: number | null; estimated_ram_gb: number | null; cpu: string; why: string; basis: string }
}

export interface RouteInfo {
  label: string
  kind: string
  limits: string
  account: string
  maestro: string
  source: string
  confidence: string
}

export interface LibraryResponse {
  hardware: { accelerator: Accelerator; basis: string }
  items: LibraryItem[]
  routes: Record<string, RouteInfo>
  route_kinds: Record<string, string>
  local_backends: Record<string, { label: string; hardware: string; maestro: string; source: string }>
  researched_on: string
}

export interface PlanFile {
  url: string
  target: string
  role: string
  repo: string
  path: string
  present: boolean
  availability?: string
  size?: number | null
  sha256?: string
}

export interface InspectReport {
  id: string
  notes: string[]
  files: PlanFile[]
  repos: Array<{ repo: string; status: string; license?: string; message?: string }>
  bytes_to_download: number
  files_to_download: number
  free_to_download: boolean
  blocked: string[]
}

export interface DownloadJob {
  id: string
  item: string
  name: string
  state: 'queued' | 'checking' | 'downloading' | 'succeeded' | 'failed' | 'cancelled'
  bytes_done: number
  bytes_total: number | null
  files_done: number
  files_total: number
  current: string
  error: { code: string; message: string } | null
  created_at: number
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    const detail = body?.detail
    throw new Error(typeof detail === 'object' && detail ? `${detail.message} [${detail.code}]` : String(detail || res.status))
  }
  return res.json() as Promise<T>
}

export function fetchLibrary(accelerator = ''): Promise<LibraryResponse> {
  return request(`/api/v1/model-library?accelerator=${encodeURIComponent(accelerator)}`)
}

export function inspectModel(body: { id: string; profile: string; accelerator: string }): Promise<InspectReport> {
  return request('/api/v1/model-library/inspect', { method: 'POST', body: JSON.stringify(body) })
}

export function startDownload(body: {
  id: string
  profile: string
  accelerator: string
  accept_license: boolean
}): Promise<DownloadJob> {
  return request('/api/v1/model-library/downloads', { method: 'POST', body: JSON.stringify(body) })
}

export function fetchDownloads(): Promise<{ jobs: DownloadJob[] }> {
  return request('/api/v1/model-library/downloads')
}

export function cancelDownload(jobId: string): Promise<DownloadJob> {
  return request(`/api/v1/model-library/downloads/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' })
}
