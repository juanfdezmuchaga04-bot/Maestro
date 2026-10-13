// HTTP client for the Films workspace (/api/v1/film/*).
import type {
  FilmAssemblyReport,
  FilmAsset,
  FilmBrief,
  FilmBriefBundle,
  FilmEngineStatus,
  FilmOverview,
  FilmProductionRequest,
  FilmProject,
  FilmProjectSummary,
  FilmReadinessIssue,
  FilmStoryboardEntry,
} from '../types/film'

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

const query = (params: Record<string, string | boolean | undefined>) => {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === '') continue
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export function fetchFilmEngine(): Promise<FilmEngineStatus> {
  return request('/api/v1/film/engine')
}

export function fetchFilmProjects(workspace?: string): Promise<{ projects: FilmProjectSummary[] }> {
  return request(`/api/v1/film/projects${query({ workspace })}`)
}

export function createFilmProject(params: {
  name: string
  workspace?: string
  approach?: string
  brief?: Partial<FilmBrief>
}): Promise<FilmProject> {
  return request('/api/v1/film/projects', { method: 'POST', body: JSON.stringify(params) })
}

export function saveFilmProject(project: FilmProject, workspace?: string): Promise<FilmProject> {
  return request(`/api/v1/film/projects/${project.id}`, {
    method: 'PUT',
    body: JSON.stringify({ workspace, project }),
  })
}

export function deleteFilmProject(projectId: string, workspace?: string): Promise<{ deleted: string }> {
  return request(`/api/v1/film/projects/${projectId}${query({ workspace })}`, { method: 'DELETE' })
}

export function fetchFilmOverview(projectId: string, workspace?: string): Promise<FilmOverview> {
  return request(`/api/v1/film/projects/${projectId}/overview${query({ workspace })}`)
}

export function fetchFilmBrief(projectId: string, workspace?: string): Promise<FilmBriefBundle> {
  return request(`/api/v1/film/projects/${projectId}/brief${query({ workspace })}`)
}

export function updateFilmBrief(
  projectId: string,
  patch: Partial<FilmBrief> & { approach?: string },
  workspace?: string,
): Promise<FilmProject> {
  return request(`/api/v1/film/projects/${projectId}/brief`, {
    method: 'PUT',
    body: JSON.stringify({ workspace, brief: patch, approach: patch.approach }),
  })
}

export function importFilmAssets(
  projectId: string,
  files: Array<{ filename: string; name?: string; role?: string; path?: string }>,
  options: { analyze?: boolean; deepAudio?: boolean; workspace?: string } = {},
): Promise<{ project: FilmProject; imported: FilmAsset[]; rejected: Array<{ filename: string; reason: string }> }> {
  return request(`/api/v1/film/projects/${projectId}/assets`, {
    method: 'POST',
    body: JSON.stringify({
      workspace: options.workspace,
      files,
      analyze: options.analyze,
      deep_audio: options.deepAudio,
    }),
  })
}

export function updateFilmAsset(
  projectId: string,
  assetId: string,
  patch: {
    role?: string
    notes?: string
    display_name?: string
    reproduce_literally?: boolean
    keep_original?: boolean
    workspace?: string
  },
): Promise<FilmProject> {
  return request(`/api/v1/film/projects/${projectId}/assets/${assetId}`, {
    method: 'PUT',
    body: JSON.stringify(patch),
  })
}

export function deleteFilmAsset(projectId: string, assetId: string, workspace?: string): Promise<FilmProject> {
  return request(`/api/v1/film/projects/${projectId}/assets/${assetId}${query({ workspace })}`, {
    method: 'DELETE',
  })
}

export function analyzeFilmAssets(
  projectId: string,
  options: { assetIds?: string[]; deepAudio?: boolean; workspace?: string } = {},
): Promise<{
  project: FilmProject
  analyzed: Array<{ asset_id: string; name: string; available: boolean; limitations: string[] }>
}> {
  return request(`/api/v1/film/projects/${projectId}/assets/analyze`, {
    method: 'POST',
    body: JSON.stringify({
      workspace: options.workspace,
      asset_ids: options.assetIds,
      deep_audio: options.deepAudio,
    }),
  })
}

