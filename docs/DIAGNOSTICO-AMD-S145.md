# Diagnóstico AMD / Lenovo IdeaPad S145 y alternativas locales sin NVIDIA

**Estado: preparado, no validado en el equipo.** Nada de este documento afirma que la
generación funcione en un S145; el objetivo es medir el equipo exacto y probar cada
backend allí antes de decir nada.

## 1. Identificar el equipo exacto

“Lenovo S145” no es un solo equipo: hay variantes con GPUs muy distintas. El script
las distingue por el **ID PCI** de la GPU y el modelo de máquina, sin suponer nada.

```bash
# En el propio S145, desde la carpeta de Maestro (no instala ni modifica nada):
python scripts/diagnose_gpu.py --json s145-report.json
# Si ya hay torch / torch-directml / onnxruntime instalados, prueba un cálculo real:
python scripts/diagnose_gpu.py --probe --json s145-report.json
```

En Windows usa `Get-CimInstance` (Win32_ComputerSystem, Win32_OperatingSystem,
Win32_Processor, Win32_VideoController); en Linux `/sys/class/dmi`, `lspci -nn`,
`rocminfo`, `clinfo` y `vulkaninfo` si existen. Envía `s145-report.json` para
cerrar el diagnóstico.

| Variante S145 | GPU esperada | ID PCI | Arquitectura |
|---|---|---|---|
| S145-14API / 15API | Radeon Vega 3/8/10 (Ryzen 3200U/3500U/3700U, Athlon 300U) | `1002:15d8` | GCN5 (familia gfx902), memoria compartida |
| S145-14AST / 15AST | Radeon R4/R5 (A6-9225 / A9-9425) | `1002:98e4` | GCN3 (gfx810), memoria compartida |
| S145-14IWL / 15IWL | Intel UHD 620 (algunas con NVIDIA MX opcional) | `8086:3ea0` | Gen9.5 |
| S145-14IGM / 15IGM | Intel UHD 600/605 | `8086:3185` / `3184` | Gen9.5 |
| S145-14IIL / 15IIL | Intel UHD / Iris Plus (Ice Lake) | `8086:8a56` / `8a52` | Gen11 |

Un ID que no esté en la tabla se informa como desconocido, no se adivina.

## 2. Backends: qué se espera y qué hay que probar

| Backend | Expectativa para las variantes AMD del S145 | Estado que reporta el script |
|---|---|---|
| **ROCm** (Linux) | AMD no incluye las APU GCN (Raven/Picasso, Stoney) en su lista de soporte. Forzar `HSA_OVERRIDE_GFX_VERSION` no está soportado | `unsupported_by_vendor` (o `not_applicable` en Windows) |
| **DirectML** (Windows) | Requiere GPU y driver DirectX 12; Vega y GCN3 lo exponen. Rendimiento y memoria compartida (2 GB típicos de UMA) sin medir | `candidate_untested` → `probe_passed/failed` solo con `--probe` y `torch-directml` instalado |
| **Vulkan** | Los drivers AMD exponen Vulkan; útil para `stable-diffusion.cpp` / `llama.cpp` Vulkan | `candidate_untested` si `vulkaninfo` ve el dispositivo |
| **CPU** | Siempre disponible; lento para difusión | `probe_passed` solo si un cálculo de torch se ejecuta |

## 3. Alternativas locales evaluadas (CPU y AMD)

Los motores actuales (Wan2GP/CUDA, ComfyUI + Wan 2.2) **no se han tocado**.

| Alternativa | Hardware | Valoración para un S145 | Integrada |
|---|---|---|---|
| ComfyUI con `--directml` (Windows) | GPU DX12 (AMD/Intel) | La vía local más realista para **imagen** en AMD Windows con modelos pequeños (SD 1.5 / SD-Turbo 512²). Maestro ya tiene adaptador ComfyUI (`comfyui_adapter.py`), pero su plantilla actual apunta a Wan 2.2 y su detección exige NVIDIA | No (pendiente de medir en el equipo) |
| ComfyUI con `--cpu` | Cualquier CPU | Funciona en principio; minutos por imagen a 512² en una CPU de 15 W | No |
| stable-diffusion.cpp (Vulkan o CPU) | AMD/Intel vía Vulkan | Binario ligero, GGUF cuantizado; candidato para imagen. Sin probar | No |
| ONNX Runtime + DirectML | GPU DX12 | Requiere modelos exportados a ONNX; más trabajo de integración | No |
| Vídeo local (Wan 2.2 5B, LTX) | — | **No viable**: requieren ≥ 8 GB de VRAM dedicada; una APU con memoria compartida no los ejecuta en tiempos útiles | No |

Conclusión provisional: en un S145 el **vídeo** debe ir a otra máquina con GPU, al
cómputo público gratuito o a un proveedor externo opt-in
([PROVEEDORES-EXTERNOS.md](PROVEEDORES-EXTERNOS.md)); la **imagen** local podría ser
posible por DirectML o Vulkan con modelos pequeños, **pendiente de medir**.

## 4. Siguiente paso para validar

1. Ejecutar `scripts/diagnose_gpu.py --json` en el S145 y compartir el informe.
2. Según la variante: en Windows con Vega/R5, crear un entorno aparte con
   `torch-directml` y volver a ejecutar con `--probe`.
3. Solo si `probe_passed`, medir una imagen 512² (tiempo, memoria) con ComfyUI
   `--directml` o stable-diffusion.cpp Vulkan y documentar el resultado aquí.

## Qué se ha probado

- `tests/test_diagnose_gpu.py`: identificación con salidas registradas de Windows
  (S145-15API con Vega 8) y Linux (Stoney R5, GPU desconocida); el diagnóstico nunca
  marca un backend como funcional sin `--probe`.
- El script se ejecutó en el contenedor de desarrollo (Linux sin GPU): informa “GPU
  no identificada” y lo dice. **No se ha ejecutado en ningún S145.**
