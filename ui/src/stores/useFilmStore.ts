import { create } from 'zustand'
import * as filmApi from '../api/film'
import { cancelJob, uploadImage } from '../api/client'
import { useStore } from './useStore'
import type {
  FilmAssemblyReport,
  FilmBrief,
  FilmBriefBundle,
  FilmEngineStatus,
  FilmOverview,
  FilmProjectSummary,
} from '../types/film'

const STORAGE_KEY = 'maestro-films-last-project'

interface FilmState {
  workspace: string
  projects: FilmProjectSummary[]
  activeProjectId: string | null
  overview: FilmOverview | null
  briefBundle: FilmBriefBundle | null
  assembly: FilmAssemblyReport | null
  engine: FilmEngineStatus | null
  loading: boolean
  busy: string
  error: string | null
  notice: string | null
  uploadProgress: { done: number; total: number } | null

  initialize: (workspace: string) => Promise<void>
  refreshProjects: () => Promise<void>
  refreshOverview: () => Promise<void>
  refreshBrief: () => Promise<void>
  refreshAssembly: () => Promise<void>
  createProject: (name: string, approach?: string) => Promise<void>
  openProject: (projectId: string) => Promise<void>
  closeProject: () => void
  deleteProject: (projectId: string) => Promise<void>
  saveBrief: (patch: Partial<FilmBrief> & { approach?: string }) => Promise<void>
  importFiles: (files: File[], analyze?: boolean) => Promise<void>
  setAssetRole: (assetId: string, role: string, extra?: { reproduceLiterally?: boolean; notes?: string }) => Promise<void>
  removeAsset: (assetId: string) => Promise<void>
  analyzeAssets: (assetIds?: string[], deepAudio?: boolean) => Promise<void>
  importDirectorPlan: (response: Record<string, unknown>, importedFrom?: string) => Promise<void>
  editShot: (shotId: string, patch: Record<string, unknown>) => Promise<void>
  addShot: (shot?: Record<string, unknown>) => Promise<void>
  removeShot: (shotId: string) => Promise<void>
  moveShot: (shotId: string, index: number) => Promise<void>
  approveTake: (shotId: string, takeIndex: number) => Promise<void>
  setShotReferences: (shotId: string, assetIds: string[], startAssetId?: string) => Promise<void>
  addReferencesToScene: (sceneId: string, assetIds: string[]) => Promise<void>
  applyContinuity: () => Promise<void>
  produce: (shotIds?: string[]) => Promise<void>
  syncProduction: () => Promise<void>
  cancelJob: (jobId: string) => Promise<void>
  planSoundtrack: () => Promise<void>
  syncCutsToMusic: (applyChanges: boolean, toleranceSec?: number) => Promise<void>
  assemble: (options?: Record<string, unknown>) => Promise<string | null>
  exportFilm: (options?: Record<string, unknown>) => Promise<void>
  clearNotice: () => void
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  return String(error)
}

