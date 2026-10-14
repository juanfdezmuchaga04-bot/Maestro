# 1080p, 4K y vídeos de más de 10–15 segundos: todas las vías

Investigación del **2026-10-14**. Cada dato lleva su fuente al final; lo que no se ha
medido en un equipo real se marca como estimación. Hay tres palancas independientes:
**resolución**, **duración** y **dónde se calcula**.

> Lo recomendado aquí (Director + nodos + FlashVSR/SeedVR2 + RIFE) ya está integrado y es
> automático: ver [Producción automática](PRODUCCION-AUTOMATICA.md).

## 1. Lo que Maestro ya tiene (no hay que añadir nada)

| Capacidad | Qué hace | Requisito |
|---|---|---|
| **Ventanas deslizantes** + planificación larga | Vídeos de cualquier duración encadenando ventanas solapadas; planificación hasta **60 minutos** | El tiempo de cálculo crece con cada ventana |
| **KFI (Frames Injection)**, **SVI 2 Pro** (Wan 2.2), **SkyReels V2 Diffusion Forcing** | Continuidad de personajes y escenas en vídeos largos | SkyReels 14B pide mucha VRAM (≈ 51 GB a 540p); existe la versión 1.3B |
| **LongCat-Video / Avatar** | Continuación de vídeo durante minutos | 13,6B, GPU grande |
| **Director** + **Editor** | Un vídeo musical o corto de minutos montado a partir de muchos planos de 5–10 s | Es como se trabaja en producción real |
| **FlashVSR** (superresolución por difusión) | Escalado de vídeo, incluido con Maestro, con modo *tiny* ligero | GPU NVIDIA |
| **Upsampler espacial y temporal de LTX-2.3** | ×2 en resolución y ×2 en fotogramas dentro del propio pipeline | GPU NVIDIA |
| **Upsampler 1080p de HunyuanVideo 1.5** | Pasa de 720p a 1080p | GPU NVIDIA; **la licencia de Hunyuan no se aplica en la UE** |
| **RIFE 4.26** | Interpolación ×2 / ×3 / ×4 de fotogramas (24 → 48–96 fps, cámara lenta) | Funciona también fuera de RTX |
| **DLSS 5 / Frame Generation** | Escalado neuronal y generación de fotogramas | Solo Windows con RTX (no sirve en Kaggle, que es Linux) |
| **H3 1080p experimental y regeneración 2K** | Resolución alta con MiniMax H3 | Mucha VRAM y RAM; 2K solo por API; **licencia que excluye la UE** |
| **Exportación del Editor** | H.264 / H.265 / AV1 a resolución de entrega, incluido 4K | Funciona en CPU (más lento) |

## 2. Resolución: cómo llegar a 1080p y 4K

| Vía | Resultado | Coste | Notas |
|---|---|---|---|
| **Generar a 720p y escalar** (FlashVSR ×2 en el nodo) | 1080p–1440p | Gratis en el nodo Kaggle | **La vía más práctica.** Pendiente de medir el tiempo en la T4 |
| **LTX-2.3 nativo a 1080p** | 1080p nativo, hasta 20 s en la API oficial | Gratis en local / nodo | Una guía no oficial indica 12 GB de VRAM para 1080p: plausible en la T4 de 16 GB, sin medir |
| **MoviiGen 1080p** (ajuste de Wan 14B) | 1080p nativo | Gratis en local | Necesita una GPU grande; en la T4, solo con mucha descarga de memoria |
| **SeedVR2** (no integrado) | Escalado a 4K en un solo paso | Gratis (código abierto) | Versiones 3B/7B; con GGUF y *BlockSwap* se reportan 8–16 GB; en 16 GB hay informes de fallos. **Candidato a integrar** |
| **Real-ESRGAN / waifu2x con ncnn-Vulkan** (Video2X) | ×2–×4, incluido 4K | Gratis | **Funciona en tu Lenovo** (Vulkan en AMD, Intel o NVIDIA, sin CUDA). Muy lento: horas por minuto de vídeo. Calidad inferior a SeedVR2 o FlashVSR |
| **API de LTX** | 1080p–4K nativos (4K hasta 10 s) | De pago, mínimo 5 $ | La única vía de 4K nativo encontrada |
| **MiniMax H3 por API** | 2K | De pago | Licencia local excluida en la UE; la API es global |

**El 4K es casi siempre escalado.** No existe un modelo abierto que genere 4K
nativo de forma práctica en local. El 4K escalado tiene buena calidad (SeedVR2 y
FlashVSR añaden detalle), pero no equivale a un 4K nativo.

## 3. Duración: más de 10–15 segundos

| Vía | Duración | Notas |
|---|---|---|
| **Ventanas deslizantes** de Maestro | Ilimitada (planificación hasta 60 min) | El coste crece de forma lineal; la coherencia se apoya en KFI, SVI y las referencias |
| **LTX-2.3** | Hasta ~20 s por generación (1080p en la API) | Las ventanas de Maestro encadenan más |
| **SVI 2 Pro / SkyReels V2 DF** | De decenas de segundos a minutos | Modelos 14B; pesados para la T4 |
| **LongCat-Video** | Minutos (los autores dicen hasta ~15 min) | Cifra de los propios autores, sin verificar |
| **FramePack** (no integrado) | Hasta un minuto con ~6 GB de VRAM | Está **basado en HunyuanVideo**: la misma exclusión de la UE |
| **Director + Editor** | Minutos (vídeo musical o corto) | Muchos planos cortos montados: la vía más robusta y la que mejor calidad da |
| **RIFE** | Más fluidez o cámara lenta | No inventa contenido nuevo; alarga el tiempo de los planos lentos |

