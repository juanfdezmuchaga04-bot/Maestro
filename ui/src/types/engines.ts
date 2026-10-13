/** Response shapes for the engine registry, compute nodes and uploads API. */

export interface EngineProviderInfo {
  provider_id: string
  label: string
  kind: string
  capabilities: string[]
  local: boolean
  requires_api_key: boolean
  node_id: string
  models: string[]
  notes: string
  detail: Record<string, unknown>
}

export interface EngineCapabilityInfo {
  id: string
  label: string
  task: string
  media: string
  needs_gpu: boolean
}

export interface EngineAvailability {
  capability: string
  label: string
  task: string
  needs_gpu: boolean
  available: boolean
  providers: string[]
}

export interface LocalEngineInfo {
  capabilities: string[]
  /** capability -> the models/tools that justified it on this machine */
  evidence: Record<string, string[]>
  generation_possible: boolean
  tools: Record<string, unknown>
}

export interface ComputeNodeInfo {
  node_id: string
  label: string
  kind: string
  local: boolean
  state: 'online' | 'offline'
  online: boolean
  last_seen: number | null
  seconds_since_seen: number | null
  offline_after_seconds: number
  capabilities: string[]
  hardware: Record<string, unknown>
  storage: Record<string, unknown>
  models: string[]
  max_concurrent_jobs: number
  notes: string
  address: string
  created_at: number | null
}

export interface ComputeSummary {
  nodes: ComputeNodeInfo[]
  node_count: number
  online_count: number
  local_online: boolean
  active_assignments: number
  waiting_for_node: number
  capabilities_online: string[]
}

export interface EnginesStatus {
  providers: EngineProviderInfo[]
  capabilities: EngineCapabilityInfo[]
  availability: EngineAvailability[]
  local_only: boolean
  local_engine: LocalEngineInfo
  compute: ComputeSummary
}

export interface ComfyTemplateInfo {
  template_id: string
  version: string
  label: string
  capabilities: string[]
  missing_node_classes: string[]
}

export interface ComfyProbeResult {
  reachable: boolean
  base_url: string
  label: string
  comfyui_version?: string | null
  devices: Array<{ name: string; type: string; vram_total?: number | null; vram_free?: number | null }>
  object_info_nodes: number
  models: string[]
  capabilities: string[]
  templates: ComfyTemplateInfo[]
  unsupported_templates: ComfyTemplateInfo[]
  notes: string
}

/** One declared input of a workflow template, straight from its manifest. */
export interface ComfyTemplateInput {
  node?: string
  field?: string
  required?: boolean
  type?: string
  default?: unknown
}

export interface ComfyTemplateDetail {
  template_id: string
  version: string
  label: string
  capabilities: string[]
  inputs: Record<string, ComfyTemplateInput>
  outputs: Record<string, unknown>
  node_classes: string[]
  notes: string
}

export interface ComputeNodeRegistration extends ComputeNodeInfo {
  /** Returned exactly once, at registration. It is never shown again. */
  token: string
  token_notice: string
}

export interface ComputeAssignments {
  summary: ComputeSummary
  parked: Array<{ job_id: string; node_id: string; state: string; capability?: string }>
}

export interface UploadLimits {
  limits: Record<string, number>
  chunk_size: number
  min_chunk_size: number
  max_chunk_size: number
  reserve_bytes: number
  note: string
}

export interface UploadSessionState {
  session_id: string
  filename: string
  kind: string
  mode: 'chunked' | 'streaming'
  state: string
  chunk_size: number
  chunks_total: number | null
  received_chunks: number
  received_bytes: number
  missing_chunks: number[]
  percent_complete: number | null
  total_bytes: number
  limit_bytes: number
  expected_sha256: string
  created_at: number | null
  updated_at: number | null
  final_filename: string | null
  final_path: string | null
  sha256: string | null
  deduplicated: boolean | null
  error: string
}

export interface UploadedReference {
  filename: string
  stored_name: string
  path: string
  kind: string
  bytes: number
  sha256: string
  content_type: string
  deduplicated: boolean
  source_url: string
  final_url: string
  note: string
}