export const useFilmStore = create<FilmState>()((set, get) => ({
  workspace: 'default',
  projects: [],
  activeProjectId: null,
  overview: null,
  briefBundle: null,
  assembly: null,
  engine: null,
  loading: false,
  busy: '',
  error: null,
  notice: null,
  uploadProgress: null,

  initialize: async (workspace) => {
    set({ workspace })
    try {
      set({ engine: await filmApi.fetchFilmEngine() })
    } catch {
      // The engine report is informational; never block the workspace on it.
      set({ engine: null })
    }
    await get().refreshProjects()
    const remembered = localStorage.getItem(STORAGE_KEY)
    const candidate = remembered && get().projects.some(project => project.id === remembered)
      ? remembered
      : get().projects[0]?.id
    if (candidate) await get().openProject(candidate)
  },

  refreshProjects: async () => {
    try {
      const { projects } = await filmApi.fetchFilmProjects(get().workspace)
      set({ projects, error: null })
    } catch (error) {
      set({ error: errorMessage(error) })
    }
  },

  refreshOverview: async () => {
    const projectId = get().activeProjectId
    if (!projectId) return
    set({ loading: true })
    try {
      const overview = await filmApi.fetchFilmOverview(projectId, get().workspace)
      set({
        overview,
        engine: overview.engine,
        projects: get().projects.some(entry => entry.id === projectId)
          ? get().projects.map(entry => (entry.id === projectId ? overview.summary : entry))
          : get().projects,
        error: null,
      })
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ loading: false })
    }
  },

  refreshAssembly: async () => {
    const projectId = get().activeProjectId
    if (!projectId) return
    try {
      set({ assembly: await filmApi.fetchFilmAssembly(projectId, get().workspace) })
    } catch (error) {
      set({ error: errorMessage(error) })
    }
  },

  createProject: async (name, approach = 'hybrid') => {
    set({ busy: 'Creating production', error: null })
    try {
      const project = await filmApi.createFilmProject({
        name,
        approach,
        workspace: get().workspace,
      })
      await get().refreshProjects()
      await get().openProject(project.id)
      set({ notice: 'Production created. Describe the idea next.' })
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '' })
    }
  },

  openProject: async (projectId) => {
    set({ activeProjectId: projectId })
    localStorage.setItem(STORAGE_KEY, projectId)
    set({ briefBundle: null, assembly: null, overview: null })
    await get().refreshOverview()
    get().refreshBrief().catch(() => undefined)
  },

  refreshBrief: async () => {
    const projectId = get().activeProjectId
    if (!projectId) return
    try {
      set({ briefBundle: await filmApi.fetchFilmBrief(projectId, get().workspace) })
    } catch (error) {
      set({ error: errorMessage(error) })
    }
  },

  closeProject: () => {
    localStorage.removeItem(STORAGE_KEY)
    set({ activeProjectId: null, overview: null, briefBundle: null, assembly: null })
  },

  deleteProject: async (projectId) => {
    set({ busy: 'Deleting production', error: null })
    try {
      await filmApi.deleteFilmProject(projectId, get().workspace)
      if (get().activeProjectId === projectId) get().closeProject()
      await get().refreshProjects()
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '' })
    }
  },

  saveBrief: async (patch) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    set({ busy: 'Saving brief', error: null })
    try {
      await filmApi.updateFilmBrief(projectId, patch, get().workspace)
      await get().refreshOverview()
      await get().refreshBrief()
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '' })
    }
  },

  importFiles: async (files, analyze = true) => {
    const projectId = get().activeProjectId
    if (!projectId || files.length === 0) return
    set({ busy: `Importing ${files.length} file(s)`, error: null, uploadProgress: { done: 0, total: files.length } })
    try {
      const uploaded: Array<{ filename: string; name: string; path: string }> = []
      for (const [index, file] of files.entries()) {
        const result = await uploadImage(file)
        uploaded.push({ filename: result.filename, name: file.name, path: result.path })
        set({ uploadProgress: { done: index + 1, total: files.length } })
      }
      const result = await filmApi.importFilmAssets(projectId, uploaded, {
        analyze,
        workspace: get().workspace,
      })
      await get().refreshOverview()
      const rejected = result.rejected.length
        ? ` ${result.rejected.length} file(s) were skipped: ${result.rejected.map(entry => entry.reason).join('; ')}`
        : ''
      set({
        notice: `Imported ${result.imported.length} file(s).${rejected} Check the creative role the Studio guessed for each one.`,
      })
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '', uploadProgress: null })
    }
  },

  setAssetRole: async (assetId, role, extra) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    set({ busy: 'Applying your correction', error: null })
    try {
      await filmApi.updateFilmAsset(projectId, assetId, {
        role,
        workspace: get().workspace,
        reproduce_literally: extra?.reproduceLiterally,
        notes: extra?.notes,
      })
      await get().refreshOverview()
      set({ notice: 'Role saved. The Director will use this file that way.' })
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '' })
    }
  },

  removeAsset: async (assetId) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    try {
      await filmApi.deleteFilmAsset(projectId, assetId, get().workspace)
      await get().refreshOverview()
    } catch (error) {
      set({ error: errorMessage(error) })
    }
  },

  analyzeAssets: async (assetIds, deepAudio = false) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    set({ busy: deepAudio ? 'Analysing audio (tempo, sections)' : 'Analysing files locally', error: null })
    try {
      const result = await filmApi.analyzeFilmAssets(projectId, {
        assetIds,
        deepAudio,
        workspace: get().workspace,
      })
      const unavailable = result.analyzed.filter(entry => !entry.available)
      set({
        notice: unavailable.length
          ? `${unavailable.length} file(s) could not be analysed locally: ${unavailable[0].limitations[0] || 'reason unknown'}`
          : `Analysed ${result.analyzed.length} file(s).`,
      })
      await get().refreshOverview()
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '' })
    }
  },

  importDirectorPlan: async (response, importedFrom = '') => {
    const projectId = get().activeProjectId
    if (!projectId) return
    set({ busy: 'Storyboarding the plan', error: null })
    try {
      const result = await filmApi.importFilmPlan(projectId, response, importedFrom, get().workspace)
      await get().refreshOverview()
      await get().refreshAssembly()
      const unmatched = result.report.characters_without_reference
      set({
        notice: unmatched.length
          ? `Storyboard ready: ${result.report.shots} shot(s). No reference photo matched: ${unmatched.join(', ')}.`
          : `Storyboard ready: ${result.report.shots} shot(s) over ${result.report.scenes} scene(s).`,
      })
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '' })
    }
  },

  editShot: async (shotId, patch) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    try {
      await filmApi.updateFilmShot(projectId, shotId, patch, get().workspace)
      await get().refreshOverview()
    } catch (error) {
      set({ error: errorMessage(error) })
    }
  },

  addShot: async (shot = {}) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    try {
      await filmApi.addFilmShot(projectId, shot, get().workspace)
      await get().refreshOverview()
    } catch (error) {
      set({ error: errorMessage(error) })
    }
  },

  removeShot: async (shotId) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    try {
      await filmApi.deleteFilmShot(projectId, shotId, get().workspace)
      await get().refreshOverview()
    } catch (error) {
      set({ error: errorMessage(error) })
    }
  },

  moveShot: async (shotId, index) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    try {
      await filmApi.moveFilmShot(projectId, shotId, index, get().workspace)
      await get().refreshOverview()
    } catch (error) {
      set({ error: errorMessage(error) })
    }
  },

  approveTake: async (shotId, takeIndex) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    try {
      await filmApi.approveFilmShotTake(projectId, shotId, takeIndex, get().workspace)
      await get().refreshOverview()
      set({ notice: 'Take approved. Assembly will use it.' })
    } catch (error) {
      set({ error: errorMessage(error) })
    }
  },

  setShotReferences: async (shotId, assetIds, startAssetId = '') => {
    const projectId = get().activeProjectId
    if (!projectId) return
    // The backend treats the first asset as the start frame when asked, so the
    // chosen start frame goes first and the rest become reference images.
    const ordered = startAssetId
      ? [startAssetId, ...assetIds.filter(id => id !== startAssetId)]
      : assetIds
    set({ busy: 'Assigning references', error: null })
    try {
      await filmApi.assignFilmReferences(projectId, {
        assetIds: ordered,
        shotIds: [shotId],
        replace: true,
        asStartImage: Boolean(startAssetId),
        workspace: get().workspace,
      })
      await get().refreshOverview()
      set({ notice: ordered.length ? 'References saved for this shot.' : 'References cleared for this shot.' })
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '' })
    }
  },

  addReferencesToScene: async (sceneId, assetIds) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    set({ busy: 'Assigning references to the scene', error: null })
    try {
      const result = await filmApi.assignFilmReferences(projectId, {
        assetIds,
        sceneIds: [sceneId],
        workspace: get().workspace,
      })
      await get().refreshOverview()
      set({ notice: `Reference added to ${result.report.shots} shot(s) of this scene.` })
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '' })
    }
  },

  applyContinuity: async () => {
    const projectId = get().activeProjectId
    if (!projectId) return
    set({ busy: 'Applying visual continuity', error: null })
    try {
      const result = await filmApi.applyFilmContinuity(projectId, { workspace: get().workspace })
      await get().refreshOverview()
      const report = result.report
      const missing = report.characters_without_reference
      set({
        notice: report.reason
          ? report.reason
          : `Continuity applied to ${report.shots_updated} shot(s)` +
            (missing.length ? `. No reference photo for: ${missing.join(', ')}.` : '.'),
      })
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '' })
    }
  },

  produce: async (shotIds) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    // The Studio's own parameter set is the source of truth for model,
    // resolution, LoRAs and steps. Without it there is nothing honest to run.
    const params = useStore.getState().params as unknown as Record<string, unknown> | undefined
    if (!params || !params.model_type) {
      set({
        error: 'Choose a model in the Studio sidecar first: production reuses those exact settings.',
      })
      return
    }
    // Naming specific shots means the user made a deliberate decision, so an
    // existing render must not silently skip them (regenerate has to regenerate).
    const explicit = Array.isArray(shotIds) && shotIds.length > 0
    set({ busy: 'Submitting shots to the queue', error: null })
    try {
      const result = await filmApi.produceFilmShots(projectId, params, {
        shotIds,
        applyReferences: true,
        onlyMissing: !explicit,
        workspace: get().workspace,
      })
      const failures = result.failures.map(entry => entry.error).slice(0, 2).join(' | ')
      set({
        notice: result.submitted.length
          ? `${result.submitted.length} shot(s) queued. Track them in the global queue.`
          : 'Nothing was submitted.',
        error: failures || null,
      })
      await get().refreshOverview()
      useStore.getState().reconnectJobs().catch(() => undefined)
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '' })
    }
  },

  cancelJob: async (jobId) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    set({ busy: 'Cancelling the job', error: null })
    try {
      // Cancellation is the shared job machinery's job; the production only
      // reflects the outcome.
      await cancelJob(jobId)
      await get().syncProduction()
      await get().refreshOverview()
      set({ notice: 'Cancellation requested. The shot is available to submit again.' })
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '' })
    }
  },

  syncProduction: async () => {
    const projectId = get().activeProjectId
    if (!projectId) return
    try {
      const result = await filmApi.syncFilmProduction(projectId, get().workspace)
      await get().refreshOverview()
      if (result.updated.length) {
        set({ notice: `Updated ${result.updated.length} shot(s) from the queue.` })
      }
    } catch (error) {
      set({ error: errorMessage(error) })
    }
  },

  planSoundtrack: async () => {
    const projectId = get().activeProjectId
    if (!projectId) return
    set({ busy: 'Planning the soundtrack', error: null })
    try {
      await filmApi.planFilmSoundtrack(projectId, get().workspace)
      await get().refreshOverview()
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '' })
    }
  },

  syncCutsToMusic: async (applyChanges, toleranceSec = 1.5) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    try {
      const result = await filmApi.syncFilmCutsToMusic(projectId, {
        applyChanges,
        toleranceSec,
        workspace: get().workspace,
      })
      if (!applyChanges) {
        set({
          notice: result.changes.length
            ? `${result.changes.length} cut(s) could be moved onto the beat (max ${Math.max(...result.changes.map(change => Math.abs(change.delta_sec))).toFixed(2)}s).`
            : 'No cut is close enough to a beat to be worth moving.',
        })
      } else {
        set({
          notice: result.changes.length
            ? `Moved ${result.changes.length} cut(s) onto the music.`
            : 'Nothing moved.',
        })
      }
      await get().refreshOverview()
    } catch (error) {
      set({ error: errorMessage(error) })
    }
  },

  assemble: async (options = {}) => {
    const projectId = get().activeProjectId
    if (!projectId) return null
    set({ busy: 'Assembling the cut', error: null })
    try {
      const result = await filmApi.assembleFilm(projectId, {
        ...(options as Parameters<typeof filmApi.assembleFilm>[1]),
        workspace: get().workspace,
      })
      await get().refreshOverview()
      await get().refreshAssembly()
      set({ notice: `Cut assembled in the Editor (project ${result.editor_project_id}).` })
      return result.editor_project_id
    } catch (error) {
      set({ error: errorMessage(error) })
      return null
    } finally {
      set({ busy: '' })
    }
  },

  exportFilm: async (options = {}) => {
    const projectId = get().activeProjectId
    if (!projectId) return
    set({ busy: 'Rendering the final cut with FFmpeg', error: null })
    try {
      const result = await filmApi.exportFilm(projectId, {
        ...(options as Parameters<typeof filmApi.exportFilm>[1]),
        workspace: get().workspace,
      })
      await get().refreshOverview()
      set({
        notice: `Render queued (job ${result.job_id}). The finished MP4 appears in the media gallery.`,
      })
      useStore.getState().reconnectJobs().catch(() => undefined)
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: '' })
    }
  },

  clearNotice: () => set({ notice: null, error: null }),
}))
