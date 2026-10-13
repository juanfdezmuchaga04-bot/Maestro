// Optional user-owned private HTTPS access. This configures Tailscale Serve;
// it never enables Funnel and therefore never exposes Maestro publicly.
module.exports = async (kernel) => {
  const os = require("os")
  const isWindows = kernel.platform === "win32"
  const isMac = kernel.platform === "darwin"
  // The macOS Tailscale app ships its CLI inside the .app bundle and only adds a
  // `tailscale` symlink to /usr/local/bin when the user installs the optional
  // command-line tooling. Without this fallback a Mac would be told that
  // Tailscale is not installed while it is running, which matters more here than
  // on Windows or Linux: private HTTPS is how an iPhone or iPad reaches Maestro.
  const macTailscale = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
  const tailscale = kernel.which("tailscale")
    || (isMac ? macTailscale : "tailscale")
  // Same gate the launcher uses for the command, so the action appears as soon
  // as either the PATH binary or the app bundle exists. Parenthesized because
  // it is combined with `&&` below.
  const tailscaleAvailable = isMac
    ? `which('tailscale') || exists('${macTailscale}')`
    : "which('tailscale')"
  const tailscaleReady = `(${tailscaleAvailable})`
  const powershell = kernel.which("powershell") || "powershell"
  const windowsUsername = kernel.envs.USERNAME || os.userInfo().username
  const windowsUser = [kernel.envs.USERDOMAIN, windowsUsername]
    .filter(Boolean)
    .join("\\")
  const setupCommand = isWindows ? {
    _: [
      powershell,
      "-NoProfile",
      "-NonInteractive",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      "scripts/tailscale_windows_setup.ps1",
      "-TailscalePath",
      tailscale,
      "-Port",
      "{{args.port}}",
      "-UserId",
      windowsUser,
    ],
  } : {
    _: [
      tailscale,
      "serve",
      "--bg",
      "--yes",
      "--https=443",
      "http://127.0.0.1:{{args.port}}",
    ],
  }

  return {
    run: [{
      when: `{{!${tailscaleReady}}}`,
      method: "input",
      params: {
        title: "Install Tailscale first",
        description: "Install the free Tailscale app from https://tailscale.com/download on this computer, sign in to your own account, then run this action again.",
      },
      next: null,
    }, {
      // The launcher keeps this action visible while Maestro is stopped so
      // users can discover it. Route creation must still wait for start.js to
      // capture the actual dynamic server port.
      when: `{{${tailscaleReady} && !args.port}}`,
      method: "input",
      params: {
        title: "Start Maestro first",
        description: "Tailscale is installed. Start Maestro, then return to its Pinokio page and choose Secure Remote Access again. The action will use Maestro's actual running port.",
      },
      next: null,
    }, {
      when: `{{${tailscaleReady} && args.port}}`,
      method: "shell.run",
      params: {
        path: ".",
        sudo: true,
        // Windows additionally registers one fixed, on-demand scheduled task.
        // Later Maestro starts can ask Task Scheduler to restore this exact
        // route without another UAC prompt. macOS and Linux run the same
        // `tailscale serve --bg` command, which is persistent on its own.
        message: setupCommand,
      },
    }, {
      when: `{{${tailscaleReady} && args.port}}`,
      method: "json.set",
      params: {
        "app/settings/remote_access.json": {
          version: 3,
          enabled: true,
          target_port: "{{args.port}}",
          pinokio_port_lock: true,
          windows_restore_task: isWindows,
          windows_restore_task_name: isWindows
            ? "Maestro Tailscale Serve"
            : null,
        },
      },
    }, {
      when: `{{${tailscaleReady} && args.port}}`,
      method: "notify",
      params: {
        html: "Private Maestro access is ready. Future Maestro starts will restore it automatically without another approval prompt. Open Maestro Settings → Notifications to copy or scan the secure URL and finish phone notification setup.",
      },
    }],
  }
}
