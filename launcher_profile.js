"use strict"

// Keep launcher-side hardware routing in one place. Pinokio exposes both a
// normalized GPU model and a CUDA architecture target before Python/PyTorch
// exists, so this works for fresh installs as well as upgrades.
const isRtx50 = (kernel = {}) => {
  const target = String(kernel.gpu_target || "").toLowerCase()
  const model = String(kernel.gpu_model || "").toLowerCase()
  return kernel.gpu === "nvidia" && (
    target === "sm_120" || /(?:geforce\s+)?rtx\s*50\d{2}/i.test(model)
  )
}

const isRtx40 = (kernel = {}) => {
  const target = String(kernel.gpu_target || "").toLowerCase()
  const model = String(kernel.gpu_model || "").toLowerCase()
  return kernel.gpu === "nvidia" && (
    target === "sm_89" || /(?:geforce\s+)?rtx\s*40\d{2}/i.test(model)
  )
}

const isSolCapable = (kernel = {}) => {
  const target = String(kernel.gpu_target || "").toLowerCase()
  return kernel.gpu === "nvidia" && (
    ["sm_89", "sm_90", "sm_100", "sm_120"].includes(target)
    || isRtx40(kernel)
    || isRtx50(kernel)
  )
}

const needsCuda13DriverUpdate = (kernel = {}) => {
  if (kernel.gpu !== "nvidia" || !kernel.gpu_driver) return false
  const driver = Number.parseFloat(String(kernel.gpu_driver))
  return Number.isFinite(driver) && driver < 580
}

// Pinokio on macOS reports platform "darwin" (Apple Silicon and Intel are both
// supported). There is no CUDA and no NVIDIA driver there, and WanGP's pipeline
// is CUDA-only, so a Mac always gets the light runtime: the whole non-generation
// surface works and the app's own preflight reports the missing CUDA. macOS is
// detected from the platform alone, so an empty GPU inventory on a Mac still
// routes correctly instead of installing an unusable CUDA environment.
const isMac = (kernel = {}) => String(kernel.platform || "").toLowerCase() === "darwin"

// Pinokio can leave `gpu` empty (hardware inventory still loading, or the probe
// failed) even on a supported NVIDIA machine, so an empty value must never be
// read as "not NVIDIA": that is what used to abort Install with a notice. Only
// an explicitly reported, non-NVIDIA adapter routes to the CPU/light runtime.
const reportedNonNvidiaGpu = (kernel = {}) => {
  const gpu = String(kernel.gpu ?? "").trim().toLowerCase()
  return gpu !== "" && gpu !== "nvidia"
}

// NVIDIA evidence in the model/target fields, used only when `gpu` is missing.
const hasNvidiaEvidence = (kernel = {}) => {
  if (kernel.gpu === "nvidia") return true
  const target = String(kernel.gpu_target || "").toLowerCase()
  const model = String(kernel.gpu_model || "").toLowerCase()
  return /^sm_\d+$/.test(target)
    || /(?:nvidia|geforce|quadro|tesla|\bgtx\b|\brtx\b|a100|h100|v100)/.test(model)
}

// MAESTRO_CPU_RUNTIME=1 in Pinokio's per-app or global ENVIRONMENT forces the
// CPU/light runtime, for machines whose GPU Pinokio reports as unknown or as a
// vendor whose name is not forwarded.
const cpuRuntimeForced = (kernel = {}) => {
  const flag = String((kernel.envs || {}).MAESTRO_CPU_RUNTIME ?? "").trim().toLowerCase()
  return ["1", "true", "yes", "on"].includes(flag)
}

// The CPU/light runtime keeps the whole non-generation surface usable (UI,
// Studio, Films, editor, queue, remote H3 peers) on machines without an NVIDIA
// GPU. Local AI generation still requires CUDA; the app reports that itself in
// its startup preflight.
const isCpuOnly = (kernel = {}) =>
  cpuRuntimeForced(kernel) || isMac(kernel) || reportedNonNvidiaGpu(kernel)

