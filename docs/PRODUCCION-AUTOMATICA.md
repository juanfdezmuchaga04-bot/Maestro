# Producción automática: de una descripción a un vídeo terminado

**Dónde:** pestaña **Auto → Auto production**. La API es `POST /api/v1/auto-production`.

Escribes qué quieres contar, añades imágenes si quieres y eliges duración, resolución y formato. Maestro hace el resto sin pedirte revisiones intermedias:

```
descripción + imágenes
   │
   ├─ 1. Comprobación de GPU ─ GPU NVIDIA local o nodos conectados (Kaggle T4 ×1/×2)
   ├─ 2. Receta ─ tamaño nativo, FlashVSR, RIFE y, si pides 4K, SeedVR2
   ├─ 3. Director (short_film_story, auto_mode)
   │      planifica escenas y planos con el LLM → genera imágenes (con tus referencias)
   │      → renderiza los clips como un único trabajo multi-clip (continuidad) → los une
   │      (sin GPU local, cada render va al nodo menos ocupado)
   ├─ 4. Acabado 4K ─ SeedVR2 en una GPU (aquí o en un nodo) con el audio original
   └─ 5. Resultado ─ reproductor en la propia pestaña + fichero en la carpeta de salida
```

Cuando termina, el proyecto también aparece en el **Director**. Desde allí puedes rehacer cualquier plano.

## Qué hace cada opción

### Resolución

| Pides | Cómo se consigue |
|---|---|
| 720p | Render nativo a 1280×720. |
| 1080p | Render 720p + **FlashVSR ×1.5** dentro del trabajo (súper-resolución por difusión). |
| 1440p | Render 720p + **FlashVSR ×2**. |
| 4K | Render 720p + FlashVSR ×1.5 (1080p) + **SeedVR2** a 2160p sobre el vídeo final. |

- En una GPU de menos de 12 GB, FlashVSR se sustituye por Lanczos y el registro lo dice.
- 4K necesita una GPU de al menos 8 GB para SeedVR2. Si no hay ninguna, se rechaza antes de empezar y el motivo aparece en pantalla.

### Otras opciones

- **Movimiento suave:** activa RIFE ×2, que duplica los fotogramas por interpolación.
- **Imágenes:**
  - La primera es la *referencia principal*: estilo e identidad.
  - Las demás son *personaje* o *localización*; lo eliges en cada miniatura.
  - Llegan al Director como `reference_image_path`, `character_ref_paths` y `location_ref_paths`.
- **Duración:** de 5 a 600 s. El Director la reparte en escenas y planos.

## Los dos nodos de Kaggle (uno por T4)

Con el acelerador **GPU T4 ×2**, el notebook arranca **dos Maestro independientes**:

- Cada uno usa una GPU (`CUDA_VISIBLE_DEVICES`), su propio puerto (7860 y 7861), su propio token y su propio túnel.
- El segundo se ejecuta desde un `git worktree`, así que no comparten ajustes ni cola.
- La carpeta de modelos es compartida, así que los pesos se descargan una sola vez.
- Se imprime **un único código** `maestro-node:` (versión 2) que conecta ambos como `free-gpu` y `free-gpu-2`.
- Para usar un solo nodo: `--nodes 1`.

### Reparto de trabajo

- Cada render delegado va al nodo con **menos trabajos en curso** (`choose_target` con `peer_load`).
- El pase SeedVR2 también va al nodo menos ocupado.
- Dentro de **una sola** producción, el Director genera las imágenes y los clips en secuencia: la continuidad entre clips exige un único trabajo de vídeo.
- Por eso el segundo nodo sirve sobre todo para:
  - dos producciones a la vez;
  - el acabado 4K de una mientras el otro renderiza la siguiente;
  - trabajos de Studio en paralelo.

**Límite real:** los dos nodos comparten unos 29 GB de RAM. Si los dos cargan modelos grandes con descarga a RAM (14B, BlockSwap) a la vez, uno puede quedarse sin memoria. En ese caso, vuelve a lanzar con `--nodes 1`.

