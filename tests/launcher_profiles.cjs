"use strict";

// Launcher hardware matrix: executes the real Pinokio scripts with simulated
// kernel inventories and asserts what each machine actually gets.
//
// The regression this pins down: Install used to STOP on any machine where
// Pinokio did not report `gpu === "nvidia"`, so the app could not be installed
// at all from Pinokio on an AMD/Intel machine, and a supported GPU with an
// older driver was blocked from the CUDA 12.8 runtime it can actually run.
// torch.js separately refused macOS outright, so a Mac could not be installed
// either; it now uses the same CPU-index wheels, which are the macOS builds
// that expose Metal/MPS.
//
// Run with: node tests/launcher_profiles.cjs

const assert = require("node:assert/strict");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const profile = require(path.join(ROOT, "launcher_profile.js"));
const install = require(path.join(ROOT, "install.js"));
const torch = require(path.join(ROOT, "torch.js"));
const start = require(path.join(ROOT, "start.js"));
const startClassic = require(path.join(ROOT, "start_classic.js"));
const update = require(path.join(ROOT, "update.js"));
const reset = require(path.join(ROOT, "reset.js"));
const launcher = require(path.join(ROOT, "pinokio.js"));

const baseKernel = (overrides = {}) => ({
  gpu: "nvidia",
  gpu_target: "sm_89",
  gpu_driver: "580",
  platform: "win32",
  port: async () => 7860,
  ...overrides,
});

const MATRIX = {
  rtx4090: {
    kernel: baseKernel(),
    env: "env-sol",
    python: "3.11",
    marker: "app/env-sol/.maestro_sol_runtime_v1.installed",
    cuda: "cu130",
  },
  rtx5090: {
    kernel: baseKernel({ gpu_target: "sm_120", gpu_driver: "581" }),
    env: "env-rtx50",
    python: "3.11",
    marker: "app/env-rtx50/.maestro_torch_rtx50_v2.installed",
    cuda: "cu130",
  },
  // Ampere on an older driver: never Sol-capable, keeps the legacy runtime.
  rtx3060_old_driver: {
    kernel: baseKernel({ gpu_target: "sm_86", gpu_driver: "560" }),
    env: "env",
    python: "3.10",
    marker: "app/env/.maestro_torch_v1.installed",
    cuda: "cu128",
  },
  // Ada on an older driver: Sol-capable in hardware, but CUDA 13 needs driver
  // 580+, so the install must fall back to app/env instead of aborting.
  rtx4070_old_driver: {
    kernel: baseKernel({ gpu_target: "sm_89", gpu_driver: "570" }),
    env: "env",
    python: "3.10",
    marker: "app/env/.maestro_torch_v1.installed",
    cuda: "cu128",
  },
  // Blackwell has no CUDA 12.8 fallback: the one intentional stop remains.
  rtx5090_old_driver: {
    kernel: baseKernel({ gpu_target: "sm_120", gpu_driver: "570" }),
    env: "env-rtx50",
    python: "3.11",
    marker: "app/env-rtx50/.maestro_torch_rtx50_v2.installed",
    cuda: "cu130",
    installStops: true,
  },
  radeon: {
    kernel: baseKernel({ gpu: "amd", gpu_target: "", gpu_driver: "" }),
    env: "env-light",
    python: "3.10",
    marker: "app/env-light/.maestro_torch_cpu_v1.installed",
    cpu: true,
  },
  intel_linux: {
    kernel: baseKernel({ gpu: "intel", gpu_target: "", gpu_driver: "", platform: "linux" }),
    env: "env-light",
    python: "3.10",
    marker: "app/env-light/.maestro_torch_cpu_v1.installed",
    cpu: true,
  },
  // Pinokio's inventory can be empty. That must never be read as "no NVIDIA":
  // the NVIDIA runtime still installs and the app reports the missing CUDA.
  unknown_inventory: {
    kernel: baseKernel({ gpu: "", gpu_target: "", gpu_driver: "" }),
    env: "env",
    python: "3.10",
    marker: "app/env/.maestro_torch_v1.installed",
    cuda: "cu128",
  },
  unknown_inventory_linux: {
    kernel: baseKernel({ gpu: undefined, gpu_target: "", gpu_driver: "", platform: "linux" }),
    env: "env",
    python: "3.10",
    marker: "app/env/.maestro_torch_v1.installed",
    cuda: "cu128",
  },
  // Explicit user override, for a GPU Pinokio reports as unknown.
  forced_cpu: {
    kernel: baseKernel({ envs: { MAESTRO_CPU_RUNTIME: "1" } }),
    env: "env-light",
    python: "3.10",
    marker: "app/env-light/.maestro_torch_cpu_v1.installed",
    cpu: true,
  },
  // macOS (Pinokio reports platform "darwin"). There is no CUDA for the
  // pipeline, so the platform alone must be enough to install and start.
  mac_apple_silicon: {
    kernel: baseKernel({ platform: "darwin", gpu: "apple", gpu_target: "", gpu_driver: "" }),
    env: "env-light",
    python: "3.10",
    marker: "app/env-light/.maestro_torch_cpu_v1.installed",
    cpu: true,
  },
  mac_intel: {
    kernel: baseKernel({ platform: "darwin", gpu: "intel", gpu_target: "", gpu_driver: "" }),
    env: "env-light",
    python: "3.10",
    marker: "app/env-light/.maestro_torch_cpu_v1.installed",
    cpu: true,
  },
  // A Mac whose GPU inventory never populated must still route to the light
  // runtime: reading the empty value as "maybe NVIDIA" would install a CUDA
  // environment that cannot run on macOS at all.
  mac_unknown_inventory: {
    kernel: baseKernel({ platform: "darwin", gpu: "", gpu_target: "", gpu_driver: "" }),
    env: "env-light",
    python: "3.10",
    marker: "app/env-light/.maestro_torch_cpu_v1.installed",
    cpu: true,
  },
};

