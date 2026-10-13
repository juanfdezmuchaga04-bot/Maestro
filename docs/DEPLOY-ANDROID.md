# Maestro from your Android phone — production setup

Maestro is not a website with a cloud backend. **The application, your projects and
the generation engine run on your own computer; the phone is a client.** That is what
makes it free per generation and private by construction, and it also defines
everything below: the phone needs a route *to your computer*, and nothing works while
that computer is off.

| Piece | Where it runs | Notes |
| --- | --- | --- |
| Interface (React) + API (FastAPI) | your computer, one process | `app/launch.py`, port from `SERVER_PORT` (default 7860) |
| Projects, media, job queue | your computer, `outputs/` | nothing is uploaded anywhere |
| Video engine | your computer's GPU | self-hosted ComfyUI + Wan 2.2, see [`COMFYUI-LOCAL.md`](COMFYUI-LOCAL.md) |
| Phone client | Chrome on Android | installable as an app (PWA), signed in with an access password |

There is **no Maestro server to rent and no account to create**. Two optional
third-party services exist and neither is required nor paid: Tailscale (free for
personal use, only for private network access) and the browser's own Web Push.

---

## 1. Install Maestro on the computer

**Pinokio (recommended, no terminal).** Install [Pinokio](https://pinokio.computer),
find Maestro, press **Install** and then **Start**. Installation creates the Python
environment, downloads PyTorch and builds the web interface.

Requirements for **local generation**: **Windows or Linux with an NVIDIA GPU** (6 GB+ VRAM to
start, 8 GB for the Wan 2.2 preset). Install no longer stops without one: any other machine —
AMD/Intel, or a Mac — gets a CPU / light runtime (`app/env-light`) where everything except
local generation works, and the work is then sent to a machine with a CUDA GPU (see
[install without an NVIDIA GPU](INSTALAR-SIN-NVIDIA.md) and
[installing on a Mac](INSTALAR-EN-MAC.md)). On RTX 40/50 with a driver
older than 580 the launcher offers a compatibility runtime instead of the CUDA 13 one.
Also put **ffmpeg on PATH** — Maestro's own installation guide documents downloading a
build from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases) and
adding its `bin` folder to PATH (on Linux, `sudo apt install ffmpeg`); without it the
assembly and MP4 export steps stop with an explicit error.

