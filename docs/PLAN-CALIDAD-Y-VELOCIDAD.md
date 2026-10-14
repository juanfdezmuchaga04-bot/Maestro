# Plan: vídeos de máxima calidad en el menor tiempo posible

Hoja de ruta viva del proyecto. Objetivo: que desde una descripción y unas imágenes
Maestro produzca vídeos de calidad lo más cercana posible a plataformas comerciales como
Higgsfield, en el menor tiempo posible y gastando lo mínimo (gratis por defecto).

Convención de estado: ✅ hecho y probado · 🟡 hecho, sin probar en GPU real · ⬜ pendiente.
Todo tiempo marcado «≈» es una estimación sin medir.

---

## 0. Punto de partida honesto

- **Qué es Higgsfield:** una plataforma comercial que da acceso a modelos de vídeo
  comerciales de primer nivel. Su web cita, entre otros, modelos de Kling, Google Veo,
  Seedance y MiniMax (conviene verificar la lista actual). Esos modelos se ejecutan en
  centros de datos con GPU de última generación. Un clip tarda en ellos minutos, no horas.
- **Qué tenemos gratis:** dos NVIDIA T4 de 16 GB en Kaggle (≈30 h/semana) y tu portátil
  sin GPU NVIDIA. La T4 es de 2018: varias veces más lenta que una RTX 4090. Además no
  admite los formatos rápidos modernos (bf16/fp8).
- **Consecuencia:**
  - Con modelos abiertos y la T4 se puede acercar mucho la **calidad de imagen**
    (resolución, nitidez, coherencia), sobre todo con buena planificación, referencias
    y escalado.
  - La **velocidad** de un centro de datos no se alcanza gratis.
  - Para planos «estrella» (movimiento complejo, física, caras en primer plano), los
    modelos comerciales siguen por delante. La vía es **híbrida**: abiertos para la
    mayoría y comerciales solo donde marquen la diferencia, si tú lo eliges.
- **Licencias (vives en España, UE):** HunyuanVideo/FramePack y los pesos locales de
  MiniMax H3 **excluyen la UE** en su licencia, así que este plan no los usa en local.
  Wan 2.x (Apache-2.0), LTX-2.x (licencia de Lightricks, revisar condiciones
  comerciales), FLUX.2 (klein 4B abierto; 9B no comercial) y SeedVR2 (Apache-2.0) sí se
  pueden usar.

---

## 1. Los tres niveles de cómputo

| Nivel | Dónde | Coste | Calidad alcanzable | Tiempo 30 s @1080p |
|---|---|---|---|---|
| **A. Gratis** | 2× T4 Kaggle (nodos de Maestro) | 0 € | Alta (720p nativo → 1080p/4K escalado) | ≈1–3 h hoy; objetivo ≈30–60 min (§3) |
| **B. Bajo coste** | GPU alquilada por horas (RTX 4090 24 GB; H100 80 GB) con el mismo script de nodo | ≈0,15–0,35 $/h (4090); ≈2 $/h (H100) | Muy alta: modelos 14B a 720p+/1080p nativo, 4K rápido | ≈10–25 min (4090) |
| **C. Híbrido con APIs** | Planos clave por API comercial (opt-in, tu clave) | Por clip | Máxima en los planos elegidos | Minutos por plano |

Los precios del nivel B son orientativos (Vast.ai/RunPod) y cambian; se comprobarán
antes de usarlos.

---

## 2. Las piezas de la calidad (qué modelo para cada tarea)

| Etapa | Qué decide la calidad | Elección actual / candidata | Estado |
|---|---|---|---|
| **Guion y planificación** | Ritmo, planos, continuidad, prompts precisos | Director v2 con el LLM configurado. Candidato: LLM 7–14B cuantizado **en el nodo**, o un LLM de API si lo eliges | 🟡 Director / ⬜ LLM en nodo |
| **Imágenes clave por plano** | Composición, caras, estilo coherente | FLUX.2 klein (por defecto) + tus referencias (personaje/lugar). Candidato: Qwen-Image para texto en imagen | 🟡 |
| **Vídeo** | Movimiento, física, coherencia | LTX-2 destilado (defecto del Director). Candidatos a medir en T4: LTX-2.3 destilado GGUF, Wan 2.2 5B, Wan 2.2 14B (con descarga a RAM, lento) | ⬜ medir |
| **Continuidad entre planos** | Mismo personaje y lugar | Referencias, KFI, multi-clip en un solo trabajo | 🟡 |
| **Resolución** | Nitidez final | FlashVSR ×1,5/×2 (1080p/1440p), SeedVR2 (4K) | 🟡 |
| **Fluidez** | 24 → 48/60 fps | RIFE ×2 | 🟡 |
| **Audio** | Música, voz, efectos | Música y voz de Maestro (ya verificadas en local); efectos: MMAudio | ✅ voz/texto · ⬜ integrar en Auto |
| **Montaje y color** | Acabado de cine | Editor de Maestro, exportación H.265/AV1 | ✅ |
| **Control de calidad** | Que nada salga roto | ⬜ Revisión automática por plano (§3, fase 5) | ⬜ |

