const {
  isCpuOnly,
  isMac,
  isRtx50,
  isSolCapable,
  needsCuda13DriverUpdate,
  runtimeProfile,
} = require("./launcher_profile")

module.exports = async (kernel) => {
  const runtime = runtimeProfile(kernel)
  const cpuOnly = isCpuOnly(kernel)
  const mac = isMac(kernel)
  return {
    requires: {
      bundle: "ai"
    },
    run: [
    {
      // Pinokio's documented GPU variable stays the trigger, but it no longer
      // stops the install. A machine without an NVIDIA GPU still gets a
      // complete install: the studio, Films, editor, queue and remote engine
      // peers work, and the app's own startup preflight reports that local
      // generation needs CUDA. See docs/INSTALAR-SIN-NVIDIA.md.
      when: "{{gpu !== 'nvidia'}}",
      method: "notify",
      params: {
        // Interpolated here rather than with {{gpu}}: this step runs precisely
        // when that value is missing or unrecognized, so it must not depend on
        // the template engine resolving it.
        html: mac
          ? `macOS detected (${kernel.gpu || "Apple GPU"}). Installing Maestro's `
            + "CPU / light runtime: planning, Studio, Films, the editor, the queue "
            + "and remote engine peers all work. Local AI generation needs an "
            + "NVIDIA GPU, so it stays on another machine. Needs an Apple Silicon "
            + "Mac on macOS 14 or newer. See docs/INSTALAR-EN-MAC.md."
          : cpuOnly
            ? `No NVIDIA GPU reported (${kernel.gpu || "not reported"}). Installing `
              + "Maestro's CPU / light runtime: planning, Studio, Films, the editor, "
              + "the queue and remote engine peers all work, but local AI generation "
              + "needs an NVIDIA GPU. See docs/INSTALAR-SIN-NVIDIA.md."
            : `Pinokio did not report an NVIDIA GPU (${kernel.gpu || "not reported"}). `
              + "Continuing with Maestro's standard NVIDIA runtime. If this machine "
              + "has no NVIDIA GPU, set MAESTRO_CPU_RUNTIME=1 in Pinokio's ENVIRONMENT "
              + "file and run Install again to get the lighter CPU runtime instead."
      }
    },
    {
      // Blackwell (RTX 50) has no CUDA 12.8 fallback for the H3 runtime, so this
      // stays a stop-and-explain gate.
      when: isRtx50(kernel) && needsCuda13DriverUpdate(kernel),
      method: "notify",
      params: {
        html: `Your NVIDIA driver (${kernel.gpu_driver}) is too old for Maestro's CUDA 13 runtime. Update to NVIDIA driver 580 or newer, then run Install again.`
      },
      next: null
    },
    {
      // Every other NVIDIA card already has a working CUDA 12.8 compatibility
      // runtime (runtimeProfile selects it below), so warn instead of blocking
      // an install that can still generate.
      when: isSolCapable(kernel) && !isRtx50(kernel) && needsCuda13DriverUpdate(kernel),
      method: "notify",
      params: {
        html: `Your NVIDIA driver (${kernel.gpu_driver}) is too old for Maestro's CUDA 13 H3 performance runtime, so Install will use the CUDA 12.8 compatibility runtime instead. Update to NVIDIA driver 580 or newer and run Update afterwards to get the faster runtime.`
      }
    },
    {
      method: "shell.run",
      params: {
        venv: runtime.env,
        venv_python: runtime.python,
        path: "app",
        message: [
          "uv pip install -r requirements.txt --index-strategy unsafe-best-match",
          "uv pip install hf-xet pip"
        ]
      }
    },
    {
      method: "script.start",
      params: {
        uri: "torch.js",
        params: {
          venv: runtime.env,
          path: "app",
          xformers: true
        }
      }
    },
    // Install pre-built llama.cpp CUDA kernels for GGUF models if a
    // wheel matches the current Python / PyTorch / CUDA combo. Without
    // this, mmgp prints "[GGUF][llama.cpp CUDA] kernels unavailable,
    // using fallback" at every startup. The helper script is a soft
    // no-op when no matching wheel exists (e.g. an unreleased version
    // combo) — the fallback path still works for GGUF models,
    // and the default INT8 / BF16 variants don't use these kernels at
    // all. Idempotent on re-runs.
    {
      method: "shell.run",
      params: {
        venv: runtime.env,
        venv_python: runtime.python,
        path: "app",
        message: "python scripts/install_gguf_kernels.py"
      }
    },
    // Fetch the seed-vc voice-conversion component (GPL-3.0). It lives in
    // its own repository and is cloned into place at install time instead
    // of being tracked in this repo, so the GPL-licensed tree keeps its own
    // license and distribution channel. Pinned to a tag for reproducible
    // installs — bump the tag here AND in update.js when shipping a new
    // component version.
    {
      when: "{{!exists('app/postprocessing/seedvc/__init__.py')}}",
      method: "shell.run",
      params: {
        message: "git clone --depth 1 --branch v1.0.0 https://github.com/Blizaine/maestro-seedvc app/postprocessing/seedvc"
      }
    },
    {
      when: "{{exists('ui/package.json')}}",
      method: "shell.run",
      params: {
        path: "ui",
        message: [
          "npm install",
          "npm run build"
        ]
      }
    },
    // SAM 3.1 segmentation service (used by experimental Inpaint mode)
    // is intentionally NOT installed here. It adds ~5+ minutes to a
    // fresh install (separate Python 3.12 conda env, torch wheels,
    // SAM 3 source, etc.) but is only needed for the inpaint feature
    // which most users won't touch — and which is gated behind the
    // experimental flag in Settings → Services anyway. Users who want
    // it can run "Install Inpaint Support" from the Pinokio menu when
    // they're ready, which fires sam_install.js.
    {
      method: 'input',
      params: {
        title: 'Installation completed',
        description: mac
          ? 'Install finished with the macOS CPU / light runtime. Click "Start": every '
            + 'part of Maestro except local AI generation is available on this Mac (see '
            + 'docs/INSTALAR-EN-MAC.md).'
          : cpuOnly
            ? 'Install finished with the CPU / light runtime. Click "Start": every '
              + 'part of Maestro except local AI generation is available (see '
              + 'docs/INSTALAR-SIN-NVIDIA.md).'
            : 'Click "Start" to get started'
      }
    }
    ]
  }
}
