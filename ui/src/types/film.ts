// Types for the Films workspace (AI production projects).
// Mirror services/film_projects.py, film_planning.py and film_assembly.py.

export type FilmMediaKind = 'image' | 'video' | 'audio' | 'document' | 'other'

export type FilmCreativeRole =
  | 'character_reference'
  | 'location_reference'
  | 'set_design'
  | 'wardrobe'
  | 'lighting'
  | 'palette'
  | 'camera_movement'
  | 'style_reference'
  | 'prop'
  | 'soundtrack'
  | 'source_music_keep'
  | 'character_voice'
  | 'narration_voice'
  | 'dialogue_audio'
  | 'sfx'
  | 'ambience'
  | 'source_video_keep'
  | 'source_video_edit'
  | 'script_document'
  | 'other'

export type FilmApproach = 'faithful' | 'creative' | 'hybrid'
export type FilmProjectStatus =
  | 'draft'
  | 'brief'
  | 'planned'
  | 'production'
  | 'assembly'
  | 'exported'

export interface FilmAssetAnalysis {
  available: boolean
  status: 'unknown' | 'ok' | 'partial' | 'unavailable'
  limitations: string[]
  duration: number
  width: number
  height: number
  fps: number
  has_audio: boolean
  size: number
  palette: string[]
  text_excerpt: string
  bpm: number
  sections: Array<{ start: number; end: number; label: string; energy: number }>
  beats: number[]
}

export interface FilmAssetConversion {
  target_format: string
  status: 'suggested' | 'running' | 'completed' | 'failed'
  command: string
  reason: string
  output_filename: string
}

export interface FilmAsset {
  id: string
  name: string
  filename: string
  display_name: string
  kind: FilmMediaKind
  role: FilmCreativeRole
  role_locked: boolean
  role_confidence: number
  role_reason: string
  origin: string
  notes: string
  tags: string[]
  reproduce_literally: boolean
  keep_original: boolean
  added_at: number
  analysis: FilmAssetAnalysis
  conversions: FilmAssetConversion[]
}

export interface FilmBrief {
  logline: string
  synopsis: string
  goal: string
  genre: string
  tone: string
  visual_style: string
  language: string
  audience: string
  aspect_ratio: string
  target_duration_sec: number
  reference_notes: string
  must_keep: string[]
  avoid: string[]
}

export interface FilmCharacter {
  id: string
  name: string
  display_name: string
  physical_description: string
  wardrobe: string
  voice_description: string
  reference_asset_ids: string[]
}

export interface FilmLocation {
  id: string
  name: string
  description: string
  lighting: string
  time_of_day: string
  reference_asset_ids: string[]
}

export interface FilmBible {
  characters: FilmCharacter[]
  locations: FilmLocation[]
  wardrobe: string[]
  props: string[]
  lighting: string
  palette: string[]
  style_notes: string
  continuity_rules: string[]
}

export interface FilmTake {
  index: number
  job_id: string
  filename: string
  status: string
  created_at: number
  error: string
}

export interface FilmShot {
  id: string
  index: number
  scene_id: string
  objective: string
  description: string
  shot_size: string
  camera_movement: string
  duration_sec: number
  transition: string
  start_image_asset_id: string
  reference_asset_ids: string[]
  elements: string[]
  dialogue: string
  music_notes: string
  sfx: string
  lighting: string
  video_prompt: string
  image_prompt: string
  model: string
  status: string
  approved: boolean
  approved_take_index: number | null
  output_filename: string
  takes: FilmTake[]
}

export interface FilmScene {
  id: string
  index: number
  title: string
  objective: string
  description: string
  location_id: string
  time_of_day: string
  mood: string
  music_cue: string
}

export interface FilmSoundtrackTrack {
  id: string
  asset_id: string
  target: 'music' | 'dialogue' | 'narration' | 'sfx' | 'ambience'
  label: string
  gain_db: number
  fade_in_sec: number
  fade_out_sec: number
  preserve: boolean
  muted: boolean
  start_sec: number
}

export interface FilmMusicBoundary {
  shot_id: string
  index: number
  planned_sec: number
  nearest_beat: number | null
  nearest_section: number | null
  delta_sec: number | null
  suggested_sec: number
}

export interface FilmMusicSync {
  available: boolean
  reason: string
  bpm: number
  sections: Array<{ start: number; end: number; label: string; energy: number }>
  boundaries: FilmMusicBoundary[]
}