---

## 3. Hoja de ruta por fases

### Fase 1 — Medir (lo primero; sin datos no se optimiza)
- ⬜ Prueba real 10 s @720p en Kaggle con dos nodos (tu primera producción).
- ⬜ Registrar por fase: instalación, descarga, planificación, imágenes, vídeo, escalado,
  VRAM/RAM máximas. El log de la producción ya guarda la hora de cada paso.
- ⬜ Comparar en la T4 3–4 modelos de vídeo con el mismo guion (calidad vs tiempo) y
  fijar la **receta T4** por defecto.
- **Criterio de salida:** tabla de tiempos reales y receta T4 elegida.

### Fase 2 — Quitar tiempos muertos (≈-20–50 min por sesión)
- ⬜ **Caché persistente en Kaggle:** entorno y modelos en un *Dataset* privado montado
  como *Input*; el nodo los usa directamente. Comprobar límites de tamaño.
- ⬜ **LLM de planificación en el nodo** (de ≈2–15 min en CPU a ≈1–2 min).
- ⬜ Arranque del segundo nodo en paralelo con el primero (hoy es en serie).

### Fase 3 — Usar las dos GPU en una misma producción (≈-40–50 % del render)
- ⬜ El Director agrupa planos por **escena**. Dentro de una escena se mantiene el
  multi-clip continuo; escenas distintas se renderizan **en paralelo** en nodos
  distintos y se unen al final.
- ⬜ Imágenes clave repartidas entre nodos.
- ⬜ Escalado FlashVSR/SeedVR2 del clip de una escena mientras la otra renderiza.

### Fase 4 — Calidad por encima de lo básico
- ⬜ **Preajustes de calidad** en Auto: *Rápido* (720p, pocos pasos), *Equilibrado*
  (720p + FlashVSR 1080p), *Máximo* (más pasos, SeedVR2, RIFE).
- ⬜ Hoja de personajes automática desde tus imágenes (varias vistas) para mejorar la
  identidad entre planos.
- ⬜ Efectos de sonido por plano (MMAudio) y mezcla automática con la música.
- ⬜ Corrección de color uniforme entre planos (LUT común al exportar).

### Fase 5 — Control de calidad automático
- ⬜ Tras cada plano, un modelo de visión puntúa: deformaciones, caras, parpadeos,
  coherencia con el guion. Si no pasa el umbral, se regenera solo ese plano con otra
  semilla (máximo N intentos) y se informa.

### Fase 6 — Niveles B y C (opcionales, solo si los eliges)
- ⬜ Script de nodo para GPU alquilada (mismo código `maestro-node:`), con aviso de coste
  por hora antes de arrancar.
- ⬜ Planos «estrella» por API comercial: Auto marca los planos difíciles y te pide
  confirmación y presupuesto antes de enviar nada.

---

## 4. Objetivos medibles

| Meta | Hoy (≈) | Objetivo nivel A | Objetivo nivel B |
|---|---|---|---|
| 30 s @1080p, primera de la sesión | 1,5–3 h | ≤ 60 min | ≤ 25 min |
| 30 s @1080p, siguientes | 1–2 h | ≤ 40 min | ≤ 15 min |
| Planos que hay que rehacer a mano | sin medir | ≤ 10 % | ≤ 5 % |
| 4K (SeedVR2) añadido a 30 s | sin medir | ≤ +45 min | ≤ +10 min |

---

## 5. Ya hecho (base de todo lo anterior)

- 🟡 Producción automática: descripción + imágenes → Director → nodos → FlashVSR/SeedVR2
  → RIFE → vídeo final ([detalles](PRODUCCION-AUTOMATICA.md)).
- 🟡 Dos nodos por sesión de Kaggle (uno por T4), reparto al menos ocupado.
- 🟡 SeedVR2 para 4K con ajustes por VRAM y audio original.
- ✅ Texto y voz en local; biblioteca de modelos y descargas en cualquier CPU.

## 6. Próximo paso

Ejecutar la **Fase 1** con tu primera prueba en Kaggle. Con esos tiempos reales se
decide la receta T4 y se empiezan las fases 2 y 3, que son las que más tiempo ahorran
sin coste.
