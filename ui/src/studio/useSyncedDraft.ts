import { useState } from 'react'

type Updater<T> = T | ((previous: T) => T)

/**
 * Keep a locally editable copy of a value that also lives on the server.
 *
 * The draft resets when the stored value changes underneath it (a reload, a new
 * plan), but never while the user is typing, because `signature` only changes
 * when the persisted value does. This uses React's documented render-phase state
 * adjustment instead of an effect, so it cannot cascade renders.
 */
export function useSyncedDraft<T>(signature: string, build: () => T): [T, (updater: Updater<T>) => void] {
  const [state, setState] = useState<{ signature: string; value: T }>(() => ({
    signature,
    value: build(),
  }))

  if (state.signature !== signature) {
    const next = { signature, value: build() }
    setState(next)
    return [
      next.value,
      updater =>
        setState(previous => ({
          signature: previous.signature,
          value: typeof updater === 'function' ? (updater as (value: T) => T)(previous.value) : updater,
        })),
    ]
  }

  return [
    state.value,
    updater =>
      setState(previous => ({
        signature: previous.signature,
        value: typeof updater === 'function' ? (updater as (value: T) => T)(previous.value) : updater,
      })),
  ]
}