export interface FilmJob {
  id: string
  shot_id: string
  kind: string
  status: string
  created_at: number
  updated_at: number
  output_filename: string
  error: string
}

export interface FilmDecision {
  at: number
  kind: string
  message: string
  data: Record<string, unknown>
}

export interface FilmExportRecord {
  filename: string
  created_at: number
  editor_project_id: string
  /** The render runs in the shared job queue and is tracked by its job id. */
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  error: string
  settings: Record<string, unknown>
}

export interface FilmProject {
  schema_version: number
  id: string
  name: string
  workspace: string
  created_at: number
  updated_at: number
  status: FilmProjectStatus
  approach: FilmApproach
  brief: FilmBrief
  assets: Record<string, FilmAsset>
  bible: FilmBible
  scenes: FilmScene[]
  shots: FilmShot[]
  soundtrack: {
    tracks: FilmSoundtrackTrack[]
    sync: FilmMusicSync
    master_gain_db: number
  }
  timeline: {
    editor_project_id: string
    last_assembled_at: number
    assembly_report: Record<string, unknown>
  }
  plan: {
    director_skill: string
    title: string
    global_style: string
    planned_at: number
    imported_from: string
  }
  jobs: FilmJob[]
  decisions: FilmDecision[]
  exports: FilmExportRecord[]
}

export interface FilmProjectSummary {
  id: string
  name: string
  workspace: string
  status: FilmProjectStatus
  approach: FilmApproach
  genre: string
  created_at: number
  updated_at: number
  asset_count: number
  scene_count: number
  shot_count: number
  generated_shot_count: number
  approved_shot_count: number
  planned_duration_sec: number
  editor_project_id: string
  export_count: number
}

export interface FilmReadinessIssue {
  code: string
  severity: 'blocking' | 'warning' | 'info'
  message: string
}

export interface FilmRequirement {
  tool: string
  why: string
  install: string
}

export interface FilmStoryboardEntry {
  id: string
  index: number
  scene_id: string
  scene_title: string
  objective: string
  description: string
  shot_size: string
  camera_movement: string
  duration_sec: number
  transition: string
  start_image_asset_id: string
  reference_asset_ids: string[]
  elements: string[]
  dialogue: string
  music_notes: string
  sfx: string
  lighting: string
  video_prompt: string
  image_prompt: string
  model: string
  status: string
  approved: boolean
  take_count: number
  output_filename: string
  start_time_sec: number
  end_time_sec: number
}

export interface FilmProductionRequest {
  shot_id: string
  index: number
  scene_id: string
  prompt: string
  image_prompt: string
  duration_sec: number
  shot_size: string
  camera_movement: string
  start_image_asset_id: string
  reference_asset_ids: string[]
  model: string
  status: string
  already_rendered: boolean
  skip: boolean
  skip_reason: string
}

export interface FilmEngineStatus {
  gpu: boolean
  ffmpeg: boolean
  ffprobe: boolean
  audio_analysis: boolean
  image_analysis: boolean
  pdf_text: boolean
  generation_possible: boolean
  detail: string
}

export interface FilmOverview {
  project: FilmProject
  summary: FilmProjectSummary
  brief: string
  storyboard: FilmStoryboardEntry[]
  storyboard_totals: {
    shot_count: number
    duration_sec: number
    approved: number
    with_prompt: number
    with_takes: number
    shots_without_prompt: string[]
  }
  soundtrack: { tracks: Array<FilmSoundtrackTrack & { volume: number }>; warnings: string[] }
  music_sync: FilmMusicSync
  readiness: FilmReadinessIssue[]
  engine: FilmEngineStatus
  requirements: FilmRequirement[]
  asset_roles: Record<string, FilmCreativeRole[]>
}

export interface FilmBriefBundle {
  brief: string
  reference_inputs: {
    reference_image_path: string | null
    character_ref_paths: string[]
    location_ref_paths: string[]
    unresolved_asset_names: string[]
    audio_file?: string
  }
  readiness: FilmReadinessIssue[]
  roles_present: string[]
}

export interface FilmAssemblyReport {
  report: {
    shots_in_timeline: number
    shots_missing: Array<{ shot_id: string; index: string; reason: string }>
    duration_sec: number
    audio_tracks: string[]
    has_subtitles: boolean
    has_titles: boolean
    music_sync_available: boolean
    music_sync_reason: string
    canvas: { width: number; height: number; fps: number }
  }
  readiness: FilmReadinessIssue[]
  edit_fraction: number
  music_sync: FilmMusicSync
}