const flatten = (value) => (Array.isArray(value) ? value.flatMap(flatten) : value ? [value] : []);

const shellMessages = (config) =>
  flatten(
    config.run
      .filter((step) => step.method === "shell.run")
      .map((step) => step.params && step.params.message),
  ).join("\n");

// Pinokio evaluates `when` at run time. Boolean conditions computed from the
// kernel are already resolved here, so honor them; string templates are left to
// the launcher itself.
const stepApplies = (step) => {
  if (step.when === undefined) return true;
  if (typeof step.when === "boolean") return step.when;
  return true;
};

const stopSteps = (config) =>
  config.run.filter((step) => step.next === null && stepApplies(step));

const installNotice = (config) =>
  config.run.find((step) => step.method === "notify" && step.when === "{{gpu !== 'nvidia'}}");

const serverStep = (config) =>
  config.run.find(
    (step) => step.method === "shell.run" && flatten(step.params && step.params.message)
      .some((line) => String(line).includes("launch.py")),
  );

const markerWrites = (config) =>
  config.run.filter((step) => step.method === "fs.write").map((step) => step.params.path);

const venvValues = (config) =>
  config.run
    .filter((step) => step.params && step.params.venv !== undefined)
    .map((step) => step.params.venv);

function menuInfo(present = []) {
  return {
    exists: (target) => present.includes(target),
    running: () => false,
    local: () => null,
  };
}

