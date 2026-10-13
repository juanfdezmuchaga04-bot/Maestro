# Auditoría Fase 1 — Adaptación de Maestro para ejecución remota de IA

**Estado:** solo auditoría. En esta fase **no se ha modificado código, ni se han instalado
dependencias, ni se han cambiado configuraciones**. Este documento es el único artefacto
creado.

**Repositorio auditado:** `juanfdezmuchaga04-bot/Maestro` (fork de `Blizaine/Maestro`),
`VERSION` = **2.6.0**, commit auditado `31d7396`.

**Hardware objetivo declarado:** equipos modestos, incluido AMD A9-9425 + Radeon R5 +
8 GB de RAM (Lenovo de referencia). Requisito: la IA pesada debe ejecutarse en servicios
remotos, conservando código, funcionalidades, interfaces y flujos existentes.

---

## 0. Método y evidencia

La auditoría es de lectura: inspección de ficheros, `grep`/recuentos y **una** ejecución de
pruebas ya existentes (no muta nada).

| Comprobación | Comando | Resultado |
| --- | --- | --- |
| Suite de la cola de producción | `PYTHONPATH=app python3 -m unittest tests.test_production_queue_api` | **31 tests OK** (1.46 s) |
| Capas reutilizables (gate, capacidades, nodos, worker) | `PYTHONPATH=app python3 -m unittest tests.test_access_gate tests.test_engine_providers tests.test_compute_nodes tests.test_queue_worker` | **91 tests OK** (3.46 s) |
| Recuento de código | `wc -l`, `find` | `app/launch.py` 31 665 líneas · `app/wgp.py` 15 717 · `app/services/*.py` 124 módulos · `ui/src` 207 ficheros `.ts/.tsx` · `tests/test_*.py` 289 |
| Endpoints backend | `grep -cE "@api\.(get\|post\|put\|delete\|patch)" app/launch.py` | **251** |
| Torch del contenedor | `python3 -c "import torch…"` | `2.7.1+cpu`, `cuda_available=False` |
| Arranque del backend completo | `PYTHONPATH=app python3 -c "import wgp"` | **falla**: `ModuleNotFoundError: No module named 'git'` → el backend no arranca en este contenedor porque el runtime fijado **no está instalado** (solo están las dependencias que usan los tests) |

**Lo que esta auditoría NO puede verificar** (y por tanto no se afirma): generación real de
vídeo/imagen/audio, latencia, consumo de VRAM/RAM en carga, comportamiento en Windows con
el launcher, rutas AMD/ROCm, ComfyUI remoto real, ni el comportamiento con Tailscale.
No hay GPU en este entorno.

---

## 1. Arquitectura actual

Maestro **no es una web con backend en la nube**. Es una aplicación *local-first* de un
solo usuario: interfaz, API, motor de generación y datos viven en la misma máquina.

