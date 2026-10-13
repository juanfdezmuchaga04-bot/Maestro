# Instalar Maestro sin tarjeta NVIDIA (runtime CPU / ligero)

Esta guía cubre la instalación desde Pinokio en un equipo **AMD/Intel sin GPU NVIDIA**
(por ejemplo el Lenovo A9-9425 + Radeon R5 + 8 GB de RAM usado como equipo objetivo), y
explica exactamente qué funciona, qué no, y cómo hacer que el conjunto sea útil.

Un **Mac** entra en esta misma familia (mismo runtime ligero) pero tiene sus propias
variantes de dependencias y un requisito de hardware distinto: ver
[Instalar Maestro en un Mac](INSTALAR-EN-MAC.md).
Para usar el móvil como cliente, ver [Maestro en tu iPhone o iPad](DEPLOY-IOS.md).

## Qué cambió en el launcher

Antes, `install.js` **abortaba** la instalación con «This app requires an NVIDIA GPU on
Windows or Linux.» y dejaba el repositorio sin instalar por la vía soportada. Ahora:

1. **La instalación nunca se detiene por no tener NVIDIA.** El aviso sigue apareciendo
   (Pinokio sigue usando su variable documentada `gpu`), pero sólo informa y continúa.
2. **Se elige un entorno distinto según el hardware** (`launcher_profile.js`):

   | Hardware detectado | Entorno | Python | PyTorch |
   | --- | --- | --- | --- |
   | RTX 40/50 compatible con H3 Sol Engine | `app/env-sol` | 3.11 | CUDA 13 |
   | RTX 50 (Blackwell) | `app/env-rtx50` | 3.11 | CUDA 13 |
   | Otra NVIDIA (RTX 20/30/40, GTX, driver antiguo) | `app/env` | 3.10 | CUDA 12.8 |
   | **Sin NVIDIA** (AMD, Intel, GPU desconocida) | **`app/env-light`** | **3.10** | **CPU (`whl/cpu`)** |

3. **Un GPU vacío ya no se lee como «no hay NVIDIA».** Pinokio a veces no rellena `gpu`
   (inventario aún cargando o sonda fallida). En ese caso se instala el runtime NVIDIA
   estándar (que funciona sin GPU: sólo deja de generar) en lugar de dar el equipo por
   inservible.
4. **Driver antiguo en RTX 40 deja de bloquear la instalación.** El runtime CUDA 12.8
   (`app/env`) funciona con drivers anteriores al 580: ahora se avisa y se instala ese
   runtime en vez de abortar. Sólo RTX 50 (Blackwell) mantiene el requisito del driver 580,
   porque no tiene runtime alternativo.
5. **`app/env-light` está soportado en todo el ciclo**: `install.js`, `torch.js`,
   `start.js`, `start_classic.js`, `pinokio.js` (menú y «Install»), `update.js` y `reset.js`.

### Forzar el runtime CPU a mano

Si Pinokio informa de una GPU NVIDIA que no existe, o de un modelo que no reconoce, añade
en el **ENVIRONMENT** de la app (Pinokio → ENVIRONMENT):

```
MAESTRO_CPU_RUNTIME=1
```

y ejecuta **Reset** → **Install**. `start.js`, `update.js` y `pinokio.js` respetarán ese
entorno ligero (`app/env-light`) mientras el flag esté puesto.

## Qué funciona y qué no en un equipo sin NVIDIA

La aplicación **arranca y sirve la interfaz completa**; el motor de generación es lo único
que necesita CUDA. El propio Maestro lo dice en su auditoría de arranque y en la UI
(«No CUDA GPU detected in this process. Planning, media and assembly are available;
generation must run on a machine with an NVIDIA GPU»).

| Área | Sin NVIDIA (`app/env-light`) |
| --- | --- |
| Interfaz, Studio, Films, Editor, biblioteca, ajustes | Sí |
| Planeación/escritura con LLM local (llama.cpp / GGUF) | Sí (lento, depende de CPU/RAM) |
| Cola de producción, estados, reintentos, cancelación | Sí |
| Montaje y export con FFmpeg, audio, mezcla | Sí |
| Inpaint (SAM 3.1) | No: entorno conda con CUDA |
| Generación local (vídeo/imagen/audio con H3, LTX, Wan, Flux…) | **No** |
| Generación en un **nodo remoto con GPU** (peers H3) | Sí — ver [STUDIO-NODES.md](STUDIO-NODES.md) |
| Generación en **cómputo público gratis por internet** | Sí, automático — ver [PUBLIC-COMPUTE.md](PUBLIC-COMPUTE.md) |

