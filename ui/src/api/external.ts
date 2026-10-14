// HTTP client for opt-in external providers (Cloudflare Workers AI, Replicate).
// Nothing here can start a paid job on its own: a job needs the confirm token of
// a disclosure the user reviewed (see ExternalWorkspace).

export type ExternalModality = 'image' | 'video'

export interface ExternalCredentialInfo {
  field: string
  label: string
  secret: boolean
  set: boolean
  source: string
  hint: string
  env_var: string
}

export interface ExternalModelInfo {
  model: string
  label: string
  license?: string
  limits: Record<string, unknown>
  timeout_s: number
  price_basis?: string
  price_url?: string
}

export interface ExternalProviderInfo {
  id: string
  label: string
  modalities: ExternalModality[]
  account: string
  billing: string
  free_quota: string
  retention: string
  cancel: 'remote' | 'local_only'
  cancel_note: string
  docs: string[]
  terms: string
  signup: string
  models: Partial<Record<ExternalModality, ExternalModelInfo>>
  extra_models?: Record<string, ExternalModelInfo & { modality: ExternalModality }>
  credentials: ExternalCredentialInfo[]
  enabled: boolean
  configured: boolean
  verified_on: string
}

export interface ExternalProvidersResponse {
  providers: ExternalProviderInfo[]
  storage: { kind: string; label: string }
  policy: string
}

export interface ExternalCost {
  amount_usd: number
  currency: string
  free: boolean
  neurons?: number
  summary: string
  basis: string
  source: string
}

export interface ExternalDisclosure {
  provider: string
  provider_id: string
  model: string
  model_label: string
  model_license: string
  modality: ExternalModality
  endpoint_host: string
  data_sent: string[]
  not_sent: string
  cost: ExternalCost
  account: string
  billing: string
  retention: string
  limits: Record<string, unknown>
  timeout_s: number
  cancel: 'remote' | 'local_only'
  cancel_note: string
  terms: string
  docs: string[]
  verified_on: string
  duration_s?: number
}

export interface ExternalQuote {
  request: Record<string, unknown>
  disclosure: ExternalDisclosure
  confirm_token: string
}

export type ExternalJobState =
  | 'queued'
  | 'submitting'
  | 'running'
  | 'downloading'
  | 'cancelling'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export interface ExternalJob {
  id: string
  provider: string
  provider_label: string
  modality: ExternalModality
  request: Record<string, unknown> & { prompt: string }
  disclosure: ExternalDisclosure
  state: ExternalJobState
  progress: string
  created_at: number
  updated_at: number
  finished_at?: number
  remote_id: string
  error: { code: string; message: string; http_status?: number; retry_after?: number } | null
  result: { filename: string; workspace: string; bytes: number; kind: string; url: string; sha256: string } | null
  notes: string[]
  can_cancel: boolean
}

export class ExternalApiError extends Error {
  code: string
  status: number
  constructor(message: string, code: string, status: number) {
    super(message)
    this.code = code
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    const detail = body?.detail
    if (detail && typeof detail === 'object') {
      throw new ExternalApiError(String(detail.message || `Request failed (${res.status})`), String(detail.code || ''), res.status)
    }
    throw new ExternalApiError(String(detail || `Request failed (${res.status})`), '', res.status)
  }
  return res.json() as Promise<T>
}

export function fetchExternalProviders(): Promise<ExternalProvidersResponse> {
  return request('/api/v1/external/providers')
}

export function updateExternalProvider(
  providerId: string,
  body: { enabled?: boolean; credentials?: Record<string, string> },
): Promise<ExternalProvidersResponse> {
  return request(`/api/v1/external/providers/${encodeURIComponent(providerId)}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export function clearExternalCredentials(providerId: string): Promise<ExternalProvidersResponse> {
  return request(`/api/v1/external/providers/${encodeURIComponent(providerId)}/credentials`, { method: 'DELETE' })
}

export function testExternalProvider(providerId: string): Promise<{ ok: boolean; message: string; note?: string }> {
  return request(`/api/v1/external/providers/${encodeURIComponent(providerId)}/test`, { method: 'POST' })
}

export function quoteExternalJob(params: Record<string, unknown>): Promise<ExternalQuote> {
  return request('/api/v1/external/quote', { method: 'POST', body: JSON.stringify(params) })
}

export function submitExternalJob(params: Record<string, unknown>, confirmToken: string): Promise<ExternalJob> {
  return request('/api/v1/external/jobs', {
    method: 'POST',
    body: JSON.stringify({ params, confirm_token: confirmToken }),
  })
}

export function fetchExternalJobs(): Promise<{ jobs: ExternalJob[] }> {
  return request('/api/v1/external/jobs')
}

export function cancelExternalJob(jobId: string): Promise<ExternalJob> {
  return request(`/api/v1/external/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' })
}