const legacyRuntimeProfile = (kernel = {}) => {
  if (isRtx50(kernel)) {
    return {
      env: "env-rtx50",
      python: "3.11",
      // v2 pins Triton 3.6 for the integrated H3 Sol Engine path. The marker
      // bump makes v1.7.5 Update migrate existing RTX 50 environments once.
      marker: "app/env-rtx50/.maestro_torch_rtx50_v2.installed",
      flashMarker: "app/env-rtx50/.maestro_flash_2_8_3_v1.installed",
      flashSupported: true,
      label: "RTX 50 / CUDA 13",
    }
  }
  const target = String(kernel.gpu_target || "").toLowerCase()
  const legacyWindowsFlashSupported = (
    kernel.platform !== "win32"
    || target === "sm_89"
    || isRtx40(kernel)
  )
  return {
    env: "env",
    python: "3.10",
    marker: "app/env/.maestro_torch_v1.installed",
    // The pinned Windows 2.7.4 wheel contains only sm_89 cubins. Bump the
    // marker for older GPUs so Update removes the incompatible package once.
    flashMarker: legacyWindowsFlashSupported
      ? "app/env/.maestro_flash_2_7_4_v1.installed"
      : "app/env/.maestro_flash_disabled_v2.installed",
    flashSupported: legacyWindowsFlashSupported,
    label: "CUDA 12.8 legacy",
  }
}

const solRuntimeProfile = (kernel = {}) => {
  if (isRtx50(kernel)) return legacyRuntimeProfile(kernel)
  return {
    env: "env-sol",
    python: "3.11",
    marker: "app/env-sol/.maestro_sol_runtime_v1.installed",
    flashMarker: "app/env-sol/.maestro_sol_flash_2_8_3_v1.installed",
    flashSupported: true,
    label: "H3 Sol Engine / CUDA 13",
  }
}

// Kept deliberately close to the legacy CUDA 12.8 environment: same Python
// 3.10, same dependency set, only the Torch build changes (CPU wheels instead of
// CUDA wheels) and the CUDA-only kernels are skipped. That keeps a single
// requirements.txt for every machine.
//
// macOS shares this environment: PyTorch's CPU index also publishes the macOS
// arm64 wheels, so a Mac gets a working Torch without a second environment to
// maintain. Only the human-readable label differs, and it deliberately does not
// say "MPS": the generation pipeline is CUDA-only and does not use the Metal
// backend, so nothing here should imply that a Mac can generate locally.
const cpuRuntimeProfile = (kernel = {}) => ({
  env: "env-light",
  python: "3.10",
  marker: "app/env-light/.maestro_torch_cpu_v1.installed",
  flashMarker: "app/env-light/.maestro_flash_disabled_cpu_v1.installed",
  flashSupported: false,
  label: isMac(kernel)
    ? "CPU / light (macOS, no NVIDIA GPU)"
    : "CPU / light (no NVIDIA GPU)",
})

// The tested CUDA 13 / Python 3.11 environment is now Maestro's preferred
// runtime on GPUs supported by H3 Sol Engine. Existing RTX 40 installations
// retain app/env as a recovery path; start.js falls back to it automatically
// until the normal Update flow finishes this side-by-side migration.
const runtimeProfile = (kernel = {}) => {
  if (isCpuOnly(kernel)) return cpuRuntimeProfile(kernel)
  return isSolCapable(kernel) && !needsCuda13DriverUpdate(kernel)
    ? solRuntimeProfile(kernel)
    : legacyRuntimeProfile(kernel)
}

module.exports = {
  cpuRuntimeForced,
  cpuRuntimeProfile,
  hasNvidiaEvidence,
  isCpuOnly,
  isMac,
  isRtx40,
  isRtx50,
  isSolCapable,
  legacyRuntimeProfile,
  needsCuda13DriverUpdate,
  reportedNonNvidiaGpu,
  runtimeProfile,
  solRuntimeProfile,
}