async function checkProfile(name, entry) {
  const { kernel, env, python, marker, cpu, cuda, installStops } = entry;
  const runtime = profile.runtimeProfile(kernel);
  const label = `${name}: `;

  assert.equal(runtime.env, env, `${label}environment`);
  assert.equal(runtime.python, python, `${label}python`);
  assert.equal(runtime.marker, marker, `${label}marker`);
  assert.equal(profile.isCpuOnly(kernel), Boolean(cpu), `${label}cpuOnly`);

  // 1. Install must reach "Installation completed" on every machine except the
  //    documented Blackwell + old-driver gate.
  const installConfig = await install(kernel);
  const stops = stopSteps(installConfig);
  if (installStops) {
    assert.equal(stops.length, 1, `${label}expected exactly one intentional stop`);
    assert.equal(stops[0].method, "notify");
    assert.match(stops[0].params.html, /driver/i, `${label}stop must explain the driver`);
    assert.match(stops[0].params.html, /580/, `${label}stop must name the required driver`);
  } else {
    assert.deepEqual(
      stops.map((step) => step.method),
      [],
      `${label}Install must never stop on this hardware`,
    );
  }
  const completion = installConfig.run[installConfig.run.length - 1];
  assert.equal(completion.method, "input", `${label}install ends in a completion prompt`);
  assert.equal(completion.params.title, "Installation completed", `${label}completion title`);

  // 2. The NVIDIA notice keeps Pinokio's documented variable and never stops.
  const notice = installNotice(installConfig);
  assert.ok(notice, `${label}keeps the documented {{gpu}} notice`);
  assert.equal(notice.next, undefined, `${label}notice must not stop the install`);
  assert.equal(notice.params.next, undefined, `${label}notice has no nested stop`);

  // 3. Every install/update step targets the selected environment.
  const installVenvs = venvValues(installConfig).filter(
    (value) => typeof value === "string" && !value.startsWith("{{"),
  );
  assert.ok(installVenvs.includes(env), `${label}install uses ${env}`);
  assert.ok(venvValues(await update(kernel)).includes(env), `${label}update uses ${env}`);
  const torchStep = installConfig.run.find(
    (step) => step.method === "script.start" && step.params.uri === "torch.js",
  );
  assert.ok(torchStep, `${label}install delegates the runtime to torch.js`);
  assert.equal(torchStep.params.params.venv, env, `${label}torch.js runs in ${env}`);

  // 4. Torch wheel set matches the environment (and the CPU path never pulls
  //    CUDA-only kernels into a machine that has no GPU).
  if (installStops) {
    // The same combination Install refuses to set up must also be refused by
    // torch.js itself, so Update cannot install an unrunnable runtime either.
    await assert.rejects(
      () => torch(kernel),
      /too old for Maestro's CUDA 13 H3 runtime/,
      `${label}torch.js refuses the unrunnable combination too`,
    );
    return;
  }
  const torchConfig = await torch(kernel);
  const torchMessages = shellMessages(torchConfig);
  assert.ok(
    markerWrites(torchConfig).includes(runtime.marker)
      && markerWrites(torchConfig).includes(runtime.flashMarker),
    `${label}torch publishes both markers`,
  );
  if (cpu) {
    assert.match(torchMessages, /--index-url https:\/\/download\.pytorch\.org\/whl\/cpu/, label + "CPU wheels");
    assert.match(torchMessages, /torch==2\.7\.1/, `${label}CPU torch pin`);
    assert.match(torchMessages, /torchvision==0\.22\.1/, `${label}CPU torchvision pin`);
    assert.match(torchMessages, /torchaudio==2\.7\.1/, `${label}CPU torchaudio pin`);
    for (const cudaOnly of ["triton", "nunchaku", "sageattention", "lightx2v_kernel"]) {
      assert.ok(
        !torchMessages.toLowerCase().includes(cudaOnly),
        `${label}CPU runtime must not install ${cudaOnly}`,
      );
    }
    assert.ok(
      !torchConfig.run.some((step) => step.method === "shell.run" && step.params.env),
      `${label}CPU runtime sets no CUDA build env`,
    );
  } else {
    assert.ok(torchMessages.includes(cuda), `${label}uses the ${cuda} wheel index`);
  }

  // 5. Start uses the same environment; the CPU runtime is never a fallback
  //    away from, and is guarded by its own marker.
  const startConfig = await start(kernel);
  const server = serverStep(startConfig);
  assert.ok(server, `${label}start launches launch.py`);
  if (cpu) {
    assert.equal(server.params.venv, env, `${label}start pins ${env}`);
    assert.equal(server.params.venv_python, python, `${label}start pins python ${python}`);
    const guard = startConfig.run.find(
      (step) => step.method === "input"
        && String(step.params.title) === "CPU / light runtime not installed",
    );
    assert.ok(guard, `${label}start guards a missing light runtime`);
    assert.equal(guard.next, null, `${label}guard stops before launching`);
    assert.equal(guard.when, `{{!exists('${marker}')}}`, `${label}guard is marker-driven`);
    assert.ok(
      startConfig.run.some((step) => step.method === "log"
        && /CPU \/ light runtime/.test(String(step.params.raw))),
      `${label}start reports the CPU runtime honestly`,
    );
  } else {
    const selected = String(server.params.venv);
    assert.ok(
      selected === env || selected.includes(`'${env}'`),
      `${label}start selects ${env} through its marker (got ${selected})`,
    );
  }
  assert.equal((await startClassic(kernel)).daemon, true, `${label}classic launcher daemon`);
  assert.equal(startConfig.daemon, true, `${label}daemon`);

  // 6. Reset removes the environment it would have created.
  assert.ok(
    reset.run.some((step) => step.method === "fs.rm" && step.params.path === `app/${env}`),
    `${label}reset removes app/${env}`,
  );

  // 7. The Pinokio menu offers Install, then Start, then keeps Start available.
  const fresh = await launcher.menu(kernel, menuInfo([]));
  assert.ok(
    fresh.some((item) => item.href === "install.js"),
    `${label}fresh menu offers Install`,
  );
  const installed = await launcher.menu(kernel, menuInfo([`app/${env}`, marker, runtime.flashMarker]));
  const startItem = installed.find((item) => item.href === "start.js");
  assert.ok(startItem, `${label}installed menu offers Start`);
  if (cpu) {
    assert.match(String(startItem.text), /CPU \/ light runtime/, `${label}Start advertises CPU mode`);
  } else {
    assert.equal(startItem.text, "Start", `${label}NVIDIA Start label unchanged`);
  }
}

