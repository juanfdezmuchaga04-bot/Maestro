# Instalar Maestro en un Mac (Apple Silicon, runtime CPU / ligero)

Pinokio funciona en macOS y presenta el equipo como plataforma `darwin`. Maestro **se
instala y funciona** en un Mac, pero el motor de generación de WanGP es **sólo CUDA**: en un
Mac no hay generación local. Lo que sí queda operativo es todo lo demás — interfaz, Studio,
Films, editor, cola de producción, montaje/export con FFmpeg, planeación con LLM local y
**nodos remotos con GPU** —, y la generación se hace en un equipo con NVIDIA.

Antes, `torch.js` rechazaba macOS de plano («Maestro's CPU / light runtime is supported on
Windows and Linux.»), así que la instalación moría en el paso de PyTorch. Ahora macOS usa el
mismo entorno ligero que un PC sin NVIDIA.

## Requisitos reales

| Requisito | Valor |
| --- | --- |
| Mac | **Apple Silicon** (M1 o posterior). Ver *Intel Mac* en Límites |
| macOS | **14 (Sonoma) o superior** (lo fija la rueda de `av` para arm64) |
| RAM | 8 GB mínimo real; 16 GB recomendado (el backend carga todo en un proceso) |
| Disco | ~15–25 GB sin pesos de modelos; 50–100 GB si además descargas modelos |
| FFmpeg | en el `PATH` (`brew install ffmpeg`); sin él, el montaje y la exportación fallan |
| Pinokio | versión de escritorio para macOS ([descarga](https://pinokio.computer)) |

## Qué entorno se instala y con qué ruedas

`launcher_profile.js` ya no depende de que Pinokio acierte con la GPU: en macOS la
plataforma basta, y por eso también funciona aunque el inventario de GPU llegue vacío.

| Comprobación | Resultado en macOS |
| --- | --- |
| Entorno | **`app/env-light`** (Python 3.10) |
| PyTorch | `torch==2.7.1`, `torchvision==0.22.1`, `torchaudio==2.7.1` desde `download.pytorch.org/whl/cpu`; ese índice sirve las ruedas `macosx_11_0_arm64` |
| Aceleración CUDA | no se instala: `triton`, SageAttention, FlashAttention, `nunchaku`, kernels `lightx2v` y `xformers` se omiten |
| Inpaint (SAM 3.1) | la acción **no aparece** en el menú: su instalador crea un entorno conda con PyTorch CUDA |
| Kernel GGUF | `scripts/install_gguf_kernels.py` es un no-op sin CUDA (sólo afecta a variantes GGUF) |

Tres dependencias de `app/requirements.txt` necesitan una variante en macOS: ninguna de las
tres es un capricho, cada una **abortaba la instalación completa** en un Mac.

| Paquete | En Windows/Linux | En macOS | Motivo |
| --- | --- | --- | --- |
| `decord` | `decord==0.6.0` | `eva-decord==0.6.1` | `decord` 0.6.0 no publica rueda para macOS arm64 ni para Python 3.10; el fork de EVA expone el **mismo módulo** `decord` (`VideoReader`, `bridge`) |
| `onnxruntime-gpu` | `onnxruntime-gpu` | `onnxruntime==1.22.0` | `onnxruntime-gpu` no tiene ninguna compilación para macOS; la rueda CPU es `universal2`, así que sirve para Apple Silicon e Intel |
| `rembg[gpu]` | `rembg[gpu]` | `rembg` | el extra `gpu` arrastra `onnxruntime-gpu` |

El resto de `requirements.txt` es común: es una decisión deliberada para no desincronizar la
lista de dependencias del código.

## Instalación paso a paso

1. Pinokio → **Install**. Verás un aviso informativo que además explica la situación:
   *«macOS detected… Installing Maestro's CPU / light runtime…»*. **No detiene nada.**
2. La instalación crea `app/env-light`, instala las dependencias (con las variantes de la
   tabla anterior), clona el componente seed-vc y compila la interfaz React.
3. Al terminar, **Start**. En el menú de Pinokio verás la etiqueta *«CPU / light runtime —
   macOS has no NVIDIA GPU…»*.
4. Abre la **Web UI**. La propia app informa en su auditoría de arranque de que no hay CUDA
   («No CUDA GPU detected…; generation must run on a machine with an NVIDIA GPU»).

## Generar desde un Mac

El Mac **dirige**; la GPU está en otro equipo. Dos caminos soportados:

- **Nodos de cómputo** — registra un equipo con NVIDIA como nodo (token propio) y encola
  desde Studio. Ver [`STUDIO-NODES.md`](STUDIO-NODES.md).
- **Motor local remoto (ComfyUI)** — apunta Maestro a la URL de la máquina con GPU. Ver
  [`COMFYUI-LOCAL.md`](COMFYUI-LOCAL.md) y el panel *Studio → Compute & transfer*.

En ambos casos el Mac conserva el proyecto, el storyboard, la cola, la biblioteca y el
montaje: la generación sólo ocurre fuera.

## Usar Maestro desde tu iPhone o iPad

Sí: el Mac se convierte en el servidor y el iPhone/iPad en el cliente. La ruta completa
—contraseña de acceso, HTTPS privado con Tailscale, icono en la pantalla de inicio y
notificaciones— está en **[Maestro en tu iPhone o iPad](DEPLOY-IOS.md)**.

Detalle específico de macOS: el CLI de Tailscale vive dentro del paquete de la aplicación
(`/Applications/Tailscale.app/Contents/MacOS/Tailscale`). `tailscale_setup.js` y
`scripts/maestro_doctor.py` ya lo buscan ahí, así que **Secure Remote Access (Tailscale)**
funciona en un Mac aunque no hayas instalado los comandos de línea de órdenes.

## Diagnóstico

```bash
python scripts/maestro_doctor.py
```

Informe de sólo lectura: GPU/CUDA, FFmpeg, disco, estado del bundle de la UI, backend, gate
de acceso, motor local y la dirección privada para el móvil, con los pasos siguientes.
Los registros completos están en `logs/api/install.js/latest` y `logs/api/start.js/latest`
(ver README → *Finding logs*).

## Límites conocidos

- **Intel Mac: no soportado.** PyTorch dejó de publicar ruedas macOS x86_64 (la última es
  2.2.2), así que `torch==2.7.1` no se puede resolver y la instalación se detendrá en el paso
  de PyTorch. Hace falta un Mac con Apple Silicon.
- **Sin generación local.** Ni MPS ni Metal se usan en el pipeline (es CUDA-only); el nombre
  del entorno es *CPU / light* precisamente para no dar a entender lo contrario.
- **Sin Inpaint (SAM 3.1)** y sin los kernels CUDA opcionales.
- **Planeación con LLM local**: funciona, pero en CPU es lenta; con 8 GB de RAM evita cargar
  modelos grandes.
- Este documento cubre el launcher y las dependencias, no el rendimiento. En un Mac modesto,
  el uso realista es dirigir y montar, no renderizar.
