/** Shapes for the local, free-per-generation engine (/api/v1/engines/local/*). */

export interface LocalGpu {
  index: number | null
  name: string
  vram_total_mb: number
  driver_version: string
  compute_capability: string
}

export interface LocalHardware {
  detected_at: number
  os: { system: string; release: string; machine: string; python: string; executable: string }
  cpu: { logical_cores: number | null; model: string }
  memory: { total_mb: number | null; total_gb: number | null }
  disk: { path: string; free_gb: number | null; total_gb: number | null }
  gpus: LocalGpu[]
  primary_gpu: LocalGpu | null
  vram_total_mb: number | null
  tools: Record<string, string>
  problems: string[]
  unmeasured: string[]
  tier: { tier: string; vram_total_mb: number | null; note: string }
  cuda: { available: boolean; reason: string }
}

export interface LocalModelFile {
  filename: string
  path_in_repo: string
  directory: string
  size_gb: number
  repo: string
  url: string
}

export interface LocalModel {
  model_id: string
  label: string
  licence: string
  licence_url: string
  commercial_note: string
  capability: string
  templates: string[]
  files: LocalModelFile[]
  template_params: Record<string, string>
  min_vram_mb: number
  vram_source: string
  min_ram_gb: number | null
  ram_is_estimate: boolean
  model_disk_gb: number
  recommended_free_disk_gb: number
  resolution: string
  fps: number
  frames: number
  notes: string
  sources: string[]
  selectable: boolean
}

export interface LocalModelFit {
  model_id: string
  label?: string
  fits: boolean
  blockers: string[]
  unknowns: string[]
  measured: { vram_total_mb: number | null; memory_total_gb: number | null; free_disk_gb: number | null }
  required: {
    min_vram_mb: number
    vram_floor_mb?: number
    min_ram_gb: number | null
    model_disk_gb: number
    recommended_free_disk_gb: number
  }
}

export interface LocalEngineSelection {
  model: LocalModel | null
  fit: LocalModelFit | Record<string, never>
  considered: LocalModelFit[]
  policy: LocalEnginePolicy
  blockers?: string[]
  unknowns?: string[]
  detail: string
}

export interface LocalEnginePolicy {
  commercial_providers: boolean
  cloud_generation: boolean
  credits: boolean
  generation_cost_per_clip: string
  note: string
}

export interface EvaluatedModel {
  model_id: string
  label: string
  local_and_free: boolean
  selected: boolean
  licence: string
  licence_url: string
  reasons: string[]
  credits: string
  sources: string[]
  when_it_would_be_selected: string
}

export interface LocalEngineHardwareResponse {
  hardware: LocalHardware
  selection: LocalEngineSelection
  catalogue: LocalModel[]
  evaluated_but_not_selected: EvaluatedModel[]
  policy: LocalEnginePolicy
  engine: { kind: string; default_url: string; api: string; cloud: boolean }
}

export interface SetupDownload {
  filename: string
  directory: string
  size_gb: number
  url: string
  path_in_repo: string
  repo: string
}

export interface SetupStep {
  id: string
  label: string
  command: string[] | null
  cwd: string | null
  why: string
  already_present?: boolean
  requires_flag?: string
  manual_step?: boolean
  long_running?: boolean
  action?: string
  downloads?: SetupDownload[]
}

export interface LocalSetupPlan {
  comfy_root: string
  models_root: string
  base_url: string
  python: string
  venv_python: string
  model: LocalModel
  policy: LocalEnginePolicy
  steps: SetupStep[]
  downloads: SetupDownload[]
  download_gb: number
  disk_required_gb: number
  disk_free_gb: number | null
  blockers: string[]
  workflow: string
  notes: string
  present?: { comfyui_checkout: boolean; venv: boolean }
}

export interface InstallStepResult {
  id: string
  status: string
  detail: string
  command?: string[]
  exit_code?: number
  files?: Array<{ filename: string; status: string; bytes?: number; reason?: string; path?: string }>
}

export interface InstallResult {
  started: boolean
  completed: boolean
  steps: InstallStepResult[]
  reason?: string
  next?: string
}

export interface FunctionalTestResult {
  passed: boolean
  base_url: string
  model_id: string
  template_id: string
  prompt_id?: string
  clip_path: string
  clip_filename?: string
  clip_url?: string
  bytes?: number
  container?: string
  sha256?: string
  seconds?: number
  state?: string
  reason: string
  note?: string
  checked: Record<string, unknown>
}

export interface LocalQueueJobResult {
  created: boolean
  job: { job_id: string; state: string } & Record<string, unknown>
  reason: string
  engine: string
  note: string
}
