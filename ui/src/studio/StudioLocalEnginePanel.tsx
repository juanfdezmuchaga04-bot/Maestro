import { useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle,
  BadgeCheck,
  Boxes,
  CheckCircle2,
  Clapperboard,
  Cpu,
  Download,
  HardDrive,
  Info,
  Loader2,
  PlayCircle,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react'
import {
  fetchLocalHardware,
  fetchLocalSetup,
  installLocalEngine,
  queueLocalClip,
  registerLocalEngine,
  runLocalFunctionalTest,
} from '../api/localEngine'
import type {
  FunctionalTestResult,
  InstallResult,
  LocalEngineHardwareResponse,
  LocalSetupPlan,
} from '../types/localEngine'

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/60 pb-1">
      <span className="text-[10px] text-text-muted">{label}</span>
      <span className="min-w-0 truncate text-[11px] text-text-secondary" title={value}>
        {value}
      </span>
    </div>
  )
}

function Note({ tone = 'info', children }: { tone?: 'info' | 'warn'; children: React.ReactNode }) {
  const styles =
    tone === 'warn'
      ? 'border-indicator-warning/40 bg-indicator-warning/10'
      : 'border-border bg-bg-tertiary'
  return (
    <div className={`mt-2 flex items-start gap-2 rounded-lg border p-2 text-[11px] text-text-secondary ${styles}`}>
      {tone === 'warn' ? (
        <AlertTriangle size={12} className="mt-0.5 shrink-0 text-indicator-warning" />
      ) : (
        <Info size={12} className="mt-0.5 shrink-0 text-text-muted" />
      )}
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}

export function StudioLocalEnginePanel() {
  const [hardware, setHardware] = useState<LocalEngineHardwareResponse | null>(null)
  const [plan, setPlan] = useState<LocalSetupPlan | null>(null)
  const [install, setInstall] = useState<InstallResult | null>(null)
  const [test, setTest] = useState<FunctionalTestResult | null>(null)
  const [registered, setRegistered] = useState<Record<string, unknown> | null>(null)
  const [baseUrl, setBaseUrl] = useState('')
  const [prompt, setPrompt] = useState('a paper boat drifting down a small stream, cinematic lighting')
  const [seconds, setSeconds] = useState(5)
  const [queued, setQueued] = useState<string>('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [downloadWeights, setDownloadWeights] = useState(false)
  const [showEvaluated, setShowEvaluated] = useState(false)

  const load = useCallback(async () => {
    setBusy('Reading this computer')
    setError('')
    try {
      const data = await fetchLocalHardware()
      setHardware(data)
      setBaseUrl(current => current || data.engine.default_url)
      setPlan(await fetchLocalSetup({}))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy('')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const act = async (label: string, action: () => Promise<void>) => {
    setBusy(label)
    setError('')
    try {
      await action()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy('')
    }
  }

  const gpu = hardware?.hardware.primary_gpu || null
  const selected = hardware?.selection.model || null

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Clapperboard size={16} className="text-accent-warm" />
        <h2 className="text-sm font-semibold text-text-primary">Local engine</h2>
        <span className="rounded-md border border-accent-green/40 bg-accent-green/10 px-1.5 py-0.5 text-[10px] text-text-primary">
          no credits · no cloud
        </span>
        <button
          type="button"
          onClick={() => void load()}
          className="ml-auto flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
        >
          {busy === 'Reading this computer' ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <RefreshCw size={12} />
          )}
          Re-detect
        </button>
      </div>

      <p className="text-xs text-text-muted">
        Generation runs on your own computer through a self-hosted ComfyUI. Every figure below is
        measured from this machine, and the engine is only registered once a real clip comes back.
      </p>

      {error && (
        <p className="flex items-start gap-2 rounded-lg border border-chip-red/40 bg-chip-red/10 p-2.5 text-[11px] text-text-primary">
          <AlertTriangle size={12} className="mt-0.5 shrink-0 text-chip-red" />
          <span className="flex-1">{error}</span>
        </p>
      )}

      {hardware && (
        <section className="rounded-xl border border-border bg-bg-secondary p-4">
          <div className="flex items-center gap-2">
            <Cpu size={15} className="text-accent-warm" />
            <h3 className="text-xs font-semibold text-text-primary">This computer</h3>
            <span className="ml-auto text-[10px] text-text-muted">{hardware.hardware.tier.tier}</span>
          </div>
          <div className="mt-3 grid gap-1.5 sm:grid-cols-2">
            <Row
              label="GPU"
              value={gpu ? `${gpu.name}${gpu.driver_version ? ` · driver ${gpu.driver_version}` : ''}` : 'no CUDA GPU detected'}
            />
            <Row
              label="VRAM"
              value={hardware.hardware.vram_total_mb ? `${(hardware.hardware.vram_total_mb / 1024).toFixed(1)} GB` : 'unknown'}
            />
            <Row
              label="System RAM"
              value={hardware.hardware.memory.total_gb ? `${hardware.hardware.memory.total_gb} GB` : 'unknown'}
            />
            <Row
              label="Free disk"
              value={hardware.hardware.disk.free_gb !== null ? `${hardware.hardware.disk.free_gb} GB` : 'unknown'}
            />
            <Row label="OS" value={`${hardware.hardware.os.system} ${hardware.hardware.os.release}`} />
            <Row label="Python" value={hardware.hardware.os.python} />
          </div>
          {hardware.hardware.unmeasured.length > 0 && (
            <Note tone="warn">
              <ul className="list-disc space-y-0.5 pl-4">
                {hardware.hardware.unmeasured.map(note => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            </Note>
          )}
          {hardware.hardware.problems.length > 0 && (
            <Note tone="warn">
              <ul className="list-disc space-y-0.5 pl-4">
                {hardware.hardware.problems.map(problem => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            </Note>
          )}
          <div className="mt-2">
            <Row label="CUDA" value={hardware.hardware.cuda.available ? 'available' : hardware.hardware.cuda.reason} />
            <Row
              label="Tools"
              value={Object.keys(hardware.hardware.tools).sort().join(', ') || 'none of git/ffmpeg/nvidia-smi found'}
            />
          </div>
        </section>
      )}

      {hardware && (
        <section className="rounded-xl border border-border bg-bg-secondary p-4">
          <div className="flex items-center gap-2">
            <Boxes size={15} className="text-accent-warm" />
            <h3 className="text-xs font-semibold text-text-primary">Model</h3>
          </div>
          {selected ? (
            <>
              <p className="mt-2 text-xs text-text-primary">{selected.label}</p>
              <div className="mt-2 grid gap-1.5 sm:grid-cols-2">
                <Row label="Licence" value={selected.licence} />
                <Row
                  label="Needs"
                  value={`${(selected.min_vram_mb / 1024).toFixed(0)} GB VRAM · ${selected.recommended_free_disk_gb} GB free disk`}
                />
                <Row label="Output" value={`${selected.resolution} · ${selected.fps} fps · ${selected.frames} frames`} />
                <Row label="Weights" value={`${selected.model_disk_gb} GB (${selected.files.length} files)`} />
              </div>
              <Note>{selected.commercial_note}</Note>
              <Note>
                Why this model:{' '}
                {hardware.selection.detail} The requirement comes from: {selected.vram_source}
              </Note>
            </>
          ) : (
            <Note tone="warn">
              {hardware.selection.detail}
              <ul className="mt-1 list-disc space-y-0.5 pl-4">
                {(hardware.selection.blockers || []).map(blocker => (
                  <li key={blocker}>{blocker}</li>
                ))}
                {(hardware.selection.unknowns || []).map(unknown => (
                  <li key={unknown}>{unknown}</li>
                ))}
              </ul>
              <p className="mt-1">
                You can still import your own files, write the storyboard and assemble a cut: those
                parts need no GPU. Generation needs one.
              </p>
            </Note>
          )}

          <button
            type="button"
            onClick={() => setShowEvaluated(value => !value)}
            className="mt-3 text-[10px] text-text-muted hover:text-text-primary"
          >
            {showEvaluated ? 'hide' : 'show'} models evaluated and not selected
          </button>
          {showEvaluated && (
            <ul className="mt-2 space-y-2">
              {hardware.evaluated_but_not_selected.map(entry => (
                <li key={entry.model_id} className="rounded-lg border border-border bg-bg-tertiary p-2.5">
                  <div className="text-[11px] font-medium text-text-primary">{entry.label}</div>
                  <div className="text-[10px] text-text-muted">{entry.licence}</div>
                  <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[10px] text-text-secondary">
                    {entry.reasons.map(reason => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                  <div className="mt-1 text-[10px] text-text-muted">{entry.credits}</div>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {plan && (
        <section className="rounded-xl border border-border bg-bg-secondary p-4">
          <div className="flex items-center gap-2">
            <Download size={15} className="text-accent-warm" />
            <h3 className="text-xs font-semibold text-text-primary">Install the engine here</h3>
          </div>
          <p className="mt-1 text-[11px] text-text-muted">
            Runs on this computer, in a folder of its own, and binds ComfyUI to loopback only.
            {plan.present?.comfyui_checkout ? ' ComfyUI is already downloaded.' : ''}
          </p>
          <div className="mt-2 grid gap-1.5 sm:grid-cols-2">
            <Row label="Folder" value={plan.comfy_root} />
            <Row label="ComfyUI address" value={plan.base_url} />
            <Row label="Weights to fetch" value={`${plan.download_gb} GB`} />
            <Row
              label="Free disk needed"
              value={`${plan.disk_required_gb} GB (free: ${plan.disk_free_gb ?? 'unknown'} GB)`}
            />
          </div>

          {plan.blockers.length > 0 && (
            <Note tone="warn">
              <ul className="list-disc space-y-0.5 pl-4">
                {plan.blockers.map(blocker => (
                  <li key={blocker}>{blocker}</li>
                ))}
              </ul>
            </Note>
          )}

          <ul className="mt-3 space-y-2">
            {plan.steps.map(step => (
              <li key={step.id} className="rounded-lg border border-border bg-bg-tertiary p-2.5">
                <div className="flex items-center gap-2">
                  <span className="text-[11px] font-medium text-text-primary">{step.label}</span>
                  {step.already_present && <span className="text-[10px] text-accent-green">already present</span>}
                  {step.requires_flag && (
                    <span className="text-[10px] text-indicator-warning">needs your confirmation</span>
                  )}
                </div>
                <div className="mt-0.5 text-[10px] text-text-muted">{step.why}</div>
                {step.command && (
                  <code className="mt-1 block overflow-x-auto rounded bg-bg-secondary px-2 py-1 text-[10px] text-text-secondary">
                    {step.command.join(' ')}
                  </code>
                )}
              </li>
            ))}
          </ul>

          <label className="mt-3 flex items-start gap-2 text-[11px] text-text-secondary">
            <input
              type="checkbox"
              checked={downloadWeights}
              onChange={event => setDownloadWeights(event.target.checked)}
              className="mt-0.5"
            />
            <span>
              Also download the model weights ({plan.download_gb} GB). Nothing is downloaded unless
              you tick this.
            </span>
          </label>

          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy !== '' || plan.blockers.length > 0}
              onClick={() =>
                void act('Installing', async () => {
                  setInstall(null)
                  setInstall(await installLocalEngine({ confirm: true, downloadModels: downloadWeights }))
                })
              }
              className="flex items-center gap-1.5 rounded-lg bg-accent-warm px-3 py-2 text-[11px] font-medium text-black disabled:opacity-40"
            >
              {busy === 'Installing' ? <Loader2 size={12} className="animate-spin" /> : <HardDrive size={12} />}
              Install on this computer
            </button>
            <span className="self-center text-[10px] text-text-muted">
              {plan.workflow ? `workflow: ${plan.workflow}` : ''}
            </span>
          </div>

          {install && (
            <div className="mt-3">
              <ul className="space-y-1">
                {install.steps.map(step => (
                  <li key={step.id} className="flex items-start gap-2 text-[10px]">
                    <span
                      className={
                        step.status === 'done' || step.status === 'skipped'
                          ? 'shrink-0 text-accent-green'
                          : step.status === 'failed'
                            ? 'shrink-0 text-chip-red'
                            : 'shrink-0 text-text-muted'
                      }
                    >
                      {step.status}
                    </span>
                    <span className="shrink-0 text-text-secondary">{step.id}</span>
                    <span className="min-w-0 flex-1 truncate text-text-muted" title={step.detail}>
                      {step.detail}
                    </span>
                  </li>
                ))}
              </ul>
              <Note tone={install.completed ? 'info' : 'warn'}>{install.next}</Note>
            </div>
          )}
        </section>
      )}

      <section className="rounded-xl border border-border bg-bg-secondary p-4">
        <div className="flex items-center gap-2">
          <PlayCircle size={15} className="text-accent-warm" />
          <h3 className="text-xs font-semibold text-text-primary">Prove it with a real clip</h3>
        </div>
        <p className="mt-1 text-[11px] text-text-muted">
          Generates a deliberately tiny clip (256×256, 9 frames) so the proof costs seconds. The
          engine is registered only if a real video file comes back.
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          <input
            value={baseUrl}
            onChange={event => setBaseUrl(event.target.value)}
            inputMode="url"
            placeholder="http://127.0.0.1:8188"
            className="min-w-0 flex-1 rounded-lg border border-border bg-bg-tertiary px-3 py-2 text-xs text-text-primary focus:border-accent-warm focus:outline-none"
          />
          <button
            type="button"
            disabled={busy !== '' || !baseUrl.trim()}
            onClick={() => void act('Testing the engine', async () => setTest(await runLocalFunctionalTest({ baseUrl: baseUrl.trim() })))}
            className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
          >
            {busy === 'Testing the engine' ? <Loader2 size={12} className="animate-spin" /> : <PlayCircle size={12} />}
            Run the test
          </button>
          <button
            type="button"
            disabled={busy !== '' || !baseUrl.trim()}
            onClick={() =>
              void act('Registering', async () => {
                const response = await registerLocalEngine({ baseUrl: baseUrl.trim() })
                setRegistered(response as unknown as Record<string, unknown>)
                if (!response.registered) setError(response.reason || 'Registration was refused')
              })
            }
            className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
          >
            <BadgeCheck size={12} /> Register engine
          </button>
        </div>

        {test && (
          <div className="mt-3">
            {test.passed ? (
              <>
                <p className="flex items-center gap-2 text-[11px] text-accent-green">
                  <CheckCircle2 size={12} /> Real clip generated and copied into your workspace
                </p>
                {test.clip_url && (
                  <video
                    src={test.clip_url}
                    controls
                    playsInline
                    className="mt-2 w-full rounded-lg border border-border bg-black"
                  />
                )}
                <div className="mt-2 grid gap-1.5">
                  <Row label="File" value={test.clip_path} />
                  <Row label="Size" value={`${test.bytes} bytes · ${test.container}`} />
                  <Row label="SHA-256" value={String(test.sha256).slice(0, 32)} />
                  <Row label="Took" value={`${test.seconds}s`} />
                  <Row label="ComfyUI prompt" value={String(test.prompt_id)} />
                </div>
                <Note>{test.note}</Note>
              </>
            ) : (
              <Note tone="warn">{test.reason}</Note>
            )}
          </div>
        )}

        {registered && (
          <Note tone={registered.registered ? 'info' : 'warn'}>
            {registered.registered
              ? `Registered as ${String((registered.node as { node_id?: string })?.node_id)} with capabilities ${((registered.node as { capabilities?: string[] })?.capabilities || []).join(', ')}. Keep its token safe: it is only shown once.`
              : String(registered.reason || 'Not registered')}
          </Note>
        )}

        {plan && (
          <div className="mt-3 rounded-lg border border-border bg-bg-tertiary p-2.5">
            <p className="text-[10px] text-text-muted">
              ComfyUI normally runs on the computer with the GPU. Start it there with:
            </p>
            <code className="mt-1 block overflow-x-auto text-[10px] text-text-secondary">
              {plan.venv_python} main.py --listen 127.0.0.1 --port 8188
            </code>
            <p className="mt-1 text-[10px] text-text-muted">
              If the GPU is on another computer, expose it safely with Tailscale and register that
              address here instead of <code>127.0.0.1</code>.
            </p>
          </div>
        )}
      </section>

      <section className="rounded-xl border border-border bg-bg-secondary p-4">
        <div className="flex items-center gap-2">
          <ShieldCheck size={15} className="text-accent-warm" />
          <h3 className="text-xs font-semibold text-text-primary">Queue a clip</h3>
        </div>
        <p className="mt-1 text-[11px] text-text-muted">
          Queued work waits if the computer is off and runs when it comes back — nothing is lost and
          no credits are spent.
        </p>
        <textarea
          value={prompt}
          onChange={event => setPrompt(event.target.value)}
          rows={3}
          className="mt-2 w-full rounded-lg border border-border bg-bg-tertiary px-3 py-2 text-xs text-text-primary focus:border-accent-warm focus:outline-none"
        />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 text-[10px] text-text-muted">
            seconds
            <input
              type="number"
              min={1}
              max={10}
              value={seconds}
              onChange={event => setSeconds(Number(event.target.value))}
              className="w-16 rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary"
            />
          </label>
          <button
            type="button"
            disabled={busy !== '' || !prompt.trim() || !selected}
            onClick={() =>
              void act('Queueing', async () => {
                const result = await queueLocalClip({
                  prompt: prompt.trim(),
                  modelId: selected?.model_id,
                  baseUrl: baseUrl.trim() || undefined,
                  seconds,
                })
                setQueued(
                  result.created
                    ? `Queued as ${result.job.job_id} (${result.job.state}). It runs when the engine is up.`
                    : result.reason,
                )
              })
            }
            className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-40"
          >
            {busy === 'Queueing' ? <Loader2 size={12} className="animate-spin" /> : <Clapperboard size={12} />}
            Add to the queue
          </button>
          {queued && <span className="text-[10px] text-text-secondary">{queued}</span>}
        </div>
        <Note>
          Progress in the queue below is ComfyUI&apos;s own state. Its local HTTP API reports no step
          percentage, so the job shows its state rather than a bar we made up.
        </Note>
      </section>
    </div>
  )
}
