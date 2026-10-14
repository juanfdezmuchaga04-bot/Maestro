# Biblioteca de modelos: acceso gratuito y descarga en cualquier ordenador

Este documento responde a dos preguntas para **todos** los modelos que trae Maestro
(211 definiciones de imagen, vídeo y audio + 11 LLM + MMAudio y Whisper = 224):

1. **¿Por dónde se puede usar cada modelo?** APIs gratuitas, créditos de prueba,
   Spaces públicos, webs gratuitas de los autores, APIs de pago (como referencia) y
   motores locales que no necesitan NVIDIA.
2. **¿Cómo se descargan en un ordenador sin NVIDIA?** Con la *Biblioteca de modelos*
   (modo **Models** de la interfaz) o con `scripts/maestro_models.py`, que solo
   necesita Python.

Investigación realizada el **2026-10-14**. Cada vía indica su fuente y su confianza:
`official` (documentación o ficha del propio proveedor), `secondary` (varias fuentes
de terceros coherentes) o `unverified` (una sola fuente o contradictoria; se muestra
para que puedas comprobarla, nunca como un hecho). Los datos viven en
`app/services/model_access.py`, no en este texto.

## Lo más importante

- **Descargar funciona en cualquier ordenador** (Windows, macOS, Linux; x86 o ARM; sin
  GPU). No hace falta PyTorch ni NVIDIA: solo Python y espacio en disco.
- **Ejecutar es otra cosa, y depende del modelo:**
  - **Texto (LLM GGUF) y transcripción (Whisper):** funcionan bien en cualquier CPU;
    Maestro ya trae llama.cpp y faster-whisper.
  - **Audio (TTS, música):** en CPU van por debajo del tiempo real, pero funcionan los
    modelos pequeños (Chatterbox, Qwen3-TTS 1.7B, ACE-Step, MMAudio).
  - **Imagen:** en CPU tardan minutos (Z-Image 6B, FLUX.2 klein 4B) o decenas de
    minutos (modelos de 12–32B) y piden 16–64 GB de RAM. En GPU AMD/Intel/Apple son
    viables con ComfyUI (ROCm, MPS, DirectML) o stable-diffusion.cpp (Vulkan, Metal).
  - **Vídeo:** **no es práctico en CPU** (horas por clip). Necesita una GPU capaz:
    NVIDIA, una AMD Radeon soportada por ROCm, o Apple Silicon con memoria suficiente.
    Sin ella, Maestro ya puede renderizar en **otro ordenador tuyo** (Studio nodes), en
    **cómputo público gratuito** (ZeroGPU) o en un **proveedor externo** (modo Cloud).
- No existe forma de que un ordenador sin GPU genere vídeo «como si tuviera una
  NVIDIA»: es un límite físico de cálculo, no de software. Lo que sí hace Maestro es
  que **todo lo demás** (descargar, planificar, editar, montar, texto, voz) funcione
  igual, y que el render se envíe a donde haya potencia.

## Formatos de pesos y hardware

Muchas definiciones traen varias precisiones del mismo archivo. El descargador elige
**una** según el hardware (detectado o elegido en «Plan for»):

| Formato | Dónde funciona | Elección por defecto |
|---|---|---|
| BF16 / FP16 | Cualquier hardware (máxima calidad, más memoria) | Perfil «Best quality» |
| INT8 (quanto) | Cualquier hardware con PyTorch; mitad de tamaño | Perfil «Smaller» |
| GGUF Q4/Q6/Q8 | CPU, Vulkan, Metal (llama.cpp / stable-diffusion.cpp) | Perfil «Smaller» si existe |
| FP8 scaled | Rápido en NVIDIA Ada/Hopper; en otro hardware hay que convertirlo | Nunca por defecto sin NVIDIA |
| NVFP4 | Solo NVIDIA Blackwell | Nunca por defecto sin NVIDIA |
| Nunchaku (INT4/FP4) | Solo NVIDIA CUDA | Nunca por defecto sin NVIDIA |

Las variantes que **solo** existen en un formato NVIDIA (p. ej. `*_nunchaku_*`,
`*_nvfp4`) aparecen marcadas «NVIDIA-only weights»; la ficha indica usar la variante
hermana portable de la misma familia.

## Licencias que hay que leer antes de descargar

La descarga exige aceptar expresamente la licencia cuando es no comercial, solo de
investigación o excluye territorios:

- **HunyuanVideo 1.0/1.5** (Tencent Hunyuan Community License): **no aplica en la Unión
  Europea, Reino Unido ni Corea del Sur.**
- **MiniMax H3**: el territorio de la licencia **excluye, según varias fuentes, EE. UU.,
  UE, Reino Unido y Corea del Sur** para despliegue local y mostrar resultados; la API
  alojada de MiniMax sí está disponible.
- **FLUX.1 [dev] y derivados, FLUX.2 [dev], FLUX.2 [klein] 9B**: no comercial.
- **Qwen-Image 2.1**: licencia de investigación (no comercial).
- **LTX-2.x**: gratis por debajo de 10 M$ de ingresos anuales.
- **Krea 2**: gratis por debajo de 50 puestos / umbral de ingresos; exige filtrado de contenido.
- Apache-2.0 / MIT (uso libre): Wan, FLUX.1 [schnell], FLUX.2 [klein] 4B, Qwen-Image 20B,
  Z-Image, ACE-Step, Chatterbox, Qwen3-TTS, Kandinsky 5 (según fuentes), Whisper.

## Servicios gratuitos encontrados