## SeedVR2 (4K)

- **Herramienta:**
  - CLI independiente de [ComfyUI-SeedVR2_VideoUpscaler](https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler) (Apache-2.0), fijada al commit `4490bd1f`.
  - Se descarga la primera vez en `app/tools/seedvr2`, que está ignorado por git.
  - Se le añaden los paquetes que falten: omegaconf, peft, rotary_embedding_torch y gguf.
  - Los pesos se bajan de Hugging Face con comprobación sha256 a `app/ckpts/seedvr2`.
- **Ajustes según la memoria de la GPU** (siguen las recetas de poca VRAM del proyecto original):

  | VRAM | Modelo | BlockSwap | Lote | Teselas VAE |
  |---|---|---|---|---|
  | 8–12 GB | 3B Q4_K_M | 32 bloques | 5 | 512 |
  | ≥15 GB (T4) | 3B Q8_0 | 16 bloques | 9 | 768 |
  | ≥24 GB | 7B Q4_K_M | 20 bloques | 21 | 1024 |

- **Funcionamiento:**
  - Procesa por fragmentos (`--chunk_size`), así que los vídeos largos no llenan la RAM.
  - Corrige el color en LAB.
  - El progreso real sale de sus líneas «Upscaling batch i/N».
  - Cancelar detiene el proceso.
  - SeedVR2 escribe vídeo sin sonido; Maestro vuelve a poner el audio original con ffmpeg.
- **En los nodos:** cada Maestro registra el motor de cola `maestro-finish`. Desde el portátil, el vídeo terminado se envía al nodo como cualquier otro medio, se escala allí y el resultado vuelve.

## Qué está probado y qué no

**Probado aquí (pruebas automáticas, sin GPU):**
- Validación de la petición y la receta por resolución y VRAM.
- Parámetros que recibe el Director.
- Selección del vídeo final (`_multiclip`).
- El orquestador completo con dependencias simuladas: éxito, fallo con su motivo real, cancelación y rechazo sin GPU.
- Códigos v2 de dos nodos.
- Reparto por carga.
- Envío de `per_clip_keyframes` anidados.
- Construcción del comando SeedVR2 y lectura de su progreso.
- Que un trabajo de acabado conserve su motor `maestro-finish`.
- `tsc`, `eslint` y `npm run build` de la interfaz.

**Preparado pero sin ejecutar todavía:**
- Una producción real de principio a fin: no hay GPU NVIDIA en este entorno.
- SeedVR2 en una T4 (memoria y tiempos reales).
- Los dos nodos en Kaggle a la vez (uso de RAM).
- Que el modelo de vídeo por defecto del Director quepa en 16 GB.

  Si el Director rechaza el modelo para ese hardware, la producción falla con el mensaje del Director. Puedes indicar otro con `video_model`.

**Limitaciones conocidas:**
- Un trabajo multi-clip delegado devuelve el vídeo unido, pero no los ficheros de cada clip por separado.
- Las referencias `per_clip_minimax_h3_references` no se envían al nodo.
- Si Maestro se reinicia a mitad de una producción, esta se marca como fallida («Maestro restarted…»). El pipeline del Director puede reanudarse desde el Director.

## API

```
GET  /api/v1/auto-production                 → {productions, compute}
POST /api/v1/auto-production                 → inicia
     {description, duration_seconds, resolution: 720p|1080p|1440p|4k,
      aspect_ratio: 16:9|9:16|1:1, smooth_motion, style,
      images: [{path, role: reference|character|location, label}],
      video_model?, image_model?, workspace?}
GET  /api/v1/auto-production/{id}            → estado (stage, progress, log, result)
POST /api/v1/auto-production/{id}/cancel
GET  /api/v1/auto-production/{id}/video      → el vídeo final
```

Las imágenes se suben antes con `POST /api/v1/upload`, y se usa el `path` que devuelve.

> Hoja de ruta para más calidad y menos tiempo: [Plan de calidad y velocidad](PLAN-CALIDAD-Y-VELOCIDAD.md).