```
   ┌──────────────────────── Tu máquina (Windows/Linux + NVIDIA) ────────────────────────┐
   │                                                                                     │
   │  Navegador (o PWA Android)                                                          │
   │      │  HTTP/JSON + SSE  (cookie de sesión opcional)                                │
   │      ▼                                                                              │
   │  FastAPI + Uvicorn  ── app/launch.py  (251 endpoints @api.*)                        │
   │      ├── sirve el bundle React ya construido  (StaticFiles mount "/")              │
   │      ├── routers extra: /api/v1/production/queue, /api/v1/film/*, downloads,        │
   │      │   character-transfer, music-training                                        │
   │      ├── access_gate.py      (login opcional, cookie firmada)                       │
   │      ├── remote_access.py    (Tailscale Serve → HTTPS privado)                      │
   │      ├── web_push.py         (notificaciones Web Push)                              │
   │      └── engine_providers.py (registro de CAPACIDADES) ─┐                           │
   │                                                          │                          │
   │      ┌───────────────────────────────────────────────────┴──────────────┐           │
   │      │ Motor local:  app/wgp.py  (port de Wan2GP, EN PROCESO)           │           │
   │      │   torch CUDA 13 · MMGP 3.8.2 (offload) · SageAttention/          │           │
   │      │   FlashAttention · Triton · nunchaku · lightx2v · comfy-kitchen  │           │
   │      └──────────────────────────────────────────────────────────────────┘           │
   │      ┌──────────────────────────────┐   ┌────────────────────────────────┐          │
   │      │ LLM: llm_service.py          │   │ Motores externos opcionales    │          │
   │      │  local = llama-server (GGUF) │   │  · ComfyUI (comfyui_adapter)   │          │
   │      │  remote/openai/anthropic     │   │  · compute_nodes (otro equipo) │          │
   │      └──────────────────────────────┘   └────────────────────────────────┘          │
   │                                                                                     │
   │  Cola persistente: generation_queue.py + queue_worker.py  (fichero JSON)             │
   │                                                                                     │
   │  Datos: SIN SGBD. `outputs/<workspace>/…`, `uploads/`, `app/settings/*.json`         │
   └─────────────────────────────────────────────────────────────────────────────────────┘
```

### 1.1 Frontend (cliente web)

- **Stack:** React 19.2 + TypeScript 5.9 + Vite 7 + Zustand 5 + Tailwind 4
  (`@tailwindcss/vite`), `lucide-react`, `dompurify`, `qrcode.react`. 207 ficheros
  `.ts/.tsx`. Sin Redux, sin Next.js.
- **Servido por el backend:** el build de `ui/dist` se monta con
  `api.mount("/", StaticFiles(directory=_ui_dist, html=True))` (`app/launch.py:31547`).
  En desarrollo, Vite usa el puerto 3000 y hace proxy de `/api` → `127.0.0.1:7860`
  (`ui/vite.config.ts`).
- **PWA instalable:** `ui/public/manifest.webmanifest`, `maestro-sw.js` (sirve
  `/offline.html` cuando el host no responde; **no** cachea el bundle), iconos 192/512/
  maskable generados por `scripts/generate_pwa_icons.py`.
- **Peso de cómputo en el cliente: prácticamente nulo.** No hay `wasm`, WebGPU, WebCodecs,
  `ffmpeg.wasm` ni bucles pesados de canvas: todo el procesado (FFmpeg, análisis, miniaturas)
  ocurre en el backend. `ffmpeg` aparece en el cliente solo como etiqueta de estado del motor
  (`ui/src/studio/StudioWorkspace.tsx:384`, `StudioFinishingPanel.tsx:255`).
  **Conclusión: el navegador del equipo modesto ya es, hoy, un cliente fino.**

### 1.2 Backend

- **FastAPI + Uvicorn**, un único proceso (`api = FastAPI(...)` en `app/launch.py:286`;
  `uvicorn.run(api, host=host, port=port)` en `:31654`; puerto por defecto 7860 vía
  `SERVER_PORT`).
- **251 endpoints** `@api.*` (92 GET, 125 POST, 15 PUT, 18 DELETE, 1 PATCH), la mayoría
  *thin wrappers*, más routers dedicados: `production_queue_api` (`/api/v1/production/queue`),
  `film_api` (`/api/v1/film/*`), descargas activas, transferencia de personajes y
  entrenamiento de música.
- **CORS** (`CORSMiddleware`, `app/launch.py:338`) y **middleware del gate de acceso
  registrado antes que CORS** (`:325`).
- **Superficie de API documentada:** `app/docs/API.md`, `docs/STUDIO-NODES.md`,
  `docs/STUDIO-QUEUE.md`, `docs/FILM-STUDIO.md`.

### 1.3 Motor de generación

- El motor **no es un servicio independiente**: `app/wgp.py` (port de Wan2GP, 15 717 líneas)
  se importa y ejecuta **dentro del mismo proceso** que la API.
- **Offload de memoria:** MMGP 3.8.2 (`mmgp==3.8.2`, `app/requirements.txt`) decide qué pesas
  viven en VRAM y cuáles en RAM/disco. `perf_recommend.py` elige perfil, cuantización
  (INT8/FP8/NVFP4) y config de VAE a partir de *tiers* de VRAM y RAM
  (`_pick_profile`, `_pick_quantization`, `_pick_vae_config`).
- **Kernels nativos propietarios de NVIDIA**, instalados por `torch.js` (no por `pip`):
  `torch==2.10.0+cu130` (o `2.7.1+cu128` en el runtime legacy), `xformers`, `triton`,
  **SageAttention**, **FlashAttention 2**, `lightx2v_kernel`, `nunchaku`,
  `comfy-kitchen`, `onnxruntime-gpu`, `rembg[gpu]`, `flash-linear-attention`.
- **Runtime por hardware** (`launcher_profile.js`): `app/env-sol/` (RTX 40 + CUDA 13),
  `app/env-rtx50/` (RTX 50), `app/env/` (resto de NVIDIA, CUDA 12.8 legacy).

### 1.4 Capa de IA desacoplada (ya existente, parcial)

Esto es el activo más importante para la misión: **la indirección ya está construida**.

- `app/services/engine_providers.py` — registro de **capacidades** (`CAPABILITIES`). El
  frontend nunca habla con un modelo ni con un proveedor: pide una capacidad y el registro
  la resuelve a un proveedor que **hoy sí puede** cumplirla. Reglas explícitas en el propio
  módulo: nada se infiere del nombre; **las APIs comerciales son opcionales y nunca se usan
  implícitamente**; sin *fallback* silencioso (si no hay proveedor, se lanza un error que
  dice qué sí puede hacerlo); el registro se construye desde estado medido.
- `app/services/compute_nodes.py` — **nodos de cómputo remotos ya soportados**: registro con
  token generado una vez (**guardado solo como hash SHA-256**), heartbeat, asignación por
  capacidad declarada, y **trabajos aparcados y devueltos** cuando el nodo vuelve.
- `app/services/comfyui_adapter.py` + `comfy_workflows/` — motor ComfyUI externo, con
  manifiestos por workflow, plan de instalación y **prueba funcional obligatoria antes de
  registrar** el motor (`docs/COMFYUI-LOCAL.md`).
- `app/services/llm_service.py` — **adaptador LLM con 4 proveedores ya implementados**:
  `local` (subproceso `llama-server` de llama.cpp con GGUF), `remote` (endpoint
  compatible OpenAI), `openai` y `anthropic`. Ver §3.

---

## 2. Diagrama de dependencias

```
launcher (Pinokio)                                  navegador / PWA
  install.js  ── aborta si gpu !== 'nvidia'            │
  torch.js    ── wheels CUDA + kernels NVIDIA          │
  launcher_profile.js ── elige env-sol / env-rtx50 / env
        │                                              │
        ▼                                              ▼
  app/launch.py (FastAPI, 251 endpoints) ──── sirve ui/dist (React)
        │
        ├─► app/services/* (124 módulos)
        │     ├─ engine_providers ──► { motor local wgp.py | ComfyUI | compute_node }
        │     ├─ generation_queue ──► queue_worker ──► handler por motor
        │     ├─ llm_service ──► { llama.cpp local | OpenAI-compatible | OpenAI | Anthropic }
        │     ├─ access_gate · remote_access (Tailscale) · web_push
        │     ├─ local_hardware (nvidia-smi) · hardware_detect (torch.cuda) · perf_recommend
        │     └─ film_* · editor_projects · gallery_* · uploads_* · safedownload
        │
        ├─► app/wgp.py ──► torch(CUDA) · diffusers · transformers · MMGP
        │                  · kernels NVIDIA (sage/flash/triton/nunchaku/lightx2v)
        │
        └─► disco: outputs/ · uploads/ · app/settings/*.json   (sin SGBD)

Dependencias externas de red (todo opcional salvo la descarga de pesos):
  huggingface.co (pesos GGUF/checkpoints) · civitai.com (LoRA) · api.anthropic.com ·
  api.openai.com · lmstudio.ai (LM Studio local/remoto) · tailscale.com · Web Push del navegador
```

**Nada de la aplicación web depende de la GPU para existir**: `local_hardware.py` y
`hardware_detect.py` degradan con gracia (`cuda_available=False`, `vram_tier="none"`), y el
motor ComfyUI se degrada a *deferido* cuando no responde. El bloqueo está en el **motor
local** (`wgp.py`) y en el **launcher** (`install.js`), no en la API.

---

## 3. Inventario de proveedores y modelos

### 3.1 Proveedores de LLM (texto/razonamiento — el "Director" y los escritores H3)

Ajustes en `app/settings` y UI `SettingsDrawer/ServicesSettingsPanel.tsx`
(`llm_provider`, `llm_remote_url`, `llm_remote_api_key`, `openai_api_key`, `anthropic_api_key`):

| Proveedor | Implementación | Notas |
| --- | --- | --- |
| `local` (por defecto) | subproceso `llama-server` (llama.cpp) + GGUF | Gemma 4 E4B por defecto (`Abhiray/gemma-4-E4B-it-heretic-GGUF`); también Qwen3.5 2B/4B/9B, Qwen3.8-27B, Qwen3.6-27B, Gemma 4 E2B… (`llm_service.py`) |
| `remote` | endpoint compatible OpenAI vía `llm_remote_url` + clave | pensado para LM Studio / servidores propios (`http://192.168.1.100:1234`) |
| `openai` | `api.openai.com` + `openai_api_key` | |
| `anthropic` | `https://api.anthropic.com/v1/messages` (`llm_service.py:2946`, `:3000`) | modelos listados: Claude Sonnet 4.6 / Haiku 4.5 |

Además `lmstudio_management.py` gestiona **carga/descarga explícita de instancias** en un
servidor LM Studio (API nativa `/api/v1/...`), con rechazo de credenciales embebidas y
comprobación de ocupación.

### 3.2 Proveedores de generación (imagen / vídeo / audio)

| Proveedor | Tipo | Estado |
| --- | --- | --- |
| Motor local `wgp.py` (WanGP) | en proceso, GPU NVIDIA | **por defecto**; requiere CUDA |
| ComfyUI externo | servicio HTTP | adaptador + prueba funcional (`comfyui_adapter.py`) |
| Nodo de cómputo | otro equipo con token/ heartbeat | registro y asignación (`compute_nodes.py`) |
| APIs comerciales | — | **opcionales por diseño**; nunca implícitas (`engine_providers.py`) |

### 3.3 Modelos (catálogo local, no nube)

Vídeo: **Wan 2.1/2.2** (TI2V-5B por defecto), **MiniMax H3** (vídeo+audio, con Sol Engine
opcional, GGUF/INT8/FP8/LoRA), **LTX-2 / LTX-2.3**, **Viggle Turbo**.
Imagen: **Flux**, **Qwen Image 2.1**, **Krea 2**, **Z-Image**, **HiDream O1**.
Audio/voz/música: **ACE-Step 1.5**, **YuE2**, **IndexTTS2**, **MMAudio**, **Whisper**
(`faster-whisper`, `openai-whisper`), **Scenema**, **seed-vc** (GPL-3.0, clonado aparte).
Visión: **SAM 3.1** (Inpaint, no se instala por defecto).
LLM: GGUF vía llama.cpp (§3.1).

**Coste y tamaño:** instalación 10–20 min; primera generación por modelo descarga pesos
(~18 GB el vídeo por defecto); instalación típica 50–100 GB; colección completa >300 GB
(`README.md`).

---

## 4. Persistencia, colas, autenticación y red

### 4.1 Persistencia — sin SGBD cliente-servidor

No hay Postgres, Redis ni ORM. Hay **un único SQLite embebido y local**, usado solo como
caché de historial de estimaciones de tiempo (`app/services/generation_eta.py:16,27`:
`generation_eta_history.sqlite3`), no como almacén de la aplicación. Todo el estado real son
**ficheros JSON con escritura atómica** (`json.dump` a temporal + `os.replace`,
p. ej. `generation_queue.py:206-218`):

| Datos | Ubicación |
| --- | --- |
| Proyectos, medios, renders | `outputs/<workspace>/…` (`save_path`) |
| Importados por el usuario | `uploads/` |
| Ajustes, gate, push, nodos, cola | `app/settings/*.json` (gitignored; `access_gate.json`, `web_push.json`, `compute_nodes/`, `generation_queue/queue.json`, `studio_queue/`) |
| Producciones de Films / montaje | `outputs/<ws>/.maestro_film/<id>.json`, `.maestro_editor/<id>.json` |
| Cachés | miniaturas, pósters, receipts de assets gestionados, historial SQLite de ETA |

### 4.2 Cola y trabajos asíncronos

- `app/services/generation_queue.py` (~1 034 líneas) es **el registro durable**: 8 estados
  (`draft → pending → preparing → running → finalizing → completed/failed/cancelled`),
  concurrencia configurable, **techo de memoria declarado**, prioridades, **reintentos
  acotados**, protección contra duplicados, distinción entre cancelación *solicitada* y
  *realmente detenida*, y **recuperación de trabajos interrumpidos al arrancar**.
- `app/services/queue_worker.py` es **el ejecutor**: reclama el siguiente trabajo viable y lo
  entrega al *handler* registrado para el motor de ese trabajo. Reglas explícitas: **sin
  handler no hay simulación** (`complete()` rechaza salidas que no existen en disco);
  **el progreso se reenvía, no se inventa** (`percent=None` si el motor no informa);
  **la cancelación se acusa de recibo, no se asume**. El registro de handlers está **vacío
  por defecto**.
- Estado verificado hoy: **31/31 tests HTTP de la cola pasan** (submit, claim, progreso,
  cancelación, completado, recuperación, y rechazos: trabajo desconocido, ruta de salida
  ilegal, ajuste desconocido, runner ausente).
- Comportamiento clave ya implementado para la misión: un trabajo cuyo motor está **offline
  no falla**, vuelve a `pending` sin consumir reintento ni registrar error, y se ejecuta
  cuando el motor responde (`docs/FILM-STUDIO.md`).

### 4.3 Autenticación y autorización

- `app/services/access_gate.py` + `scripts/maestro_access.py`: **opcional y apagado por
  defecto** (en loopback el navegador y el servidor son el mismo usuario).
- Al activarlo: hash **PBKDF2** en `app/settings/access_gate.json` (nunca la contraseña),
  cookie de sesión firmada **HttpOnly**, 30 días, `SameSite=Lax`, `Secure` automático sobre
  HTTPS; `401` para `/api/...` anónimo; página de acceso para navegación anónima; bloqueo
  temporal tras varios fallos; cambiar la contraseña cierra todas las sesiones. Alternativa
  por entorno: `MAESTRO_ACCESS_TOKEN`.
- **No hay multiusuario, ni roles, ni permisos por recurso, ni cuotas por usuario, ni
  aislamiento de datos entre personas.** Es un producto de *un solo usuario*.

### 4.4 Red y acceso remoto

- `remote_access.py`: **Tailscale Serve** (HTTPS privado, certificado de confianza); el
  módulo **no usa Tailscale Funnel** y rechaza sobrescribir una ruta Serve existente.
  El plan Personal de Tailscale es gratuito.
- Alternativa LAN: `PINOKIO_SHARE_LOCAL=true` → `http://<ip-lan>:7860` (HTTP plano, sin
  instalación PWA ni notificaciones).
- `web_push.py`: Web Push (VAPID) con claves y suscripciones **solo locales**
  (`app/settings/web_push.json`).
- `reference_urls.py`: importación por URL **con guarda anti-SSRF**.
- `upload_chunks.py`: subida reanudable por trozos.
- **No existe ninguna URL pública publicada por el repositorio**, y el diseño lo declara
  deliberadamente (exponer un espacio de trabajo multimedia completo a Internet exigiría
  autenticación real, TLS en el borde y protección anti-abuso que este diseño no aborda).

---

## 5. Funcionalidades que ya pueden reutilizarse sin reescribir

| Pieza existente | Por qué sirve para la misión |
| --- | --- |
| `engine_providers.py` (registro de capacidades) | Es exactamente la "capa de adaptadores" que pide la Fase 3: el frontend pide capacidad, no proveedor. Añadir un proveedor *cloud* es registrar una entrada, no tocar la UI. |
| `compute_nodes.py` (nodos con token + heartbeat) | Modelo ya listo para "un equipo remoto con GPU ejecuta los trabajos". Token solo como hash, latido, aparcado y devolución de trabajos. |
| `generation_queue.py` + `queue_worker.py` | La Fase 4 (cola durable, estados, reintentos, idempotencia por `job_id`, cancelación, recuperación, persistencia de resultados) **ya está implementada y probada (31 + 91 tests, incluidos worker y nodos)**. Falta el handler del motor remoto. |
| `comfyui_adapter.py` + `comfy_workflows/` | Un motor de generación externo por HTTP ya integrado, con verificación obligatoria antes de aceptar trabajo. Es la plantilla para cualquier motor remoto (incluido un servidor ComfyUI en la nube). |
| `llm_service.py` (4 proveedores) | El desacoplamiento de modelos de lenguaje ya existe; el trabajo de Fase 3 aquí es homogeneizar errores/timeout/costes/telemetría y validación, no sustituir el framework. |
| `access_gate.py` | Base de autenticación ya implementada y cubierta por `tests/test_access_gate.py` (verificado, incluido en los 91 tests OK). |
| `remote_access.py` + PWA + Web Push | Acceso privado HTTPS y notificaciones ya resueltos sin abrir puertos. |
| Cola/estados expuestos en la UI (`StudioComputePanel`, `StudioWorkspace`, `GlobalQueuePopover`) | El usuario ya ve y controla los trabajos desde el móvil. |
| `hardware_detect.py`, `local_hardware.py`, `perf_recommend.py`, `PreflightBanner` | La app **ya sabe medir** la máquina y **ya dice** qué falta (GPU, CUDA, ffmpeg, disco). Es la base para decidir en caliente "local vs remoto". |
| `scripts/maestro_doctor.py` | Preflight de producción de solo lectura, con el paso siguiente para cada `[fail]`. |
| Films/Editor (`film_*`, `editor_projects.py`) | Flujos completos de producción que no dependen del motor: brief, storyboard, mezcla, montaje, export. |

---

## 6. Bloqueos reales para el hardware objetivo (A9-9425 + Radeon R5 + 8 GB)

Los bloqueos **no están en la web ni en la API**: están en el motor y en el launcher.

| # | Bloqueo | Evidencia | Impacto |
| --- | --- | --- | --- |
| B1 | ~~El launcher aborta sin NVIDIA~~ **Resuelto** | `install.js` ya no detiene la instalación (sólo avisa: Pinokio mantiene su variable documentada `gpu`); `launcher_profile.js` añade el runtime CPU/ligero `app/env-light` y un GPU vacío deja de leerse como «sin NVIDIA» | El repositorio ya es instalable y arrancable por la vía soportada en el equipo objetivo. La **generación local** sigue requiriendo NVIDIA (B2/B4): ver `docs/INSTALAR-SIN-NVIDIA.md` |
| B2 | **AMD y macOS no soportados** | `README.md:979` («AMD GPUs and macOS are not currently supported… depends on CUDA and several NVIDIA-only kernels») | A9-9425/Radeon R5 fuera de alcance por hardware |
| B3 | **Radeon R5 no está en ninguna matriz soportada** | `app/docs/AMD-INSTALLATION.md` (documentación *upstream* de Wan2GP) solo lista RDNA2+ (gfx1030+), RDNA3/4 y APUs Strix (gfx1150/1151). Además **ese camino AMD no está cableado en el launcher de Maestro** | No hay ruta ROCm/TheRock para esa GPU |
| B4 | **Kernels nativos CUDA-only** | `torch.js`: `torch+cu130`, `xformers`, `triton`, SageAttention, FlashAttention, `lightx2v_kernel`, `nunchaku`; `comfy-kitchen` en `requirements.txt` | Sin CUDA, el motor no tiene backend de atención optimizado; `wgp.py` degrada con avisos («FlashAttention is unavailable…») pero **no genera** |
| B5 | **Requisitos de máquina declarados por encima del objetivo** | `README.md:961-975`: **mínimo** NVIDIA 6 GB VRAM, **16 GB RAM**, 150 GB libres; recomendado 24 GB/32 GB/500 GB | 8 GB de RAM queda **por debajo del mínimo** para ejecutar el motor |
| B6 | **Presupuestos de memoria dimensionados para NVIDIA** | `perf_recommend._pick_profile(ram_tier, vram_tier)`, `hardware_detect.vram_tier` (`high ≥24 GB / low 12-23 / tight <12 / none`), MMGP offload | Con `vram_tier="none"` no hay perfil útil |
| B7 | **Peso del propio backend en RAM (no medido aquí)** | `expectativas de import`: `torch` solo ya ~200 MB RSS; `transformers` ~220 MB; `diffusers` ~334 MB; y `launch.py` importa `wgp.py` en el mismo proceso | Riesgo real en 8 GB si se intenta ejecutar el backend completo localmente: **hay que medirlo** (ver §9) |
| B8 | **El runtime fijado no arranca sin las dependencias completas** | En este contenedor `import wgp` falla por `git` ausente (no hay `app/env*` instalado) | Cualquier despliegue "minimalista" exige resolver el árbol de dependencias completo (torch, diffusers, transformers, librosa, moviepy, av, opencv, spaCy…) |

**Contrapeso importante:** el **cliente** no está bloqueado. El frontend es fino (§1.1), la
API y la cola funcionan sin GPU, y la app **ya sabe operar sin motor** (Films/Editor/Biblioteca
siguen funcionando; los trabajos se difieren). Es decir: el bloqueo es *dónde se genera*, no
*qué se puede usar*.

---

## 7. Riesgos de seguridad, límites de ejecución y dependencias de infraestructura

### 7.1 Riesgos

| Riesgo | Estado hoy | Qué falta para un despliegue remoto |
| --- | --- | --- |
| **Sin autenticación por defecto** | Gate apagado en loopback; se activa a mano | Activar por defecto (`access_gate`) cuando el puerto salga de loopback; documentarlo como obligatorio |
| **Un solo usuario** | Sin cuentas, roles ni permisos por recurso | Multiusuario real, aislamiento de datos, permisos por endpoint y por herramienta |
| **Sin cuotas ni límites por usuario** | No hay rate limiting por identidad (solo bloqueo del gate por dirección) | Cuotas por usuario/por capacidad y límites de consumo/coste (Fase 3/5) |
| **Claves de proveedor en el servidor** | En `app/settings` (local, gitignored) y enmascaradas en la UI (`*_api_key_set`), nunca enviadas al cliente | Confirmar cifrado en reposo y rotación; auditoría de uso |
| **Contenido de usuario** | Todo local; el tráfico de red son descargas opcionales (HF/CivitAI) y notificaciones | Si se aloja centralmente: cifrado, retención, borrado, cumplimiento |
| **Ejecución arbitraria** | Sin sandbox de código más allá de `plugins.json`/plugins locales; los "handlers" de la cola se registran **en proceso** | Aislar la ejecución de herramientas autorizadas (Fase 2) |
| **Errores y trazas** | El proyecto cuida el mensaje ("no revela secretos" en el gate; `access_log_filter.py` filtra el log de acceso) | Auditar que ningún error de proveedor externo devuelva cuerpos crudos |
| **Aprobación humana** | No existe como concepto | Exigirla para operaciones sensibles (Fase 5) |
| **Exposición a Internet** | Tailscale privado; sin Funnel; sin URL pública | Si hace falta acceso desde cualquier red: TLS en el borde + auth real + anti-abuso |

### 7.2 Límites de ejecución observados

- **Un proceso único** para API + motor: una generación puede bloquear/competir con la API
  según concurrencia de la cola y techo de memoria declarado.
- **Estado en ficheros**: coherente y atómico, pero no apto para múltiples réplicas del
  backend sobre el mismo directorio sin coordinación (la cola no usa bloqueo distribuido ni
  base de datos compartida; el único SQLite es una caché local de ETA).
- **Persistencia de trabajos largos:** la cola sobrevive a cierres y reinicios, pero la
  *reanudación* de una generación ya iniciada depende de que el motor pueda reanudar; el
  código no lo presupone (regla explícita en `docs/FILM-STUDIO.md` y `queue_worker.py`).

### 7.3 Dependencias de infraestructura (todas hoy opcionales)

Pinokio (launcher) · NVIDIA CUDA/driver ≥580 (runtime CUDA 13) · FFmpeg/FFprobe ·
Hugging Face (pesos) · CivitAI (LoRA) · Tailscale (acceso privado) · Web Push del navegador ·
LM Studio (si se elige `remote`) · ComfyUI (si se elige motor externo).

---

## 8. Plan de migración incremental (propuesta — **requiere aprobación**)

Principio rector: **no reescribir** frontend, API, cola, agentes ni persistencia. Se añade un
**proveedor remoto** detrás de las costuras que ya existen.

**Decisión pendiente (Fase 2).** Hay dos arquitecturas posibles y no son equivalentes:

- **Opción A — backend en la máquina con GPU, equipo modesto solo navegador.** El navegador
  del Lenovo abre la app por HTTPS privado en el equipo/servidor con GPU. *Máxima
  reutilización, mínimo cambio de código*; exige hosting protegido (auth real, TLS,
  multiusuario) porque el backend deja de ser "mi propio PC".
- **Opción B — backend ligero en el equipo modesto + generación en nodo(s) remotos.**
  Reutiliza `engine_providers` + `compute_nodes` + `comfyui_adapter`: el equipo modesto
  dirige, el nodo con GPU ejecuta. *Más fiel a la arquitectura actual*; exige medir si el
  stack Python completo cabe y es usable en 8 GB de RAM (§6, B7), y resolver el
  descubrimiento/registro de nodos fuera de la LAN.

Recomendación preliminar: **B como objetivo de producto, A como atajo inmediato y banco de
pruebas** (A permite validar el flujo ya, sin tocar el motor).

Pasos propuestos, cada uno pequeño, verificable y reversible:

1. **(F2) Inventario de contrato**: fijar por escrito la interfaz `EngineProvider`
   (capacidades → proveedor) y qué capacidades quedan remotas y cuáles locales.
2. **(F2) Proveedor remoto de generación**: registro de un proveedor que delega en un
   endpoint existente (`/api/v1/production/queue`) con token, reutilizando `compute_nodes`.
   *Regresión:* los 31 tests de la cola + tests de `engine_providers`/`compute_nodes`.
3. **(F3) Adaptador de IA uniforme**: extender a *todas* las capacidades las garantías que
   hoy solo tiene el LLM en parte — timeouts, errores homogéneos, validación de entrada/
   salida, límites de consumo/coste, telemetría sin secretos (mantener el patrón
   `*_api_key_set` de la UI).
4. **(F4) Handler remoto en el worker** + reanudación/idempotencia explícita por `job_id`;
   el motor remoto **no** debe asumir reanudación automática (regla ya establecida).
5. **(F5) Endurecer**: gate obligatorio fuera de loopback, permisos por endpoint y por
   herramienta, cuotas, aprobación humana para operaciones sensibles, y revisión de errores.
6. **(F6) Migración por lotes**: sin tocar React/Zustand, sin tocar ficheros JSON, sin
   cambiar a los agentes; cada lote con su prueba de regresión y su reversión.
7. **(F7) Validación en el Lenovo (8 GB)**: medir RAM/latencia/coste reales, confirmar
   "cero requisitos NVIDIA en el cliente", persistencia de trabajos largos, recuperación
   ante cortes de red, seguridad de credenciales y regresión funcional. Documentar despliegue,
   variables de entorno y recuperación.

---

## 9. Incertidumbres y lo que NO está verificado

Se declara explícitamente para no presentar suposiciones como hechos:

1. **Sin GPU en este entorno**: no se ha medido generación, latencia, VRAM ni consumo. Las
   cifras del `README.md` (p. ej. 23m43s → 12m59s con optimizaciones) son datos del proyecto,
   no mediciones de esta auditoría.
2. **Backend completo no arrancado**: `import wgp` falla por dependencias no instaladas
   (fase 1 prohíbe instalarlas). Por tanto **no se ha verificado** el arranque end-to-end,
   ni la UI servida, ni el gate en ejecución en este entorno.
3. **Peso en RAM del backend en 8 GB** (B7): estimación por importaciones parciales, **no una
   medición de la app en marcha**. Es la primera comprobación a hacer en Fase 2/7.
4. **Ruta AMD/ROCm**: no verificada y no cableada en el launcher; el documento AMD del repo
   es documentación upstream, no una ruta soportada por Maestro.
5. **Motor remoto**: `ComfyUI` y `compute_nodes` están implementados y probados por sus
   tests, pero **no se han ejercitado aquí** contra una máquina real.
6. **Suite completa**: 289 ficheros de test existen; en esta fase se ejecutaron las suites de
   la cola de producción y de las capas reutilizables (31 + 91 = **122 tests, OK**). El resto
   no se ejecutó (varias pruebas requieren GPU, FFmpeg o pesos de modelo).
7. **Modelo de referencia para LLM remoto**: la lista de modelos remotos se consulta al
   proveedor (`get_available_models`); los identificadores concretos pueden cambiar con el
   tiempo.

---

## 10. Resumen de una página

- **Qué es:** estudio local-first de IA audiovisual (vídeo/imagen/audio/LLM) para **un
  usuario**, con interfaz React servida por su propia API FastAPI y un motor de generación
  (WanGP) **dentro del mismo proceso**, sobre **GPU NVIDIA con CUDA**.
- **Qué ya está bien:** el cliente es fino; la API, la cola durable, el registro de
  capacidades, los nodos de cómputo con token, el adaptador de LLM con 4 proveedores, el
  motor ComfyUI externo, el gate de acceso y el acceso privado por Tailscale **ya existen**.
  **La indirección que pide la misión está, en gran parte, construida.**
- **Qué bloquea al hardware objetivo:** el launcher **aborta sin NVIDIA**; AMD/Radeon R5
  no está en ninguna matriz soportada; los kernels de atención son CUDA-only; los mínimos
  declarados (16 GB RAM, 150 GB disco, 6 GB VRAM) están por encima del equipo objetivo.
- **Qué NO hay que tocar:** el frontend, la cola, la persistencia JSON, los agentes/guías y
  los flujos de Films/Editor. La migración se hace **añadiendo proveedores** detrás del
  registro de capacidades.
- **Primer paso esperado (Fase 2):** decidir Opción A (backend en la máquina con GPU) vs
  Opción B (backend ligero local + nodo con GPU), medir el consumo real del backend, y definir
  el contrato del proveedor remoto antes de escribir código.
