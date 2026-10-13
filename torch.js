const {
  isCpuOnly,
  isMac,
  isRtx50,
  needsCuda13DriverUpdate,
  runtimeProfile,
} = require("./launcher_profile")

module.exports = async (kernel) => {
  const runtime = runtimeProfile(kernel)
  const cpuOnly = isCpuOnly(kernel)
  const mac = isMac(kernel)
  // The CUDA 13 wheel set belongs to the Python 3.11 performance environments.
  // app/env is a Python 3.10 CUDA 12.8 environment and app/env-light is CPU
  // only, so the wheel set follows the environment that was actually selected.
  // That is also what lets a supported GPU on an older driver fall back to the
  // CUDA 12.8 runtime it can run instead of failing the whole install.
  const cuda13Runtime = runtime.env === "env-sol" || runtime.env === "env-rtx50"
  const windows = kernel.platform === "win32"
  const linux = kernel.platform === "linux"

  // macOS routes to the light runtime (see launcher_profile.js), so a Mac still
  // reaches the CPU branch below. Only a genuinely unknown platform is refused.
  if (!windows && !linux && !mac) {
    throw new Error(
      cpuOnly
        ? "Maestro's CPU / light runtime is supported on Windows, Linux and macOS."
        : "Maestro's NVIDIA runtime is supported on Windows and Linux."
    )
  }
  if (isRtx50(kernel) && needsCuda13DriverUpdate(kernel)) {
    throw new Error(
      `NVIDIA driver ${kernel.gpu_driver} is too old for Maestro's CUDA 13 H3 runtime. ` +
      "Install NVIDIA driver 580 or newer, then run Update again."
    )
  }

  let message
  let flashMessage
  let flashInstalled = true
  let optionalMessage = null
  let env = undefined
  const verifyMessage = cuda13Runtime
    ? "python scripts/verify_sol_runtime.py"
    : null

  const cudaArch = ({
    sm_89: "8.9",
    sm_90: "9.0",
    sm_100: "10.0",
    sm_120: "12.0",
  })[String(kernel.gpu_target || "").toLowerCase()] || "8.9"

  if (cpuOnly) {
    // CPU wheels from PyTorch's own index, matching the legacy environment's
    // Python 3.10 / Torch 2.7.1 combination so the dependency set in
    // app/requirements.txt stays identical on every machine. CUDA-only
    // packages (Triton, SageAttention, Nunchaku, lightx2v kernels) are skipped:
    // there is no GPU for them to run on.
    //
    // The same index also publishes the macOS arm64 wheels
    // (torch-2.7.1-cp310-none-macosx_11_0_arm64.whl), and those are the builds
    // that expose the Metal/MPS backend — so a Mac gets the identical command.
    message = [
      "uv pip install torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1 --index-url https://download.pytorch.org/whl/cpu --force-reinstall --no-deps",
      "uv pip install numpy==2.1.2",
    ]
    flashMessage = "uv pip uninstall flash-attn"
    flashInstalled = false
  } else if (cuda13Runtime && windows) {
    message = [
      // H3 Sol Engine and Blackwell's native NVFP4 path share this tested
      // Python 3.11 / CUDA 13 / Torch 2.10 ABI.
      "uv pip install torch==2.10.0 torchvision==0.25.0 torchaudio==2.10.0 --index-url https://download.pytorch.org/whl/cu130 --force-reinstall --no-deps",
      "{{args && args.xformers ? 'uv pip install xformers==0.0.35 --index-url https://download.pytorch.org/whl/cu130 --force-reinstall --no-deps' : ''}}",
      "uv pip install triton-windows==3.6.0.post25 --force-reinstall",
      "uv pip install https://github.com/woct0rdho/SageAttention/releases/download/v2.2.0-windows.post4/sageattention-2.2.0+cu130torch2.9.0andhigher.post4-cp39-abi3-win_amd64.whl --force-reinstall --no-deps",
      "uv pip install https://github.com/deepbeepmeep/kernels/releases/download/Light2xv/lightx2v_kernel-0.0.2+torch2.10.0-cp311-abi3-win_amd64.whl --force-reinstall --no-deps",
      "uv pip install https://github.com/nunchaku-ai/nunchaku/releases/download/v1.2.1/nunchaku-1.2.1+cu13.0torch2.10-cp311-cp311-win_amd64.whl --force-reinstall --no-deps",
    ]
    flashMessage = "uv pip install https://github.com/deepbeepmeep/kernels/releases/download/Flash2/flash_attn-2.8.3-cp311-cp311-win_amd64.whl --force-reinstall --no-deps"
  } else if (cuda13Runtime && linux) {
    message = [
      "uv pip install torch==2.10.0 torchvision==0.25.0 torchaudio==2.10.0 --index-url https://download.pytorch.org/whl/cu130 --force-reinstall --no-deps",
      "{{args && args.xformers ? 'uv pip install xformers==0.0.35 --index-url https://download.pytorch.org/whl/cu130 --force-reinstall --no-deps' : ''}}",
      "uv pip install 'triton>=3.6,<3.7' --force-reinstall",
      "uv pip install https://github.com/deepbeepmeep/kernels/releases/download/Light2xv/lightx2v_kernel-0.0.2+torch2.10.0-cp311-abi3-linux_x86_64.whl --force-reinstall --no-deps",
      "uv pip install https://github.com/nunchaku-ai/nunchaku/releases/download/v1.2.1/nunchaku-1.2.1+cu13.0torch2.10-cp311-cp311-linux_x86_64.whl --force-reinstall --no-deps",
    ]
    // PyTorch's cu130 wheel does not provide nvcc. Compiling either package
    // against a distro CUDA 12.x toolkit fails before the runtime markers are
    // written and leaves Pinokio offering the same upgrade forever. Install
    // the tested Linux wheels through a guarded helper instead; both packages
    // remain optional because H3 Sol uses Maestro's bundled Triton kernels.
    optionalMessage = "python scripts/install_optional_cuda_acceleration.py"
    flashMessage = "python scripts/install_optional_cuda_acceleration.py --flash-only"
    env = {
      TORCH_CUDA_ARCH_LIST: cudaArch,
      MAX_JOBS: "4",
    }
  } else if (windows) {
    // Preserve the known-good public runtime for RTX 20/30/40 systems.
    message = [
      "uv pip install torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1 {{args && args.xformers ? 'xformers==0.0.30' : ''}} --index-url https://download.pytorch.org/whl/cu128 --force-reinstall --no-deps",
      "uv pip install triton-windows==3.3.1.post19",
      "uv pip install https://github.com/woct0rdho/SageAttention/releases/download/v2.2.0-windows/sageattention-2.2.0+cu128torch2.7.1-cp310-cp310-win_amd64.whl",
    ]
    if (runtime.flashSupported) {
      // Match WanGP's documented Python 3.10 / Torch 2.7.1 / CUDA 12.8 ABI.
      flashMessage = "uv pip install https://github.com/Redtash1/Flash_Attention_2_Windows/releases/download/v2.7.0-v2.7.4/flash_attn-2.7.4.post1+cu128torch2.7.0cxx11abiFALSE-cp310-cp310-win_amd64.whl --force-reinstall --no-deps"
    } else {
      // This wheel contains only sm_89 cubins. On RTX 30/Ampere and older
      // GPUs it imports but fails at its first CUDA launch, so remove it and
      // let Maestro use SageAttention/SDPA instead.
      flashMessage = "uv pip uninstall flash-attn"
      flashInstalled = false
    }
  } else {
    message = [
      "uv pip install torch==2.7.0 torchvision==0.22.0 torchaudio==2.7.0 {{args && args.xformers ? 'xformers==0.0.30' : ''}} --index-url https://download.pytorch.org/whl/cu128 --force-reinstall",
      "uv pip install https://huggingface.co/MonsterMMORPG/SECourses_Premium_Flash_Attention/resolve/main/sageattention-2.1.1-cp310-cp310-linux_x86_64.whl",
      "uv pip install numpy==2.1.2",
    ]
    flashMessage = "uv pip install https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/download/v0.7.16/flash_attn-2.7.4+cu128torch2.7-cp310-cp310-linux_x86_64.whl --force-reinstall --no-deps"
  }

  return {
    run: [
      {
        method: "log",
        params: {
          raw: `Installing Maestro's ${runtime.label} runtime...`,
        },
      },
      {
        method: "shell.run",
        when: "{{!args || !args.flash_only}}",
        params: {
          venv: "{{args && args.venv ? args.venv : null}}",
          path: "{{args && args.path ? args.path : '.'}}",
          ...(env ? { env } : {}),
          message: optionalMessage ? message : [...message, flashMessage],
        },
      },
      ...(optionalMessage ? [{
        // Optional attention packages must never invalidate an otherwise
        // working CUDA 13 / Triton Sol runtime. The helper uses prebuilt
        // wheels and converts download/ABI failures into a clear fallback
        // notice so the required readiness markers can still be written.
        method: "shell.run",
        when: "{{!args || !args.flash_only}}",
        params: {
          venv: "{{args && args.venv ? args.venv : null}}",
          path: "{{args && args.path ? args.path : '.'}}",
          ...(env ? { env } : {}),
          message: optionalMessage,
        },
      }] : []),
      {
        // Update can repair only the optional FlashAttention wheel without
        // redownloading Torch, Triton, SageAttention, or the model kernels.
        method: "shell.run",
        when: "{{args && args.flash_only}}",
        params: {
          venv: "{{args && args.venv ? args.venv : null}}",
          path: "{{args && args.path ? args.path : '.'}}",
          ...(env ? { env } : {}),
          message: flashMessage,
        },
      },
      ...(verifyMessage ? [{
        // Do not publish the main runtime marker merely because package
        // installation commands returned. Verify the exact Python/Torch/CUDA,
        // Triton, GPU, and Sol capability contract first.
        method: "shell.run",
        when: "{{!args || !args.flash_only}}",
        params: {
          venv: "{{args && args.venv ? args.venv : null}}",
          path: "{{args && args.path ? args.path : '.'}}",
          message: verifyMessage,
        },
      }] : []),
      {
        // update.js uses this hardware-specific marker to avoid unnecessary
        // multi-gigabyte reinstalls while still making interrupted migrations
        // resumable.
        method: "fs.write",
        when: "{{!args || !args.flash_only}}",
        params: {
          path: runtime.marker,
          text: `Maestro ${runtime.label} runtime installed. Delete this file and run Update to reinstall it.`,
        },
      },
      {
        method: "fs.write",
        params: {
          path: runtime.flashMarker,
          text: optionalMessage
            ? `Maestro ${runtime.label} optional attention packages checked. Delete this file and run Update to retry them.`
            : flashInstalled
              ? `Maestro ${runtime.label} FlashAttention wheel installed. Delete this file and run Update to repair it.`
              : cpuOnly
                ? (mac
                  ? `Maestro ${runtime.label} skips FlashAttention: it needs an NVIDIA GPU. Generation stays on a CUDA machine; on macOS the Apple GPU is not used for it, and everything else works.`
                  : `Maestro ${runtime.label} skips FlashAttention: it needs an NVIDIA GPU. Local generation requires CUDA; everything else works.`)
                : `Maestro ${runtime.label} uses SageAttention/SDPA because the bundled FlashAttention wheel does not support this GPU.`,
        },
      },
    ],
  }
}