| Servicio | Tipo | Límite publicado | Modelos de Maestro que cubre | Confianza |
|---|---|---|---|---|
| Cloudflare Workers AI | cuota diaria | 10 000 Neurons/día sin tarjeta | FLUX.1 schnell, FLUX.2 klein 4B y 9B (**los tres integrados**), Whisper large-v3-turbo; FLUX.2 dev sin confirmar | official |
| Hugging Face ZeroGPU Spaces | Space público | ~2 min GPU/día anónimo, 5 con cuenta | Wan 2.2, LTX-2/2.3, Z-Image Turbo, ACE-Step 1.5, IndexTTS 2, Chatterbox (EN); HunyuanVideo 1.5 sin confirmar. **Integrado** como cómputo público | official / secondary |
| Hugging Face Inference Providers | crédito mensual | 0,10 $/mes en cuentas gratuitas | Wan 2.2 TI2V 5B, FLUX.1 dev/schnell | official |
| ModelScope API-Inference | cuota diaria | 2 000 llamadas/día (puede haber cambiado a saldo) | Qwen-Image 20B y 2.1, Z-Image Turbo (**integrados**) | secondary |
| Kaggle Notebooks | cuaderno con GPU NVIDIA | T4 ×2 o P100, ~30 h GPU/semana | Cualquier modelo que quepa en 16 GB de VRAM, ejecutado por ti dentro del cuaderno | secondary |
| Lightning AI Studios | créditos mensuales | ~80 h de T4 al mes (según terceros) | Igual que Kaggle | secondary |
| Vercel AI Gateway | crédito mensual | 5 $ cada 30 días a cuentas sin pagos | FLUX.2 klein 4B y 9B | secondary |
| Pollinations.AI | anónimo | ~1 petición / 15 s | FLUX (imagen) | secondary |
| Groq | cuota diaria | 2 000 peticiones/día, 8 h de audio/día | Whisper large-v3 | secondary |
| Google AI Studio | cuota diaria | Gemma 4 26B/31B ~1 500 peticiones/día | Gemma 4 **base** (no los fine-tunes de Maestro) | unverified |
| OpenRouter `:free` | cuota diaria | 50 peticiones/día (1 000 tras comprar 10 $) | la lista rota; Gemma/Qwen sin confirmar | secondary |
| SiliconFlow | modelos a 0 $ | lista rotativa + ~1 $ inicial | Qwen pequeños base, Kolors | secondary |
| acemusic.ai | web gratuita | "100 % gratis" según los autores; sin API documentada | ACE-Step | official |
| NVIDIA build.nvidia.com | créditos de prueba | ~1 000 créditos al registrarse | FLUX.1 dev/schnell/Kontext, FLUX.2 klein, Chatterbox Multilingual | secondary / official |
| Alibaba Model Studio | cuota de bienvenida | por modelo, visible en la consola | Wan 2.x, Qwen-Image, Qwen3-TTS (no confirmado si entra en la cuota) | official |

**Sin acceso remoto gratuito encontrado:** LongCat, Kandinsky 5 (solo pago: fal, Runware),
HiDream O1 (pago en fal), Krea 2 (API propia de pago), MiniMax H3 (API de pago),
LTX-2.5 (API de pago, mínimo 5 $), YuE/YuE2, HeartMuLa, KugelAudio, MiniMax-Music3,
modelos de audio LTX y todos los derivados/fine-tunes (VACE, MultiTalk, InfiniteTalk,
Phantom, SkyReels, Ovi, Lucy, Chrono Edit, etc.). Para ellos: local o un ordenador tuyo.

## Motores locales sin NVIDIA

| Motor | Hardware | Estado en Maestro |
|---|---|---|
| ComfyUI | NVIDIA, **AMD ROCm** (Windows Desktop ≥ v0.7.0 y Linux, solo Radeon/Ryzen AI soportadas), **Apple MPS**, CPU (`--cpu`) | Adaptador existente; plantilla Wan 2.2 5B. La instalación automática de Maestro aún exige NVIDIA |
| stable-diffusion.cpp | **CPU, Vulkan (AMD/Intel), Metal, OpenCL, SYCL** — FLUX.1/2, Wan 2.1/2.2, LTX-2.3, HunyuanVideo 1.5, Qwen-Image (GGUF) | No integrado |
| llama.cpp | CPU, Vulkan, Metal, CUDA, ROCm | Integrado (LLM) |
| faster-whisper | CPU int8, CUDA | Integrado (transcripción) |

## Cómo usarlo

**Interfaz:** modo **Models → Model library**. Filtra por tipo, «Free API» o «Runs on
<hardware>», abre un modelo para ver licencia, todas las vías de acceso con su fuente,
el motor local adecuado y la estimación en CPU. «Check files and sizes» pregunta al
Hub qué es público, cuánto pesa cada archivo y qué ya está instalado. «Download to
Maestro» descarga con verificación SHA-256, reanudación y cancelación.

**Línea de comandos** (sin arrancar Maestro, cualquier ordenador con Python 3.10+):

```bash
python scripts/maestro_models.py list --modality video --free-api
python scripts/maestro_models.py show z_image
python scripts/maestro_models.py plan i2v_2_2 --compact         # tamaños y disponibilidad
python scripts/maestro_models.py download z_image                # en esta máquina
python scripts/maestro_models.py download i2v_2_2 --for cuda     # preparar para un PC con NVIDIA
python scripts/maestro_models.py download hunyuan_1_5_t2v --accept-license
python scripts/maestro_models.py routes                          # todas las vías y motores
```

Los archivos quedan en `app/ckpts` y `app/loras/<familia>` exactamente con la
estructura que lee Maestro: se pueden copiar a otro ordenador con GPU y se usan tal cual.
Para repositorios con acceso restringido (*gated*), acepta la licencia en Hugging Face
con tu cuenta gratuita y conecta Hugging Face (acción del lanzador o `HF_TOKEN`); el
token solo se envía a `huggingface.co`, nunca al CDN al que redirige.

## Qué se ha probado y qué no