/** Attach specific images to specific shots or to every shot of a scene. */
export function assignFilmReferences(
  projectId: string,
  options: {
    assetIds: string[]
    shotIds?: string[]
    sceneIds?: string[]
    allShots?: boolean
    asStartImage?: boolean
    replace?: boolean
    workspace?: string
  },
): Promise<{
  project: FilmProject
  report: { shots: number; references_added: number; as_start_image: boolean; replaced: boolean }
  storyboard: FilmStoryboardEntry[]
}> {
  return request(`/api/v1/film/projects/${projectId}/references`, {
    method: 'POST',
    body: JSON.stringify({
      workspace: options.workspace,
      asset_ids: options.assetIds,
      shot_ids: options.shotIds ?? [],
      scene_ids: options.sceneIds ?? [],
      all_shots: options.allShots ?? false,
      as_start_image: options.asStartImage ?? false,
      replace: options.replace ?? false,
    }),
  })
}

/** Carry the character bible into every shot that features those characters. */
export function applyFilmContinuity(
  projectId: string,
  options: { replace?: boolean; workspace?: string } = {},
): Promise<{
  project: FilmProject
  report: {
    characters: number
    shots_updated: number
    references_added: number
    characters_matched: string[]
    characters_without_reference: string[]
    shots_without_references: string[]
    reason?: string
  }
  storyboard: FilmStoryboardEntry[]
}> {
  return request(`/api/v1/film/projects/${projectId}/continuity`, {
    method: 'POST',
    body: JSON.stringify({ workspace: options.workspace, replace: options.replace ?? false }),
  })
}

/** Store a Director plan (production plan or short-film script output) as the storyboard. */
export function importFilmPlan(
  projectId: string,
  response: Record<string, unknown>,
  importedFrom = '',
  workspace?: string,
): Promise<{
  project: FilmProject
  report: {
    scenes: number
    shots: number
    renders_preserved: number
    characters: number
    characters_without_reference: string[]
    planned_duration_sec: number
  }
  storyboard: FilmStoryboardEntry[]
}> {
  return request(`/api/v1/film/projects/${projectId}/plan/import`, {
    method: 'POST',
    body: JSON.stringify({ workspace, response, imported_from: importedFrom }),
  })
}

export function addFilmShot(
  projectId: string,
  shot: Partial<FilmStoryboardEntry>,
  workspace?: string,
): Promise<{ project: FilmProject; storyboard: FilmStoryboardEntry[] }> {
  return request(`/api/v1/film/projects/${projectId}/shots`, {
    method: 'POST',
    body: JSON.stringify({ workspace, shot }),
  })
}

export function updateFilmShot(
  projectId: string,
  shotId: string,
  patch: Record<string, unknown>,
  workspace?: string,
): Promise<{ project: FilmProject; storyboard: FilmStoryboardEntry[] }> {
  return request(`/api/v1/film/projects/${projectId}/shots/${shotId}`, {
    method: 'PUT',
    body: JSON.stringify({ workspace, shot: patch }),
  })
}

export function deleteFilmShot(
  projectId: string,
  shotId: string,
  workspace?: string,
): Promise<{ project: FilmProject; storyboard: FilmStoryboardEntry[] }> {
  return request(`/api/v1/film/projects/${projectId}/shots/${shotId}${query({ workspace })}`, {
    method: 'DELETE',
  })
}

export function moveFilmShot(
  projectId: string,
  shotId: string,
  index: number,
  workspace?: string,
): Promise<{ project: FilmProject; storyboard: FilmStoryboardEntry[] }> {
  return request(`/api/v1/film/projects/${projectId}/shots/${shotId}/move`, {
    method: 'POST',
    body: JSON.stringify({ workspace, index }),
  })
}