**Manual (advanced — Pinokio's Install is the supported path).** A bare
`pip install -r app/requirements.txt` is *not* enough: the launcher also creates the
virtual environment, picks the CUDA PyTorch wheels for your GPU (`torch.js`: CUDA 13 for
RTX 40/50 on driver 580+, otherwise CUDA 12.8), installs the attention kernels, runs
`scripts/install_gguf_kernels.py`, clones the GPL seed-vc component into
`app/postprocessing/seedvc`, and builds the UI. Doing it by hand means following
`install.js` and `torch.js` step by step:

```bash
git clone <your-fork-url> maestro && cd maestro
# 1. Python environment (the equivalent of install.js)
cd app && uv venv env --python 3.10
uv pip install -r requirements.txt --index-strategy unsafe-best-match
# 2. CUDA PyTorch + kernels: use the `uv pip install` lines in ../torch.js for your GPU
# 3. Optional GPL component and GGUF kernels
python scripts/install_gguf_kernels.py
# 4. Web interface
cd ../ui && npm install && npm run build
# 5. Run (API + UI on SERVER_PORT, default 7860)
cd ../app && python launch.py
```

Then verify what actually works before touching the network:

```bash
python scripts/maestro_doctor.py
```

The doctor is read-only and prints one line per capability, with the reason for every
`[fail]`/`[?]` and a numbered "Next steps" list at the end. Run it again after any
change.

## 2. Set the access password (do this before exposing anything)

Maestro has no login on loopback by default — the browser and the server are the same
user. As soon as another device can reach the port, that changes, so turn on the gate:

```bash
python scripts/maestro_access.py enable
```

It prints a generated password **once**. Store it somewhere safe (a password manager).
Details:

| Action | Effect |
| --- | --- |
| `enable` | writes a PBKDF2 hash to `app/settings/access_gate.json` (**never** the password) |
| `reset` | new password; every existing session on every device stops working |
| `disable` | opens the app again — only when bound to loopback |
| `status` | shows on/off and the mode, without printing any secret |

Behaviour once enabled: every request needs a signed, HttpOnly session cookie
(30 days, `SameSite=Lax`; `Secure` automatically when the site is served over HTTPS).
Anonymous visitors to `/api/...` get `401`; anonymous navigations get a sign-in page.
Manifest, icons, service worker and the offline page stay public so the app can still
be installed and can explain itself when the computer is unreachable. Five wrong
passwords from one address trigger a short lockout. Changing the password ends all
sessions. Sign out on a device by opening `/api/v1/access/logout`.

Set `MAESTRO_ACCESS_TOKEN` instead if you want a fixed secret with nothing written to
disk (automation). Environment variables win over the file.

## 3. Give the phone a private HTTPS address

> On an **iPhone or iPad** instead of Android, the client setup differs (Safari's Add to
> Home Screen, the 180x180 Home Screen icon, and iOS's 16.4+ rule for Web Push). Follow
> [Maestro on your iPhone or iPad](DEPLOY-IOS.md); the password gate and the Tailscale
> address below are the same.

Tailscale is the cheapest way to do it, keeps the port closed to the public internet,
and issues a trusted certificate (which Android requires for install and for
notifications). Maestro never uses Tailscale Funnel — nothing becomes public.

1. Install Tailscale on the computer and on the phone, signed into **your own** account
   (free Personal plan).
2. Start Maestro. In Pinokio choose **Secure Remote Access (Tailscale)**; on a
   non-elevated setup use **Settings → Notifications → Private HTTPS access** instead.
3. Maestro prints the address: `https://<machine>.<your-tailnet>.ts.net`. That is your
   URL from now on, on every device you are signed into.

Full detail and troubleshooting: [`TAILSCALE_REMOTE_ACCESS.md`](TAILSCALE_REMOTE_ACCESS.md).
`python scripts/maestro_doctor.py` prints the same address under "Reach it from your
phone", and tells you if the route is missing.

*Alternative without Tailscale:* set `PINOKIO_SHARE_LOCAL=true` and use
`http://<computer-lan-ip>:7860`. It only works on the same Wi-Fi, it is plain HTTP
(so Android will not offer installation, and notifications need HTTPS), and the access
gate becomes the only thing protecting the port. Prefer Tailscale.

## 4. Install it on Android

1. Open the private `https://…ts.net` address in **Chrome** on the phone.
2. Sign in with the access password. (Do this first: the install prompt needs the app
   to load, and an unauthenticated visit shows the sign-in page.)
3. Chrome menu (**⋮**) → **Install app** / **Add to Home screen**.
4. Launch it from the Home Screen. It opens **standalone** (no browser chrome), with
   Maestro's name and icon, and keeps the session, so you are not asked again.

What the install uses: `ui/public/manifest.webmanifest` (name "Maestro AI Studio",
short name "Maestro", standalone, dark theme), a 192 and a 512 icon plus a maskable
512 (generated from the canonical artwork by `scripts/generate_pwa_icons.py`), and
`maestro-sw.js`, whose fetch handler serves `/offline.html` when the computer cannot
be reached. Sessions and reconnections are handled in the app: an expired session
returns you to the sign-in page once, and regaining the connection refreshes the queue
and gallery automatically.

Optional: enable **System notifications** in Maestro's Notifications settings to get
Web Push when a render finishes while the app is closed.

## 5. Connect the engine (the part that needs your GPU)

The studio can talk to an engine it has proved works. On the computer:

```bash
python scripts/maestro_doctor.py     # GPU, CUDA, disk, ffmpeg, ComfyUI + weights
```

In the app (phone or desktop) → **Studio → Compute & transfer → Local engine**:

1. **Re-detect** — measured CPU/RAM/disk/GPU and the model that fits.
2. **Install on this computer** — shows the exact commands first; tick the box to fetch
   the ~18 GB of Wan 2.2 weights; nothing runs without confirmation.
3. Start ComfyUI with the command the panel prints (`--listen 127.0.0.1`).
4. **Run the test** — generates a tiny real clip, fetches it back, verifies the
   container, and plays it in the panel. That is the proof.
5. **Register engine** — only possible with a passed test on record.
6. **Add to the queue** — type a prompt, choose the seconds, and the job appears in the
   job list. It runs on the GPU and the finished clip comes back to the gallery.

**Your computer must be on to generate video.** The phone client keeps working without
it — browsing projects, editing the storyboard, importing media, queueing shots — and a
job whose engine is offline is **deferred, not failed**: it returns to `pending`,
consumes no retry and records no error, then runs when ComfyUI answers again. Nothing
needs to be re-submitted.

## 6. What is deployed, what is verified, what is missing

**Implemented and tested in this repository** (see the end of this document for how to
re-run everything): the production UI bundle served by the backend, persistence under
`outputs/`, the job queue with recovery, the sign-in gate (21 HTTP tests), the PWA
manifest/icons/service worker, the offline page, the doctor, the local-engine install
plan and functional test, and the private-URL path.

**Cannot be verified from the development container, and says so:** the generation
engine needs an NVIDIA GPU, CUDA, `ffmpeg` and the model weights. In a container
without them, no clip is generated, so treat any claim about generation speed or model
quality as untested here. `scripts/maestro_doctor.py` reports exactly which of those is
missing on the machine where you run it.

**No public URL is published by this repository.** Maestro is designed to be reached on
your own private network; exposing a full media workspace to the internet would need
real authentication, TLS at the edge and abuse protection that this local-first design
deliberately does not attempt.

## 7. Maintenance

| Task | How |
| --- | --- |
| Start / stop | Pinokio **Start** / **Stop**, or `python app/launch.py` and Ctrl-C |
| Update | Pinokio **Update** (pulls, rebuilds the UI when needed); manual: `git pull` then `cd ui && npm install && npm run build` |
| Is it healthy? | `python scripts/maestro_doctor.py` |
| Logs (Pinokio) | `logs/api/start.js/latest`, `logs/api/update.js/latest` |
| Change the password | `python scripts/maestro_access.py reset` |
| Turn the gate off | `python scripts/maestro_access.py disable` (only while on loopback) |
| Rotate the phone URL | `tailscale serve --https=443 off`, then run Secure Remote Access again |
| Backups | copying `outputs/` (projects, media, renders) and `app/settings/` (preferences, gate, push keys) is a complete backup |
| Disk hygiene | renders and imports live in `outputs/` and `uploads/`; the doctor reports free space |
| Start over | Pinokio **Reset** recreates the Python environment; projects under `outputs/` are kept |

## 8. If something does not work

| Symptom | Check |
| --- | --- |
| Phone shows "Maestro can't reach your computer" | computer on? Maestro started? Tailscale connected on both devices? |
| Address opens but asks for a password you lost | `python scripts/maestro_access.py reset` |
| "Install app" never appears in Chrome | address must be `https://` (Tailscale), manifest reachable, and the page must have loaded after signing in |
| Engine panel says the workflow needs a newer ComfyUI | update ComfyUI; the panel lists the exact missing node classes |
| Jobs sit at `pending` | engine offline: they are deferred by design; start ComfyUI and they run |
| Nothing generates and the doctor says "no NVIDIA GPU" | generation is not possible on that machine |

## Official references

- [Tailscale](https://tailscale.com/download) — private network, free for personal use
- [ComfyUI Wan 2.2 guide](https://docs.comfy.org/tutorials/video/wan/wan2_2) — the engine
- [Chrome install criteria](https://web.dev/articles/install-criteria) — why the manifest and worker are shaped this way