**Probado:**
- El manifiesto se genera sin PyTorch ni GPU importando los 27 handlers con módulos
  simulados; cubre las 211 definiciones, sus archivos auxiliares, codificadores de texto
  y LoRAs. Una prueba exige que esté al día con `app/defaults` y con la lista de
  handlers de `wgp.py`.
- 18 pruebas (`tests/test_model_downloads.py`) con un Hub simulado: elección de
  formato por hardware, módulos referenciados, rutas de LLM y Whisper, verificación
  SHA-256, reanudación con `Range`, hash incorrecto, repos restringidos con y sin
  token, token que no sale de `huggingface.co`, aceptación de licencia, disco lleno,
  cancelación, archivos ya instalados y la API HTTP.
- La interfaz recorrida en Chromium contra el router real con un **Hub simulado**:
  filtros, ficha con licencia y vías, comprobación de archivos, bloqueo hasta aceptar
  la licencia, progreso, cancelación, reanudación y omisión de lo ya instalado.

**No probado:**
- **Ninguna descarga real desde Hugging Face**: este entorno de desarrollo no tiene
  salida a `huggingface.co`. El formato de `/api/models/{repo}?blobs=true` se basa en
  la API pública documentada del Hub.
- Ningún servicio gratuito de la tabla se ha llamado desde aquí; los límites son los
  publicados en las fuentes indicadas.
- Ejecución de modelos en AMD, Apple o CPU: pendiente de medir en el equipo real (ver
  [DIAGNOSTICO-AMD-S145.md](DIAGNOSTICO-AMD-S145.md)).

## La vía «idéntica a NVIDIA»: nodo GPU gratuito (Kaggle / Lightning)

La única forma de obtener **exactamente** los resultados del motor de Maestro sin
tener una NVIDIA es ejecutar ese motor en una NVIDIA de otro sitio. Maestro ya delega
renders en otro Maestro (*peer*, ver [STUDIO-NODES.md](STUDIO-NODES.md)); el **nodo GPU
gratuito** convierte una sesión gratuita de cuaderno en ese peer.

1. Maestro → **Models → Free NVIDIA GPU** → descarga `maestro_free_gpu_node.ipynb`
   (también en `cloud/` del repositorio).
2. Kaggle → *Create → Notebook → File → Import notebook*. *Settings → Accelerator →
   GPU T4 ×2* e *Internet → On* (cuenta verificada por teléfono).
3. *Run all*. `scripts/maestro_cloud_node.py`:
   - mide GPU, versión CUDA del driver y discos (`--check` solo mide);
   - crea un entorno Python 3.11 aislado con `uv` e instala la build de PyTorch que
     usa el lanzador de Maestro y que admite ese driver (CUDA 13 → torch 2.10;
     12.8 → torch 2.7.1; P100 o driver 12.6 → torch 2.7.1 cu126) y `app/requirements.txt`;
   - pone `app/ckpts` y la caché de Hugging Face en el disco con más espacio;
   - arranca `app/launch.py` en modo token (`MAESTRO_ACCESS_TOKEN` aleatorio por
     sesión, escuchando solo en 127.0.0.1);
   - abre un túnel HTTPS de TryCloudflare (sin cuenta) hacia ese puerto;
   - arranca la cola del nodo e imprime un código `maestro-node:…`.
4. Pega el código en **Free NVIDIA GPU → Connect**. Maestro registra el peer
   `free-gpu` con el motor `maestro` (el pipeline real de Studio/Films), lo sondea y
   arranca la cola local. Desde entonces el enrutado envía los renders a esa GPU y
   trae los archivos de vuelta. Cada sesión nueva da una dirección nueva: pega el
   nuevo código (sustituye al anterior).

**Seguridad:** el servidor del cuaderno solo acepta el token de esa sesión (modo
token de `access_gate`); el código contiene ese token, así que no lo compartas. El
túnel desaparece al parar el cuaderno.

**Condiciones:** Kaggle limita a uso personal y no comercial y prohíbe la minería; no
menciona los túneles (zona gris): úsalo de forma personal y con la pestaña abierta.
Lightning AI admite servir puertos desde un Studio. **Colab gratuito no se puede usar**:
sus términos prohíben servir una interfaz web desde el cuaderno (el script se niega).

**Probado / no probado:** 7 pruebas (`tests/test_cloud_node.py`) cubren la lectura de
`nvidia-smi`, la elección de PyTorch, la elección de disco, la detección de la URL del
túnel, el código de conexión, que el cuaderno no lleve un token fijo y que el script
se niegue sin GPU. **No se ha ejecutado en Kaggle**: este entorno no tiene GPU ni red
hacia Kaggle. La instalación de los requisitos de Maestro en el Python de Kaggle, el
tiempo de arranque y qué modelos caben en la T4 con su RAM quedan por medir en la
primera sesión real.

## Anexo: los 224 modelos

Generado con `python scripts/maestro_models.py list --markdown --for cpu`.
«Pesos portables» indica si existe una variante que no requiere NVIDIA.