## 4. Dónde se calcula (de gratis a pago)

| Recurso | GPU / VRAM | Cuánto | Uso realista |
|---|---|---|---|
| **Tu Lenovo** | Gráfica integrada AMD, sin CUDA | Ilimitado | Planificar, editar, montar, exportar 1080p/4K; escalado y RIFE por Vulkan (lento) |
| **Nodo Kaggle** (ya integrado) | T4 de 16 GB (×2) | ~30 h/semana gratis | 720p + escalado a 1080p; vídeos largos por ventanas (cada ventana consume cuota). **Idea:** un Maestro por cada T4 = dos nodos en paralelo |
| **Lightning AI** | T4 (otras con créditos) | ~80 h/mes (según terceros) | Igual que Kaggle |
| **Hugging Face ZeroGPU** | **H200 de 70 GB** por llamada | 2 min/día anónimo, 3,5 con cuenta gratis, 25 PRO (9 $/mes) | Mucha potencia, poquísimo tiempo; ya integrado como cómputo público |
| **AMD Developer Cloud** | MI300X de 192 GB | ~100 $ de créditos (~50 h) para desarrolladores aceptados; pide método de pago; caducan | ROCm, no CUDA: el motor de Maestro no está validado ahí |
| **Becas de investigación** (Nebius, Modal académico, Azure for Students 100 $) | Variable | Por solicitud | Solo si eres estudiante o investigador |
| **Alquiler barato** (Vast.ai, RunPod) | RTX 4090 de 24 GB | ~0,14–0,34 $/hora | **La vía más barata hacia el 1080p nativo** y el escalado 4K rápido: unas 10 horas cuestan ~2–4 $. Mismo mecanismo de nodo (pendiente de adaptar el script) |
| **H100 de alquiler** | 80 GB | ~1,7–2,7 $/hora | Todo: Wan 14B a 720p+, SkyReels, LongCat, SeedVR2 a 4K |

## 5. Flujo recomendado (gratis) para 1080p y vídeos largos

1. **Director** planifica la pieza (por ejemplo, 90 s = 18 planos de 5 s).
2. Cada plano se renderiza a **720p** en el **nodo Kaggle** (Wan 2.2 5B o LTX-2.3
   destilado GGUF).
3. **FlashVSR ×2** en el mismo nodo → 1080p–1440p. **RIFE** si quieres 48–60 fps.
4. El **Editor** monta, mezcla el audio y exporta a 1080p (o a 4K escalado).
5. **4K:** escalar el montaje final con SeedVR2 en una GPU (cuando esté integrado) o con
   Real-ESRGAN-Vulkan en el Lenovo durante la noche.

## 6. Lo que se puede añadir a Maestro, por impacto

1. **Dos nodos en Kaggle** (un Maestro por cada T4): duplica los planos por hora, gratis.
2. **SeedVR2** como escalador de 4K (GGUF y *BlockSwap*) en el nodo.
3. **Acabado por Vulkan en el Lenovo**: Real-ESRGAN y RIFE ncnn para escalar e
   interpolar sin CUDA.
4. **Nodo en GPU alquilada** (Vast.ai / RunPod) con el mismo script: 1080p nativo por
   pocos euros cuando haga falta.
5. **Preajuste «1080p largo»** en Director: 720p + FlashVSR + RIFE + montaje.

## Fuentes

- ZeroGPU (H200 de 70 GB, cuotas): <https://huggingface.co/docs/hub/spaces-zerogpu>
- Límites de la API de LTX-2.3: <https://help.ltx.io/hc/en-us/articles/37060851201682-Supported-resolutions-frame-rates-and-durations-for-the-API>
- LTX-2.3 en local (12 GB para 1080p, no oficial): <https://damodev.csdn.net/6a433c6310ee7a33f2846819.html>
- SeedVR2: <https://www.ainvfx.com/blog/seedvr2-v2-5-the-complete-redesign-that-makes-7b-models-run-on-8gb-gpus/> · <https://weirdwonderfulai.art/resources/seedvr2-upscaler-for-video-and-images/> · <https://huggingface.co/numz/SeedVR2_comfyUI/blob/refs%2Fpr%2F4/README.md>
- FlashVSR: <https://arxiv.org/html/2510.12747v1> · <https://github.com/LujiaJin/FlashVSR-Pro>
- SkyReels V2: <https://comfyui-wiki.com/en/models/skyreels> · FramePack: <https://decrypt.co/316027/open-source-video-generators-full-length-movies> · LongCat: <https://app.cinevva.com/guides/long-reference-video-models>
- Real-ESRGAN ncnn-Vulkan: <https://aireiter.com/blog/real-esrgan-guide-download-gui>
- Multi-GPU (xDiT/USP): <https://diffsynth-studio-doc.readthedocs.io/en/latest/Pipeline_Usage/Accelerated_Inference.html>
- AMD Developer Cloud: <https://www.amd.com/en/developer/resources/technical-articles/2026/openclaw-with-vllm-running-for-free-on-amd-developer-cloud-.html> · <https://www.amd.com/en/developer/resources/cloud-access.html>
- Becas y créditos: <https://nebius.com/nebius-research-grants> · <https://gpuperhour.com/blog/free-cloud-gpus-and-credits>
- Precios de GPU: <https://getdeploying.com/guides/cheapest-gpu-cloud> · <https://www.spheron.network/blog/h100-price-per-hour-2026/>
