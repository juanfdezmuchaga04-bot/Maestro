import { useCallback, useRef, useState } from 'react'
import {
  cancelUploadSession,
  completeUploadSession,
  createUploadSession,
  fetchUploadSession,
  uploadChunk,
} from '../api/engines'
import type { UploadSessionState } from '../types/engines'

export type ChunkedUploadStatus =
  | 'idle'
  | 'preparing'
  | 'uploading'
  | 'assembling'
  | 'done'
  | 'error'
  | 'cancelled'

export interface ChunkedUploadState {
  status: ChunkedUploadStatus
  sessionId: string | null
  filename: string
  sentBytes: number
  totalBytes: number
  chunkIndex: number
  chunkCount: number
  result: UploadSessionState | null
  error: string
}

const INITIAL: ChunkedUploadState = {
  status: 'idle',
  sessionId: null,
  filename: '',
  sentBytes: 0,
  totalBytes: 0,
  chunkIndex: 0,
  chunkCount: 0,
  result: null,
  error: '',
}

/** SHA-256 of a chunk, or '' when the browser has no SubtleCrypto (http, old). */
async function chunkDigest(blob: Blob): Promise<string> {
  try {
    const buffer = await blob.arrayBuffer()
    const digest = await crypto.subtle.digest('SHA-256', buffer)
    return Array.from(new Uint8Array(digest))
      .map(byte => byte.toString(16).padStart(2, '0'))
      .join('')
  } catch {
    return ''
  }
}

/**
 * Upload a large file in chunks, resuming an interrupted transfer instead of
 * restarting it. Nothing is buffered whole: one chunk is read at a time and the
 * server assembles the file on disk.
 */
export function useChunkedUpload() {
  const [state, setState] = useState<ChunkedUploadState>(INITIAL)
  const abortRef = useRef<AbortController | null>(null)

  const reset = useCallback(() => {
    abortRef.current = null
    setState(INITIAL)
  }, [])

  const send = useCallback(
    async (file: File, session: UploadSessionState) => {
      const chunkSize = session.chunk_size || 8 * 1024 * 1024
      const controller = new AbortController()
      abortRef.current = controller
      const alreadySent = session.received_bytes
      const chunkCount = session.chunks_total ?? Math.ceil(file.size / chunkSize)
      setState(prev => ({
        ...prev,
        status: 'uploading',
        sessionId: session.session_id,
        filename: session.filename,
        totalBytes: session.total_bytes || file.size,
        chunkCount,
        sentBytes: alreadySent,
      }))

      let sent = alreadySent
      for (const index of session.missing_chunks) {
        const start = index * chunkSize
        const blob = file.slice(start, Math.min(start + chunkSize, file.size))
        const digest = await chunkDigest(blob)
        const updated = await uploadChunk(session.session_id, index, blob, {
          sha256: digest || undefined,
          signal: controller.signal,
        })
        sent = updated.received_bytes
        setState(prev => ({ ...prev, chunkIndex: index + 1, sentBytes: sent }))
      }

      setState(prev => ({ ...prev, status: 'assembling' }))
      const result = await completeUploadSession(session.session_id)
      setState(prev => ({ ...prev, status: 'done', result, sentBytes: result.received_bytes }))
      return result
    },
    [],
  )

  const run = useCallback(
    async (file: File, options: { chunkSize?: number; sha256?: string } = {}) => {
      setState({ ...INITIAL, status: 'preparing', filename: file.name, totalBytes: file.size })
      let session: UploadSessionState
      try {
        session = await createUploadSession({
          filename: file.name,
          totalBytes: file.size,
          contentType: file.type || 'application/octet-stream',
          sha256: options.sha256,
          chunkSize: options.chunkSize,
        })
      } catch (error) {
        setState(prev => ({ ...prev, status: 'error', error: (error as Error).message }))
        return null
      }
      try {
        return await send(file, session)
      } catch (error) {
        const message = (error as Error).message
        const aborted = /abort/i.test(message)
        setState(prev => ({
          ...prev,
          status: aborted ? 'cancelled' : 'error',
          error: aborted ? '' : message,
        }))
        return null
      } finally {
        abortRef.current = null
      }
    },
    [send],
  )

  /** Resume a session that was interrupted: only the missing chunks are sent. */
  const resume = useCallback(
    async (file: File, sessionId: string) => {
      setState({ ...INITIAL, status: 'preparing', filename: file.name, sessionId })
      try {
        const session = await fetchUploadSession(sessionId)
        if (session.total_bytes && session.total_bytes !== file.size) {
          throw new Error(
            `This session expects ${session.total_bytes} bytes but the selected file has ${file.size}`,
          )
        }
        return await send(file, session)
      } catch (error) {
        setState(prev => ({ ...prev, status: 'error', error: (error as Error).message }))
        return null
      } finally {
        abortRef.current = null
      }
    },
    [send],
  )

  /** Stop the transfer and delete the temporary chunks on the server. */
  const cancel = useCallback(async () => {
    abortRef.current?.abort()
    const sessionId = state.sessionId
    if (!sessionId) {
      setState(prev => ({ ...prev, status: 'cancelled' }))
      return
    }
    try {
      await cancelUploadSession(sessionId)
    } catch (error) {
      setState(prev => ({ ...prev, status: 'error', error: (error as Error).message }))
      return
    }
    setState(prev => ({ ...prev, status: 'cancelled', error: '' }))
  }, [state.sessionId])

  return { ...state, run, resume, cancel, reset }
}