export function approveFilmShotTake(
  projectId: string,
  shotId: string,
  takeIndex: number,
  workspace?: string,
): Promise<{ project: FilmProject; storyboard: FilmStoryboardEntry[] }> {
  return request(`/api/v1/film/projects/${projectId}/shots/${shotId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ workspace, take_index: takeIndex }),
  })
}

export function fetchFilmProduction(
  projectId: string,
  workspace?: string,
  onlyMissing = true,
): Promise<{
  requests: FilmProductionRequest[]
  runnable: FilmProductionRequest[]
  skipped: FilmProductionRequest[]
  readiness: FilmReadinessIssue[]
  estimated_shots: number
}> {
  return request(`/api/v1/film/projects/${projectId}/production${query({ workspace, only_missing: onlyMissing })}`)
}

/**
 * Submit storyboard shots to the real generation queue. `params` must be the
 * Studio's own parameter set so the model settings are exactly what the user
 * configured; the backend only overrides the prompt and shot references.
 */
export function produceFilmShots(
  projectId: string,
  params: Record<string, unknown>,
  options: { shotIds?: string[]; applyReferences?: boolean; onlyMissing?: boolean; workspace?: string } = {},
): Promise<{
  submitted: Array<{ shot_id: string; job_id: string; kind: string }>
  failures: Array<{ shot_id: string; error: string }>
  skipped: FilmProductionRequest[]
  project: FilmProject
}> {
  return request(`/api/v1/film/projects/${projectId}/produce`, {
    method: 'POST',
    body: JSON.stringify({
      workspace: options.workspace,
      params,
      shot_ids: options.shotIds,
      apply_references: options.applyReferences ?? true,
      only_missing: options.onlyMissing ?? true,
    }),
  })
}

export function syncFilmProduction(
  projectId: string,
  workspace?: string,
): Promise<{ project: FilmProject; updated: Array<{ job_id: string; status: string; filename: string }> }> {
  return request(`/api/v1/film/projects/${projectId}/production/sync${query({ workspace })}`, {
    method: 'POST',
  })
}

export function planFilmSoundtrack(
  projectId: string,
  workspace?: string,
): Promise<{ project: FilmProject; soundtrack: { tracks: unknown[]; warnings: string[] }; music_sync: unknown }> {
  return request(`/api/v1/film/projects/${projectId}/soundtrack${query({ workspace })}`, { method: 'POST' })
}

export function syncFilmCutsToMusic(
  projectId: string,
  options: { toleranceSec?: number; applyChanges?: boolean; workspace?: string } = {},
): Promise<{
  project: FilmProject
  changes: Array<{ shot_id: string; from_sec: number; to_sec: number; delta_sec: number }>
  reason: string
  applied: boolean
}> {
  return request(`/api/v1/film/projects/${projectId}/soundtrack/sync`, {
    method: 'POST',
    body: JSON.stringify({
      workspace: options.workspace,
      tolerance_sec: options.toleranceSec ?? 1.5,
      apply_changes: options.applyChanges ?? false,
    }),
  })
}

export function fetchFilmAssembly(projectId: string, workspace?: string): Promise<FilmAssemblyReport> {
  return request(`/api/v1/film/projects/${projectId}/assembly${query({ workspace })}`)
}

export function assembleFilm(
  projectId: string,
  options: {
    includeSubtitles?: boolean
    includeTitles?: boolean
    titleText?: string
    creditsText?: string
    onlyApproved?: boolean
    width?: number
    height?: number
    fps?: number
    workspace?: string
  } = {},
): Promise<{
  project: FilmProject
  editor_project_id: string
  report: Record<string, unknown>
}> {
  return request(`/api/v1/film/projects/${projectId}/assembly`, {
    method: 'POST',
    body: JSON.stringify({
      workspace: options.workspace,
      include_subtitles: options.includeSubtitles,
      include_titles: options.includeTitles,
      title_text: options.titleText,
      credits_text: options.creditsText,
      only_approved: options.onlyApproved,
      width: options.width,
      height: options.height,
      fps: options.fps,
    }),
  })
}

export function exportFilm(
  projectId: string,
  options: {
    assemble?: boolean
    queueMode?: 'now' | 'held'
    includeSubtitles?: boolean
    includeTitles?: boolean
    creditsText?: string
    onlyApproved?: boolean
    workspace?: string
  } = {},
): Promise<{ job_id: string; status: string; editor_project_id: string; project: FilmProject }> {
  return request(`/api/v1/film/projects/${projectId}/export`, {
    method: 'POST',
    body: JSON.stringify({
      workspace: options.workspace,
      assemble: options.assemble ?? true,
      queue_mode: options.queueMode ?? 'now',
      include_subtitles: options.includeSubtitles,
      include_titles: options.includeTitles,
      credits_text: options.creditsText,
      only_approved: options.onlyApproved,
    }),
  })
}