| Id | Modelo | Tipo | Licencia | Acceso remoto | Pesos portables (sin NVIDIA) |
|---|---|---|---|---|---|
| `ace_step_v1` | ACE-Step v1.0 3.5B | audio | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); acemusic.ai (ACE-Step authors) (web gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ace_step_v1_5` | ACE-Step v1.5 Turbo 2B | audio | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); acemusic.ai (ACE-Step authors) (web gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ace_step_v1_5_turbo_lm_0_6b` | ACE-Step v1.5 Turbo LM_0.6B 2B | audio | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); acemusic.ai (ACE-Step authors) (web gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ace_step_v1_5_turbo_lm_1_7b` | ACE-Step v1.5 Turbo LM_1.7B 2B | audio | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); acemusic.ai (ACE-Step authors) (web gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ace_step_v1_5_turbo_lm_4b` | ACE-Step v1.5 Turbo LM_4B 2B | audio | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); acemusic.ai (ACE-Step authors) (web gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ace_step_v1_5_xl` | ACE-Step v1.5 XL Turbo 4B | audio | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); acemusic.ai (ACE-Step authors) (web gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ace_step_v1_5_xl_sft` | ACE-Step v1.5 XL SFT 4B | audio | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); acemusic.ai (ACE-Step authors) (web gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ace_step_v1_5_xl_sft_lm_4b` | ACE-Step v1.5 XL SFT LM_4B 4B | audio | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); acemusic.ai (ACE-Step authors) (web gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ace_step_v1_5_xl_turbo_lm_4b` | ACE-Step v1.5 XL Turbo LM_4B 4B | audio | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); acemusic.ai (ACE-Step authors) (web gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `alpha` | Wan2.1 Alpha v1.0 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `alpha2` | Wan2.1 Alpha v2.0 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `alpha2_sf` | Wan2.1 Alpha v2.0 Lightning 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `alpha_sf` | Wan2.1 Alpha v1.0 Lightning 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `animate` | Wan2.2 Animate 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `chatterbox` | TTS Chatterbox Multilingual | audio | MIT | NVIDIA build.nvidia.com (NIM trial) (prueba); Hugging Face ZeroGPU Spaces (Space); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `chrono_edit` | Wan2.1 Chrono Edit 14B | image | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `chrono_edit_distill` | Wan2.1 Chrono Edit Distill 14B | image | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `dramabox_audio` | DramaBox Audio | audio | LTX-2 Community License | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `fantasy` | Fantasy Talking 720p 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flf2v_720p` | First Last Frame to Video 720p (FLF2V) 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Alibaba Cloud Model Studio (DashScope) (prueba); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux` | Flux 1 Dev 12B | image | FLUX.1 [dev] Non-Commercial License | NVIDIA build.nvidia.com (NIM trial) (prueba); Hugging Face Inference Providers (gratis); Hugging Face ZeroGPU Spaces (Space, no verificado); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux2_dev` | Flux 2 Dev 32B | image | FLUX Non-Commercial License | Cloudflare Workers AI (gratis, no verificado); Black Forest Labs API (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux2_dev_nvfp4` | Flux 2 Dev NVFP4 32B | image | FLUX Non-Commercial License | Cloudflare Workers AI (gratis, no verificado); Black Forest Labs API (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `flux2_klein_4b` | Flux 2 Klein 4B | image | Apache-2.0 | Cloudflare Workers AI (gratis); Vercel AI Gateway (gratis); NVIDIA build.nvidia.com (NIM trial) (prueba); Black Forest Labs API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux2_klein_9b` | Flux 2 Klein 9B | image | FLUX Non-Commercial License | Cloudflare Workers AI (gratis); Vercel AI Gateway (gratis); Black Forest Labs API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux2_klein_base_4b` | Flux 2 Klein Base 4B | image | Apache-2.0 | Cloudflare Workers AI (gratis); Vercel AI Gateway (gratis); NVIDIA build.nvidia.com (NIM trial) (prueba); Black Forest Labs API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux2_klein_base_9b` | Flux 2 Klein Base 9B | image | FLUX Non-Commercial License | Cloudflare Workers AI (gratis); Vercel AI Gateway (gratis); Black Forest Labs API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux_chroma` | Flux 1 Chroma 1 HD 8.9B | image | Apache-2.0 (reported) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux_chroma_radiance` | Flux 1 Chroma Radiance 8.9B | image | Apache-2.0 (reported) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux_dev_kontext` | Flux 1 Dev Kontext 12B | image | FLUX.1 [dev] Non-Commercial License | NVIDIA build.nvidia.com (NIM trial) (prueba); Hugging Face ZeroGPU Spaces (Space, no verificado); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux_dev_kontext_dreamomni2` | Flux 1 DreamOmni2 12B | image | FLUX.1 [dev] Non-Commercial License | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux_dev_umo` | Flux 1 UMO 12B | image | FLUX.1 [dev] Non-Commercial License | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux_dev_uso` | Flux 1 USO Dev 12B | image | FLUX.1 [dev] Non-Commercial License | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux_krea` | Flux 1 Dev Krea 12B | image | FLUX.1 [dev] Non-Commercial License | Hugging Face ZeroGPU Spaces (Space, no verificado); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux_schnell` | Flux 1 Schnell 12B | image | Apache-2.0 | Cloudflare Workers AI (gratis); Pollinations.AI (gratis); NVIDIA build.nvidia.com (NIM trial) (prueba); Hugging Face Inference Providers (gratis); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux_srpo` | Flux 1 Dev SRPO 12B | image | FLUX.1 [dev] Non-Commercial License | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `flux_srpo_uso` | Flux 1 USO SRPO 12B | image | FLUX.1 [dev] Non-Commercial License | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `fun_inp` | Fun InP image2video 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `fun_inp_1.3B` | Fun InP image2video 1.3B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `heartmula_oss_3b` | HeartMuLa OSS 3B | audio | See model card | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `heartmula_rl_oss_3b_20260123` | HeartMuLa RL OSS (20260123) 3B | audio | See model card | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hidream_o1` | HiDream O1 Image Full 10B | image | See model card (not confirmed) | fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hidream_o1_dev` | HiDream O1 Image Dev 10B | image | See model card (not confirmed) | fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan` | Hunyuan Video Text2video 720p 13B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_1_5_480_i2v` | Hunyuan Video 1.5  Image2video 480p 8B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Hugging Face ZeroGPU Spaces (Space, no verificado); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_1_5_480_i2v_step_distilled` | Hunyuan Video 1.5  Image2video 480p Step Distilled 8B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Hugging Face ZeroGPU Spaces (Space, no verificado); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_1_5_480_t2v` | Hunyuan Video 1.5  Text2video 480p 8B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Hugging Face ZeroGPU Spaces (Space, no verificado); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_1_5_480_t2v_lightx2v` | Hunyuan Video 1.5 Text2video 480p Lightx2v 8B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Hugging Face ZeroGPU Spaces (Space, no verificado); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_1_5_i2v` | Hunyuan Video 1.5 Image2video 720p 8B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Hugging Face ZeroGPU Spaces (Space, no verificado); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_1_5_t2v` | Hunyuan Video 1.5  Text2video 720p 8B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Hugging Face ZeroGPU Spaces (Space, no verificado); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_1_5_upsampler` | Hunyuan Video 1.5  Upsampler 720p 8B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Hugging Face ZeroGPU Spaces (Space, no verificado); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_1_5_upsampler_1080` | Hunyuan Video 1.5  Upsampler 1080p 8B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Hugging Face ZeroGPU Spaces (Space, no verificado); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_avatar` | Hunyuan Video Avatar 720p 13B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_custom` | Hunyuan Video Custom 720p 13B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_custom_audio` | Hunyuan Video Custom Audio 720p 13B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_custom_edit` | Hunyuan Video Custom Edit 720p 13B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_i2v` | Hunyuan Video Image2video 720p 13B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_t2v_accvideo` | Hunyuan Video Text2video 720p AccVideo 13B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `hunyuan_t2v_fast` | Hunyuan Video Text2video 720p FastHunyuan 13B | video | Tencent Hunyuan Community License ⚠ territorio restringido | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `i2v` | Wan2.1 Image2video 480p 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `i2v_2_2` | Wan2.2 Image2video 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Hugging Face ZeroGPU Spaces (Space); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `i2v_2_2_Enhanced_Lightning_v2` | Wan2.2 Image2video Enhanced Lightning v2 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Hugging Face ZeroGPU Spaces (Space); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `i2v_2_2_Enhanced_Lightning_v2_svi2pro` | Wan2.2 Image2video SVI 2 Pro Enhanced Lightning v2 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Hugging Face ZeroGPU Spaces (Space); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `i2v_2_2_multitalk` | Wan2.2 Multitalk 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Hugging Face ZeroGPU Spaces (Space); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `i2v_2_2_svi2pro` | Wan2.2 Image2video SVI 2 Pro 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Hugging Face ZeroGPU Spaces (Space); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `i2v_720p` | Wan2.1 Image2video 720p 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Alibaba Cloud Model Studio (DashScope) (prueba); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `i2v_fusionix` | Wan2.1 Image2video 480p FusioniX 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `i2v_nvfp4` | Wan2.1 Image2video 480p 14B NVFP4 Lightx2v 4-step | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `index_tts2` | TTS Index TTS 2 | audio | Bilibili Index model licence (commercial use below user/revenue thresholds — check the file) | Hugging Face ZeroGPU Spaces (Space); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `infinitetalk` | Infinitetalk Single Speaker 480p 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `infinitetalk_multi` | Infinitetalk Multi Speakers 480p 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `k5_lite_i2v` | Kandinsky 5 Lite I2V SFT 5s 2B | video | Apache-2.0 (reported) | fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `k5_lite_t2v` | Kandinsky 5 Lite T2V SFT 5s 2B | video | Apache-2.0 (reported) | fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `k5_lite_t2v_10s_distil` | Kandinsky 5 Lite T2V Distilled 10s 2B | video | Apache-2.0 (reported) | fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `k5_lite_t2v_10s_distil_sparse` | Kandinsky 5 Lite T2V Distilled 10s Sparse 2B | video | Apache-2.0 (reported) | fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `k5_lite_t2v_5s_distil` | Kandinsky 5 Lite T2V Distilled 5s 2B | video | Apache-2.0 (reported) | fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `k5_pro_i2v` | Kandinsky 5 Pro I2V SFT 5s 19B | video | Apache-2.0 (reported) | fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `k5_pro_t2v` | Kandinsky 5 Pro T2V SFT 5s 19B | video | Apache-2.0 (reported) | fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `k5_pro_t2v_10s_sft` | Kandinsky 5 Pro T2V SFT 10s 19B | video | Apache-2.0 (reported) | fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `k5_pro_t2v_10s_sft_sparse` | Kandinsky 5 Pro T2V SFT 10s Sparse 19B | video | Apache-2.0 (reported) | fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `kiwi_edit` | Kiwi Edit Instruct-Reference 5B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `kiwi_edit_instruct_only` | Kiwi Edit Instruct-Only 5B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `kiwi_edit_reference_only` | Kiwi Edit Reference-Only 5B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `krea2_raw` | Krea 2 RAW | image | Krea Community License (free under 50 seats / revenue threshold; content filtering required) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `krea2_raw_edit` | Krea 2 RAW Identity Edit v1.2 | image | Krea Community License (free under 50 seats / revenue threshold; content filtering required) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `krea2_turbo` | Krea 2 Turbo | image | Krea Community License (free under 50 seats / revenue threshold; content filtering required) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `krea2_turbo_edit` | Krea 2 Turbo Identity Edit v1.2 | image | Krea Community License (free under 50 seats / revenue threshold; content filtering required) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `kugelaudio_0_open` | TTS KugelAudio 0 Open 7B | audio | See model card | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `longcat_avatar` | LongCat Avatar Single 13.6B | video | MIT (reported, unconfirmed) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `longcat_avatar_multi` | LongCat Avatar Multi 13.6B | video | MIT (reported, unconfirmed) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `longcat_video` | LongCat Video 13.6B | video | MIT (reported, unconfirmed) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_19B` | LTX-2.0 Dev 19B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_19B_nvfp4` | LTX-2.0 Dev NVFP4 19B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `ltx2_22B` | LTX-2.3 Dev 22B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_22B_10eros` | LTX-2.3 22B 10Eros (Mature) | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_22B_10eros_v14` | LTX-2.3 22B 10Eros v1.4 (Mature) | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `ltx2_22B_1_1` | LTX-2.3 Dev 1.1 22B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_22B_distilled` | LTX-2.3 Distilled 22B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_22B_distilled_1_1` | LTX-2.3 Distilled 1.1 22B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_22B_distilled_1_1_omninft` | LTX-2.3 Distilled 1.1 22B + OmniNFT Sync | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_22B_distilled_fp8` | LTX-2.3 Distilled FP8 22B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `ltx2_22B_distilled_gguf_q4_k_m` | LTX-2.3 Distilled GGUF Q4_K_M Light 22B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_22B_distilled_gguf_q6_k` | LTX-2.3 Distilled GGUF Q6_K Light 22B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_22B_distilled_gguf_q8_0` | LTX-2.3 Distilled GGUF Q8_0 Light 22B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_22B_fp8` | LTX-2.3 Dev FP8 22B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `ltx2_22B_nvfp4` | LTX-2.3 Dev NVFP4 22B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `ltx2_22B_sulphur2_distilled` | LTX-2.3 22B Sulphur 2 Distilled FP8 (Mature) | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `ltx2_25` | LTX-2.5 — Distilled | video | LTX-2.x Community License (free below $10M revenue); Hugging Face repo is gated | LTX API (Lightricks) (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_25_dev` | LTX-2.5 — Dev | video | LTX-2.x Community License (free below $10M revenue); Hugging Face repo is gated | LTX API (Lightricks) (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_25_nvfp4` | LTX-2.5 — Distilled NVFP4 | video | LTX-2.x Community License (free below $10M revenue); Hugging Face repo is gated | LTX API (Lightricks) (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `ltx2_distilled` | LTX-2.0 Distilled 19B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_distilled_gguf_q4_k_m` | LTX-2 2.0 Distilled GGUF Q4_K_M 19B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_distilled_gguf_q6_k` | LTX-2 2.0 Distilled GGUF Q6_K 19B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltx2_distilled_gguf_q8_0` | LTX-2 2.0 Distilled GGUF Q8_0 19B | video | LTX-2 Community License (free below $10M annual revenue) | Hugging Face ZeroGPU Spaces (Space); LTX API (Lightricks) (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltxv_13B` | LTX Video 0.9.8 13B | video | LTXV Open Weights License | Hugging Face ZeroGPU Spaces (Space, no verificado); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ltxv_distilled` | LTX Video 0.9.8 Distilled 13B | video | LTXV Open Weights License | Hugging Face ZeroGPU Spaces (Space, no verificado); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `lucy_edit` | Wan2.2 Lucy Edit v1 5B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `lucy_edit_1_1` | Wan2.2 Lucy Edit v1.1 5B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `lucy_edit_fastwan` | Wan2.2 Lucy Edit v1 FastWan 5B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `lucy_edit_fastwan_1_1` | Wan2.2 Lucy Edit v1.1 FastWan 5B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `lynx` | Wan2.1 Lynx 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `minimax_h3` | H3 First / Last — Pruned | video | MiniMax H3 licence (commercial use allowed with attribution; >$20M revenue needs authorisation) ⚠ territorio restringido | MiniMax platform API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `minimax_h3_full` | H3 First / Last — Full | video | MiniMax H3 licence (commercial use allowed with attribution; >$20M revenue needs authorisation) ⚠ territorio restringido | MiniMax platform API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `minimax_h3_fused_turbo` | H3 Fused 4-Step — Frames (Experimental) | video | MiniMax H3 licence (commercial use allowed with attribution; >$20M revenue needs authorisation) ⚠ territorio restringido | MiniMax platform API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `minimax_h3_ref2va` | H3 Omni — Pruned | video | MiniMax H3 licence (commercial use allowed with attribution; >$20M revenue needs authorisation) ⚠ territorio restringido | MiniMax platform API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `minimax_h3_ref2va_full` | H3 Omni — Full | video | MiniMax H3 licence (commercial use allowed with attribution; >$20M revenue needs authorisation) ⚠ territorio restringido | MiniMax platform API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `minimax_h3_ref2va_fused_turbo` | H3 Fused 4-Step — References (Experimental) | video | MiniMax H3 licence (commercial use allowed with attribution; >$20M revenue needs authorisation) ⚠ territorio restringido | MiniMax platform API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `minimax_h3_ref2va_singularity` | H3 Singularity — References (Experimental) | video | MiniMax H3 licence (commercial use allowed with attribution; >$20M revenue needs authorisation) ⚠ territorio restringido | MiniMax platform API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `minimax_h3_singularity` | H3 Singularity — Frames (Experimental) | video | MiniMax H3 licence (commercial use allowed with attribution; >$20M revenue needs authorisation) ⚠ territorio restringido | MiniMax platform API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `minimax_h3_vdn` | H3 VDN First / Last — Pruned | video | MiniMax H3 licence (commercial use allowed with attribution; >$20M revenue needs authorisation) ⚠ territorio restringido | MiniMax platform API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `minimax_h3_vdn_full` | H3 VDN First / Last — Full | video | MiniMax H3 licence (commercial use allowed with attribution; >$20M revenue needs authorisation) ⚠ territorio restringido | MiniMax platform API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `minimax_h3_voice_audio` | H3 Voice Audio — Pruned | video | MiniMax H3 licence (commercial use allowed with attribution; >$20M revenue needs authorisation) ⚠ territorio restringido | MiniMax platform API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `minimax_music3` | MiniMax-Music3 | audio | See model card ⚠ territorio restringido | MiniMax platform API (pago, no verificado); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `mocha` | Wan2.1 MoCha 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `moviigen` | MoviiGen 1080p 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `multitalk` | Multitalk 480p 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `multitalk_720p` | Multitalk 720p 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ovi` | Wan2.2 Ovi v1.0 5s 10B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ovi_1_1` | Wan2.2 Ovi v1.1 5s 10B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ovi_1_1_10s` | Wan2.2 Ovi v1.1 10s 10B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ovi_1_1_10s_fastwan` | Wan2.2 Ovi v1.1 FastWan 10s 10B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ovi_1_1_fastwan` | Wan2.2 Ovi v1.1 FastWan 5s 10B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ovi_fastwan` | Wan2.2 Ovi v1.0 FastWan 5s 10B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `phantom_1.3B` | Phantom 1.3B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `phantom_14B` | Phantom 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `pi_flux2` | pi-FLUX.2 Dev 32B | image | FLUX Non-Commercial License | Cloudflare Workers AI (gratis, no verificado); Black Forest Labs API (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `pi_flux2_nvfp4` | pi-FLUX.2 Dev NVFP4 32B | image | FLUX Non-Commercial License | Cloudflare Workers AI (gratis, no verificado); Black Forest Labs API (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `qwen3_tts_base` | TTS Qwen3 Base (12Hz) 1.7B | audio | Apache-2.0 | Alibaba Cloud Model Studio (DashScope) (prueba); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `qwen3_tts_customvoice` | TTS Qwen3 Custom Voice (12Hz) 1.7B | audio | Apache-2.0 | Alibaba Cloud Model Studio (DashScope) (prueba); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `qwen3_tts_voicedesign` | TTS Qwen3 Voice Design (12Hz) 1.7B | audio | Apache-2.0 | Alibaba Cloud Model Studio (DashScope) (prueba); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `qwen_image_20B` | Qwen Image 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `qwen_image_21_7B` | Qwen Image 2.1 7B | image | Qwen Research License — non-commercial research/evaluation only | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `qwen_image_2512_20B` | Qwen Image 2512 Release 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `qwen_image_edit_20B` | Qwen Image Edit 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `qwen_image_edit_20B_gguf_q4_k_m` | Qwen Image Edit GGUF Q4_K_M 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `qwen_image_edit_20B_gguf_q6_k` | Qwen Image Edit GGUF Q6_K 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `qwen_image_edit_20B_gguf_q8_0` | Qwen Image Edit GGUF Q8_0 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `qwen_image_edit_2511_20B_fp8` | Qwen Image Edit (2511) FP8 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `qwen_image_edit_2511_20B_fp8_lightning_4step` | Qwen Image Edit (2511) Lightning 4-Step FP8 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `qwen_image_edit_2511_20B_fp8_lightning_8step` | Qwen Image Edit (2511) Lightning 8-Step FP8 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `qwen_image_edit_plus2_20B` | Qwen Image Edit Plus (2511) 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `qwen_image_edit_plus_20B` | Qwen Image Edit Plus (2509) 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `qwen_image_edit_plus_20B_nunchaku_r128_fp4` | Qwen Image Edit Plus (2509) Nunchaku FP4 r128 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `qwen_image_edit_plus_20B_nunchaku_r128_int4` | Qwen Image Edit Plus (2509) Nunchaku INT4 r128 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `qwen_image_layered_20B` | Qwen Image Layered 20B | image | Apache-2.0 | ModelScope API-Inference (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Replicate (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `recam_1.3B` | ReCamMaster 1.3B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `scail` | SCAIL Preview 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `scail2_14B` | SCAIL-2 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `scail2_14B_fast` | SCAIL-2 14B Fast | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `scail2_14B_recast_fast` | SCAIL-2 Recast Fast | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `scenema_audio` | Scenema Audio | audio | LTX-2 Community License | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `sky_df_1.3B` | SkyReels2 Diffusion Forcing 1.3B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `sky_df_14B` | SkyReels2 Diffusion Forcing 540p 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `sky_df_720p_14B` | SkyReels2 Diffusion Forcing 720p 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `standin` | Wan2.1 Standin 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `steadydancer` | Wan2.1 Steady Dancer 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `t2v` | Wan2.1 Text2video 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Alibaba Cloud Model Studio (DashScope) (prueba); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `t2v_1.3B` | Wan2.1 Text2video 1.3B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `t2v_1.3B_nvfp4` | Wan2.1 Text2video NVFP4 Lightx2v 4-step 1.3B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `t2v_2_2` | Wan2.2 Text2video 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Hugging Face ZeroGPU Spaces (Space); Alibaba Cloud Model Studio (DashScope) (prueba); Replicate (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `t2v_fusionix` | Wan2.1 Text2video FusioniX 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `t2v_nexus_1.3B` | Wan2.1 Text2video Nexus 1.3B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `t2v_sf` | Wan2.1 Text2video Lightning 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ti2v_2_2` | Wan2.2 TextImage2video 5B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Hugging Face ZeroGPU Spaces (Space); Hugging Face Inference Providers (gratis); Alibaba Cloud Model Studio (DashScope) (prueba); Replicate (pago); fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `ti2v_2_2_fastwan` | Wan2.2 TextImage2video FastWan 5B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Hugging Face ZeroGPU Spaces (Space); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_1.3B` | Vace 1.3B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_14B` | Vace 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | fal.ai (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_14B_2_2` | Wan2.2 Vace 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_14B_cocktail` | Vace Cocktail 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_14B_cocktail_2_2` | Wan2.2 Vace Experimental Cocktail 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_14B_fusionix` | Vace FusioniX 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_14B_lightning_3p_2_2` | Wan2.2 Vace Lightning 3 Phases 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_14B_sf` | Vace Self-Forcing 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_ditto_14B` | Ditto 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_fun_14B_2_2` | Wan2.2 Vace Fun 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_fun_14B_cocktail_2_2` | Wan2.2 Vace Fun Cocktail 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_lynx_14B` | Vace Lynx 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_multitalk_14B` | Vace Multitalk FusioniX 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `vace_standin_14B` | Vace Standin 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `viggle_animate` | Viggle Animate — 3-Step | video | MiniMax H3 licence (commercial use allowed with attribution; >$20M revenue needs authorisation) ⚠ territorio restringido | MiniMax platform API (pago); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `wanmove` | Wan2.1 Wan-Move 480p 14B | video | Apache-2.0 (Wan base models; derivatives keep their own card) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `yue2` | YuE2 3B | audio | See model card (non-commercial terms) | Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `z_image` | Z-Image Turbo 6B | image | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); ModelScope API-Inference (gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `z_image_base` | Z-Image Base 6B | image | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); ModelScope API-Inference (gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `z_image_control` | Z-Image Turbo Fun ControlNet v1 6B | image | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); ModelScope API-Inference (gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `z_image_control2` | Z-Image Turbo Fun ControlNet v2 6B | image | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); ModelScope API-Inference (gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `z_image_control2_1` | Z-Image Turbo Fun ControlNet v2.1 6B | image | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); ModelScope API-Inference (gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `z_image_control2_1_8s` | Z-Image Turbo Fun ControlNet v2.1 8 Steps YYY 6B | image | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); ModelScope API-Inference (gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `z_image_nunchaku_r128_fp4` | Z-Image Turbo Nunchaku FP4 (r128) 6B | image | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); ModelScope API-Inference (gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `z_image_nunchaku_r256_int4` | Z-Image Turbo Nunchaku INT4 (r256) 6B | image | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); ModelScope API-Inference (gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | no (solo NVIDIA) |
| `z_image_twinflow_turbo` | TwinFlow Z-Image Turbo 6B | image | Apache-2.0 | Hugging Face ZeroGPU Spaces (Space); ModelScope API-Inference (gratis); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `llm:Abhiray/gemma-4-E4B-it-heretic-GGUF` | Gemma 4 4B Heretic Uncensored (Vision, Fast) (Recommended) | text | Gemma terms of use (base); fine-tunes add their own card | Google AI Studio (Gemini API) (gratis, no verificado); OpenRouter :free models (gratis, no verificado) | sí |
| `llm:Jiunsong/supergemma4-26b-uncensored-gguf-v2` | Gemma 4 26B MoE Uncensored (Fast) | text | Gemma terms of use (base); fine-tunes add their own card | Google AI Studio (Gemini API) (gratis, no verificado); OpenRouter :free models (gratis, no verificado) | sí |
| `llm:JonathanColetti/Qwen3.8-27B-Uncensored-GGUF` | Qwen3.8 27B Uncensored Q4_K_M (Vision, Deep Thinking) | text | Apache-2.0 (base) | OpenRouter :free models (gratis, no verificado); SiliconFlow (gratis, modelo base) | sí |
| `llm:Nesuwka/gemma-4-E2B-it-heretic-ara-Q4_K_M-GGUF` | Gemma 4 2B Heretic Uncensored (Vision, Tiny) | text | Gemma terms of use (base); fine-tunes add their own card | Google AI Studio (Gemini API) (gratis, no verificado); OpenRouter :free models (gratis, no verificado) | sí |
| `llm:SulphurAI/Sulphur-2-base` | Sulphur-2 Uncensored Prompt Enhancer (Vision) | text | Apache-2.0 (base) | OpenRouter :free models (gratis, no verificado); SiliconFlow (gratis, modelo base) | sí |
| `llm:Youssofal/Qwen3.6-27B-Abliterated-Heretic-Uncensored-GGUF` | Qwen3.6 27B Abliterated Heretic (Uncensored, Vision) | text | Apache-2.0 (base) | OpenRouter :free models (gratis, no verificado); SiliconFlow (gratis, modelo base) | sí |
| `llm:mradermacher/Huihui-Qwen3.5-9B-Claude-4.6-Opus-abliterated-i1-GGUF` | Qwen3.5 9B Claude Opus Abliterated Q6_K | text | Apache-2.0 (base) | OpenRouter :free models (gratis, no verificado); SiliconFlow (gratis, modelo base) | sí |
| `llm:mradermacher/gemma-4-12B-it-abliterated-uncensored-i1-GGUF` | Gemma 4 12B Abliterated (Text, Experimental) | text | Gemma terms of use (base); fine-tunes add their own card | Google AI Studio (Gemini API) (gratis, no verificado); OpenRouter :free models (gratis, no verificado) | sí |
| `llm:paperscarecrow/Gemma-4-31B-it-abliterated-gguf` | Gemma 4 31B Abliterated Q4_K_M (Vision) | text | Gemma terms of use (base); fine-tunes add their own card | Google AI Studio (Gemini API) (gratis, no verificado); OpenRouter :free models (gratis, no verificado) | sí |
| `llm:unsloth/Qwen3.5-2B-GGUF` | Qwen3.5 2B (Fast) | text | Apache-2.0 (base) | OpenRouter :free models (gratis, no verificado); SiliconFlow (gratis, modelo base) | sí |
| `llm:unsloth/Qwen3.5-4B-GGUF` | Qwen3.5 4B (Balanced) | text | Apache-2.0 (base) | OpenRouter :free models (gratis, no verificado); SiliconFlow (gratis, modelo base) | sí |
| `extra:mmaudio` | MMAudio (sound effects for video) | audio | MIT (code) / CC-BY-NC (weights) | Hugging Face ZeroGPU Spaces (Space, no verificado); Kaggle Notebooks (free NVIDIA GPU) (cuaderno GPU gratis); Lightning AI Studios (free monthly credits) (cuaderno GPU gratis) | sí |
| `extra:whisper_small` | faster-whisper small (lyrics / speech transcription) | text | MIT | Cloudflare Workers AI (gratis); Groq (gratis) | sí |