(async () => {
  for (const [name, entry] of Object.entries(MATRIX)) {
    await checkProfile(name, entry);
  }

  // Cross-checks that are not per-kernel.
  //
  // macOS: platform alone decides, so an Apple Silicon Mac, an Intel Mac and a
  // Mac whose GPU inventory never populated all get the same usable runtime.
  const darwin = baseKernel({ gpu: "", gpu_target: "", gpu_driver: "", platform: "darwin" });
  assert.equal(profile.isMac(darwin), true, "darwin is recognized as macOS");
  assert.equal(profile.isCpuOnly(darwin), true, "macOS is CPU-only");
  assert.equal(profile.runtimeProfile(darwin).env, "env-light", "macOS routes to the light runtime");
  assert.match(profile.cpuRuntimeProfile(darwin).label, /macOS/, "macOS label names the platform");
  assert.doesNotMatch(
    profile.cpuRuntimeProfile(baseKernel({ gpu: "amd", gpu_target: "" })).label,
    /macOS/,
    "non-macOS CPU machines keep the plain light label",
  );
  // The runtime is deliberately not called "MPS": the generation pipeline is
  // CUDA-only, so a Mac must never look like a machine that can render locally.
  assert.doesNotMatch(profile.cpuRuntimeProfile(darwin).label, /MPS/);
  assert.doesNotMatch(
    String((await install(darwin)).run[0].params.html),
    /MPS/,
    "the macOS notice must not imply Metal generation",
  );
  const darwinTorch = await torch(darwin);
  const darwinMessages = shellMessages(darwinTorch);
  assert.match(
    darwinMessages,
    /--index-url https:\/\/download\.pytorch\.org\/whl\/cpu/,
    "macOS uses the CPU index, whose macOS wheels are the Metal/MPS builds",
  );
  assert.match(darwinMessages, /torch==2\.7\.1/, "macOS torch pin");
  assert.doesNotMatch(
    darwinMessages,
    /triton|nunchaku|sageattention|lightx2v/i,
    "macOS installs no CUDA-only kernels",
  );
  const darwinInstall = await install(darwin);
  assert.match(
    String(installNotice(darwinInstall).params.html),
    /macOS/,
    "macOS installation notice explains the platform",
  );
  assert.deepEqual(stopSteps(darwinInstall), [], "macOS install still reaches the completion prompt");
  // An unknown platform is still refused instead of silently half-installing.
  await assert.rejects(
    () => torch(baseKernel({ platform: "freebsd", gpu: "", gpu_target: "", gpu_driver: "" })),
    /supported on Windows and Linux/,
  );
  await assert.rejects(
    () => torch(baseKernel({ platform: "freebsd", gpu: "amd", gpu_target: "", gpu_driver: "" })),
    /CPU \/ light runtime is supported on Windows, Linux and macOS/,
  );

  // Sol-only entry points keep refusing non-NVIDIA hardware.
  const solInstall = require(path.join(ROOT, "sol_install.js"));
  const startSol = require(path.join(ROOT, "start_sol.js"));
  const radeon = baseKernel({ gpu: "amd", gpu_target: "", gpu_driver: "" });
  await assert.rejects(() => solInstall(radeon), /requires an NVIDIA/);
  await assert.rejects(() => startSol(radeon), /requires an NVIDIA/);

  process.stdout.write(
    `Launcher hardware matrix passed for ${Object.keys(MATRIX).length} kernels + macOS/Sol guards.\n`,
  );

  // The macOS matrix above exercises the launcher; this pins the dependency set
  // a Mac actually installs, because a single un-installable requirement aborts
  // `uv pip install -r app/requirements.txt` and takes the whole install with it.
  const requirements = require("node:fs")
    .readFileSync(path.join(ROOT, "app", "requirements.txt"), "utf8")
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#") && !line.startsWith("--"));
  const darwinExcluded = (name) => requirements
    .filter((line) => new RegExp(`^${name}(\\[[^\\]]*\\])?[=<>!~ ]`).test(line))
    .every((line) => /sys_platform != "darwin"/.test(line));
  const darwinIncluded = (name) => requirements
    .filter((line) => new RegExp(`^${name}(\\[[^\\]]*\\])?[=<>!~ ]`).test(line))
    .some((line) => /sys_platform == "darwin"/.test(line));
  for (const name of ["onnxruntime-gpu", "decord"]) {
    assert.ok(
      darwinExcluded(name),
      `${name} must be excluded on macOS: it publishes no macOS wheel, and one `
        + "un-installable requirement aborts the whole install",
    );
  }
  assert.ok(
    requirements.every((line) => !line.startsWith("rembg[gpu]") || /sys_platform != "darwin"/.test(line)),
    "the rembg [gpu] extra must be excluded on macOS (it pulls onnxruntime-gpu)",
  );
  for (const name of ["onnxruntime", "eva-decord", "rembg"]) {
    assert.ok(darwinIncluded(name), `${name} must be installed on macOS`);
  }
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});