Es decir: el equipo modesto **dirige**, y la generación ocurre donde hay GPU — en un
equipo tuyo con tarjeta, o en una **máquina pública gratuita que Maestro conecta solo**
si no hay ninguna otra. Este último caso no necesita que configures nada: busca Spaces
públicos en ejecución, los sondea y usa el que puede hacer la tarea; el resultado se
descarga en la carpeta del trabajo igual que cualquier otro render. Ojo a la diferencia:
un equipo con tarjeta ejecuta *tu* receta, la máquina pública ejecuta *su* modelo, así que
ahí el vídeo se parece a lo que pediste pero no es idéntico.

## Requisitos y expectativas reales

- **Disco:** la instalación Python (sin pesos de modelos) ocupa ~10–20 GB. Los pesos de los
  modelos que no vas a generar no se descargan si no se piden.
- **RAM:** 8 GB es el mínimo real para el backend completo (API + motor importado en el
  mismo proceso). Con 8 GB, evita generaciones locales y trabajos simultáneos.
- **FFmpeg** debe estar en el `PATH`: la app lo comprueba al arrancar y avisa si falta.
- Los paquetes de aceleración NVIDIA (`triton`, SageAttention, FlashAttention, `nunchaku`,
  kernels `lightx2v`) **no se instalan** en `app/env-light`: no hay GPU donde ejecutarlos.
- Se usa **el mismo `app/requirements.txt`** en todas las máquinas. Es una decisión
  deliberada: una lista distinta por hardware se desincronizaría del código y rompería
  imports que hoy funcionan.

## Instalación paso a paso

1. Pinokio → **Discover** → *Maestro* (o pegar la URL del repositorio).
2. **Install**. Verás un aviso informativo («No NVIDIA GPU reported…») y la instalación
   continuará: `app/env-light` (Python 3.10 + PyTorch CPU), dependencias de
   `app/requirements.txt`, kernel GGUF (no-op sin CUDA), componente seed-vc y build de la UI.
3. Al terminar, **Start**. El menú mostrará «Start — CPU / light runtime».
4. Abre la **Web UI**. Ya puedes generar: si tienes otro equipo con tarjeta, regístralo
   ([STUDIO-NODES.md](STUDIO-NODES.md)) para que ejecute tu receta exacta; si no, Maestro
   conecta por su cuenta una máquina pública gratuita ([PUBLIC-COMPUTE.md](PUBLIC-COMPUTE.md))
   y la usa. En **Studio → Compute & transfer → Public compute (internet)** ves a cuál se
   ha conectado.

## Diagnóstico

- **«CPU / light runtime not installed»** al pulsar Start → falta el paso Install (o se hizo
  Reset). Ejecuta **Install** y vuelve a pulsar Start.
- **La instalación vuelve a pedir NVIDIA y se detiene** → sólo ocurre en RTX 50 con driver
  anterior al 580 (`install.js` mantiene esa única parada a propósito). Actualiza el driver.
- **Quiero volver al runtime NVIDIA** (por ejemplo tras cambiar de equipo) → borra la línea
  `MAESTRO_CPU_RUNTIME` del ENVIRONMENT y ejecuta **Reset** → **Install**.
- **`python scripts/maestro_doctor.py`** imprime el estado de GPU/CUDA/ffmpeg, del bundle de
  la UI y del gate de acceso; es la primera comprobación recomendada.
- **`logs/api/install.js/latest`** y **`logs/api/start.js/latest`** contienen la salida
  completa de cada acción (ver README → *Finding logs*).

## Límites conocidos

- **AMD y macOS siguen sin soportar generación.** No hay ruta ROCm/TheRock cableada en el
  launcher: la Radeon R5 (gfx no soportado) no está en ninguna matriz de WanGP. El runtime
  ligero hace el repositorio *instalable y usable*, no *generable* en esa GPU. En macOS el
  mismo runtime se instala con las ruedas de PyTorch para macOS y tres dependencias
  sustituidas porque no existen para Mac (`decord` → `eva-decord`, `onnxruntime-gpu` →
  `onnxruntime`, `rembg[gpu]` → `rembg`); los detalles y el requisito de Apple Silicon están
  en [INSTALAR-EN-MAC.md](INSTALAR-EN-MAC.md).
- **No se instala Inpaint (SAM 3.1)** en este camino.
- Este documento describe el launcher, no el rendimiento: en un A9-9425 la planeación con
  LLM local es lenta y la generación local no existe. Para producir, usa un nodo con GPU.
