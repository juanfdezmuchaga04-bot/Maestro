# Opciones de cómputo remoto sin ningún pago

Investigación de qué se puede usar **sin método de pago, sin tarjeta y sin
crédito de prueba** para renderizar desde una computadora que no puede generar
localmente. Sirve para decidir qué vale la pena conectar a Maestro y qué no,
sin tocar la regla del proyecto: **nada de la ruta de render exige un pago**.

Fecha de la verificación: 2026-10-13. Cada afirmación indica la página de la que
se leyó; lo que no se pudo verificar se dice como tal en vez de darlo por bueno.

## Resumen (lo que importa)

1. **Para video no existe hoy una API comercial gratuita de verdad.** La única
   vía gratuita y recurrente verificada para *video* es un Space Gradio de
   Hugging Face sobre hardware **ZeroGPU** — exactamente el tier que Maestro ya
   usa (`app/services/public_cloud.py`). Cuota de una cuenta gratuita:
   **5 minutos de GPU por día** (2 sin cuenta, 40 con PRO), reinicio 24 h después
   del primer uso.
2. **Para imagen sí hay alternativas sin tarjeta.** La más útil es
   **Cloudflare Workers AI**: cuota gratuita recurrente de **10 000 Neurons/día**
   (FLUX schnell sirve `text-to-image`), sin tarjeta. Es solo imagen: **no hay
   modelo de video** en su catálogo.
3. **Casi todo lo demás es pago o crédito que después exige tarjeta.** Modal
   (aunque dé $30/mes) exige método de pago; Together pide una compra mínima de
   $15; Replicate, fal, Segmind, DeepInfra y Novita cobran por consumo; el trial
   de Google Cloud pide tarjeta y **no permite GPU** sin subir a facturación.
4. **Colab gratuito queda descartado por sus propios términos**, no por el
   hardware: su FAQ prohíbe “file hosting, media serving, or other web service
   offerings not related to interactive compute” y el “remote control … remote
   desktops” / “bypassing the notebook UI”.
5. La mejor opción “gratis” en calidad sigue siendo **una computadora propia con
   GPU**, alcanzable por Tailscale (ya integrado), porque ejecuta el pipeline de
   Maestro exactamente en vez de aproximarlo.

## Cómputo: hosts y notebooks

| Opción | ¿Gratis sin método de pago? | GPU y cuota | Almacenamiento persistente | Red / HTTPS | Veredicto |
|---|---|---|---|---|---|
| **Hugging Face Spaces ZeroGPU** | Sí (cuenta personal en regla; hasta 2 Spaces ZeroGPU) | Media RTX Pro 6000 Blackwell, **48 GB VRAM** (`xlarge` consume doble); **5 min GPU/día** gratis (2 anónimo, 40 PRO); llama por defecto 60 s | Disco del Space **50 GB pero efímero**; para persistir hay Storage Buckets y **no se verificó una cuota gratuita** | Sí: el Space es una app web con URL propia; salida a 80/443/8080 | **La mejor VRAM y la única vía gratuita de video**, pero la cuota diaria y el caché efímero la vuelven un motor “de vez en cuando”, no un worker estable. **Ya conectado en Maestro.** |
| **Kaggle Notebooks** | Notebook $0, sin tarjeta indicada | P100 o **T4 ×2 (16 GB cada una, no 32 GB)**; se reporta ~30 h GPU/semana y sesiones de 12 h, **no publicado en la página oficial leída** | Working disk ~20 GB; guardar outputs no es un volumen persistente; 40 GB de pesos no caben | Internet de salida sí; un túnel podría exponer el servicio, pero **no hay documentación oficial que lo apruebe como API entrante** | Sirve para experimentar arrancando la sesión a mano; **no** como backend estable. Los Términos/AUP no se pudieron leer completos: se deja como **no verificado**, no como prohibido. |
| **Google Colab free** | Sí (los recursos no están garantizados) | GPU dinámica no publicada; sesión máx. 12 h | VM borrada al vencer; Drive como almacenamiento lento/no apto a escala | Salida sí, pero da igual | **Descartado por TOS** (media serving / remote control / saltarse la UI del notebook). No es una opción válida para un backend. |
| **Amazon SageMaker Studio Lab** | Era gratis, pero **AWS ya no acepta clientes nuevos** | GPU hasta 4 h por sesión y 4 h por 24 h; AWS no publica el modelo/VRAM | 15 GB de proyecto persistente (menos que los pesos) | Documentado como JupyterLab/GitHub/S3; sin servicio entrante oficial | Inservible para Maestro: sin altas nuevas y con 4 h/día y 15 GB. |
| **Modal (Starter)** | **No**: $30/mes en créditos, pero *“you must have a payment method on file”* y GPU requiere método de pago válido | T4/L4/A10/L40S/A100/H100…; la cuota es en dólares, no en horas GPU | Volumes persistentes (cuota gratuita no publicada) | Sí: Web Functions HTTPS/FastAPI; pero bajo demanda, con arranques en frío | Técnicamente el mejor encaje de los pagos; **falla el requisito de no pagar**. |
| **Lightning AI** | “Sin tarjeta para empezar” y 5 créditos al registrarse = **créditos iniciales**, no una cuota recurrente verificada | No se pudo verificar GPU/VRAM/horas sin tarjeta | No verificado para 18–40 GB | Se describe publicar apps; página no legible | Posible para una prueba corta; **no verificado** como worker gratuito. |
| **Paperspace / DigitalOcean Gradient** | Hay plan Free $0, pero no se verificó si el alta pide tarjeta | Free = **M4000 8 GB VRAM** (insuficiente), auto-shutdown 12 h | 5 GB (insuficiente) | Uso descrito como notebooks en workspaces privados | Descartado por VRAM/almacenamiento y alcance del plan gratuito (“only be used for Notebooks”). |
| **Google Cloud free trial** | **No**: el trial pide tarjeta | **No se pueden añadir GPUs** a instancias de la cuenta no facturable | Discos aparte (pagados) | — | Inservible por pago; el trial no da GPU. |
| **Oracle Cloud Always Free** | Exige **verificación de pago/tarjeta** al alta | **Sin GPU**: 2 OCPU + 12 GB de RAM (memoria de sistema) | Hasta 200 GB Always Free | IP pública posible | Sirve para un servicio CPU pequeño, no para este pipeline. |

## APIs de inferencia (imagen y video)

| Servicio | Tareas que sirve | ¿Gratis? | Límites verificados | Notas |
|---|---|---|---|---|
| **Hugging Face ZeroGPU Spaces** | video e imagen, según el Space | **Sí, recurrente** | 5 min GPU/día (cuenta gratis) | Es un Space Gradio, no la API de Inference Providers. **Única vía gratuita de video verificada.** |
| **Cloudflare Workers AI** | `text-to-image` (`@cf/black-forest-labs/flux-1-schnell`, FLUX.2, DreamShaper); **sin video** | **Sí, sin tarjeta** | **10 000 Neurons/día**, reset 00:00 UTC; después requiere plan pago ($0.011/1 000) | `POST /client/v4/accounts/{id}/ai/run/{model}`, token propio. Imagen en base64, llamada síncrona. |
| **Hugging Face Inference Providers** | imagen y video (p. ej. Wan, LTX) | **No para cuentas gratis**: sin créditos; PRO recibe $2/mes (plan pago) | — | Usa el token del usuario; no un token embebido en la app. |
| **Pollinations.ai** | imagen y video (`gen.pollinations.ai/video/{prompt}`) | Condicional: el repo describe Pollen de pago por uso y *free Pollen* por misiones | **Sin cuota gratuita verificada** | Advertencia explícita: **no exponer claves `sk_`** en el cliente. Tratar como experimental. |
| **Google AI Studio / Gemini** | imagen (Nano Banana) y video (Veo) | **No verificado** que imagen/Veo entren en el tier gratis | RPM/TPM/RPD por proyecto | Veo es operación asíncrona que se sondea. El material del tier gratis **puede usarse para mejorar productos de Google**. |
| **Together AI** | imagen y **video** (Wan, MiniMax) | **No**: *“does not currently offer free trials”*, mínimo **$15** | — | Video asíncrono con sondeo y `video_url`. |
| **Replicate** | imagen y video (modelos de la comunidad) | **No**: unos modelos corren gratis “un rato” y luego pide facturación | 600 creaciones/min; con créditos y sin método de pago: 1/s, 6/min | Predicciones con id y URLs de get/cancel. |
| **fal.ai** | imagen y video | **No**: los créditos gratis **solo** valen en Sandbox/Playground, no en la API | 2 peticiones concurrentes iniciales | Cola con estado/webhook y cancelación; URLs de salida públicas y con caducidad. |
| **Novita** | imagen, audio, video | Promo ($100/90 días en **Sandbox**, sin tarjeta), **no verificado** que paguen la API de generación | — | No se pudo leer el detalle de los endpoints Wan. |
| **Segmind** | imagen y video | **No**, pese a la etiqueta “Free Plan”: sin créditos mensuales, recarga mínima **$10** | — | v1 devuelve bytes; v2 para trabajos largos. |
| **DeepInfra** | imagen (FLUX schnell); lista video sin endpoint verificado | **No**: consumo medido, sin cuota gratis | — | Imagen en base64, síncrona. |

## Qué queda descartado por política, no por hardware

- **Colab gratuito: sí, con claridad.** Su propio FAQ prohíbe servir media/web
  ajena al notebook interactivo y controlarlo fuera de la UI.
- **Paperspace (máquina gratis):** documentado como solo notebooks de
  desarrollo/pruebas, además de 8 GB de VRAM y 5 GB de disco.
- **Kaggle:** los Términos y el AUP no se pudieron leer completos; queda como
  **sin verificar**. No se construye un producto sobre un túnel a Kaggle sin
  confirmar el texto vigente.
- De HF, Modal, Lightning, Studio Lab, GCP y Oracle **no** se verificó una
  prohibición específica de “usar como backend”: en todos los casos el hardware,
  la cuota, el pago, el almacenamiento o la elegibilidad ya deciden.

## Qué significa para Maestro

- La ruta de render del proyecto **ya está alineada** con esta investigación:
  `app/services/engine_providers.py` clasifica los proveedores en
  `local_engine`, `compute_node`, `comfyui`, `public_cloud` y `commercial_api`,
  y **`resolve()` usa `allow_commercial=False` por defecto**: un adaptador
  comercial nunca se usa si no está registrado y elegido explícitamente. El tier
  `public_cloud` se registra con `free: True` y `requires_api_key=False`
  (Spaces anónimos de ZeroGPU), tal como describe [Free public
  compute](PUBLIC-COMPUTE.md).
- **Actualización 2026-10-14:** existen ahora proveedores externos **opt-in**
  (Cloudflare Workers AI y Replicate) en el modo *Cloud*, fuera del enrutado
  automático: apagados por defecto, con clave propia y confirmación de coste por
  trabajo. Ver [PROVEEDORES-EXTERNOS.md](PROVEEDORES-EXTERNOS.md).
- Si en el futuro se añade un tier gratuito más, el único candidato sin tarjeta
  y con cuota publicada es **Cloudflare Workers AI, solo imagen** (token propio
  del usuario, nunca embebido en la app). El video seguiría dependiendo de
  ZeroGPU o de una computadora propia con GPU.
- Los hosts que la gente suele proponer como “gratis” (Modal, Colab, SageMaker
  Studio Lab, GCP) **exigen tarjeta, tienen GPU insuficiente o lo prohíben sus
  términos**: conviene no prometerlos en la documentación de usuario.

## No verificado (no se debe afirmar como hecho)

- Cuota vigente de Kaggle y el texto actual de sus Términos/AUP.
- Si los créditos Sandbox de Novita cubren la API de generación.
- Si los modelos de imagen actuales de Gemini y Veo entran en el tier gratuito.
- Cuota gratuita de Storage Buckets de Hugging Face, de Volumes de Modal y de
  almacenamiento de Lightning.
- Un permiso o prohibición general de automatizar estas APIs desde un cliente de
  escritorio (lo concreto leído: licencias por modelo de fal, aviso de claves
  secretas de Pollinations/Gradio, y divulgación de uso de datos de Google).

## Fuentes leídas

- ZeroGPU: <https://huggingface.co/docs/hub/en/spaces-zerogpu> · Spaces:
  <https://huggingface.co/docs/hub/en/spaces-overview> · almacenamiento:
  <https://huggingface.co/docs/hub/en/spaces-storage> · términos:
  <https://huggingface.co/terms-of-service>
- Cloudflare Workers AI:
  <https://developers.cloudflare.com/workers-ai/platform/pricing/> ·
  <https://developers.cloudflare.com/workers-ai/models/flux-1-schnell/> ·
  <https://www.cloudflare.com/products/workers-ai/>
- Colab FAQ: <https://research.google.com/colaboratory/faq.html>
- Kaggle: <https://www.kaggle.com/docs/notebooks> ·
  <https://www.kaggle.com/terms> · <https://www.kaggle.com/aup>
- SageMaker Studio Lab:
  <https://docs.aws.amazon.com/sagemaker/latest/dg/studio-lab-overview.html> ·
  <https://docs.aws.amazon.com/sagemaker/latest/dg/studio-lab-availability-change.html>
- Modal: <https://modal.com/pricing> · <https://modal.com/docs/guide/billing> ·
  <https://modal.com/docs/guide/gpu> · <https://modal.com/docs/guide/webhooks>
- Lightning AI: <https://lightning.ai/pricing/> ·
  <https://lightning.ai/docs/platform/build/ai-studio>
- Paperspace: <https://www.paperspace.com/pricing> ·
  <https://docs.digitalocean.com/products/paperspace/pricing/>
- Google Cloud: <https://docs.cloud.google.com/free/docs/free-cloud-features> ·
  <https://cloud.google.com/free>
- Oracle: <https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>
- Inference Providers: <https://huggingface.co/docs/inference-providers/pricing> ·
  <https://huggingface.co/docs/inference-providers/tasks/text-to-video>
- Pollinations: <https://github.com/pollinations/pollinations>
- Gemini API: <https://ai.google.dev/gemini-api/docs/image-generation> ·
  <https://ai.google.dev/gemini-api/docs/veo> ·
  <https://ai.google.dev/gemini-api/docs/pricing>
- Together: <https://docs.together.ai/docs/billing-credits> ·
  <https://docs.together.ai/docs/inference/videos/overview>
- Replicate: <https://replicate.com/docs/topics/billing> ·
  <https://replicate.com/docs/topics/predictions/rate-limits>
- fal.ai: <https://fal.ai/docs/documentation/model-apis/faq> ·
  <https://fal.ai/docs/documentation/model-apis/inference/queue>
- Novita: <https://docs.novita.ai/de/api-reference/api-reference-overview> ·
  <https://docs.novita.ai/guides/sandbox-your-first-agent-sandbox>
- Segmind: <https://docs.segmind.com/docs/platform/pricing-and-billing>
- DeepInfra: <https://docs.deepinfra.com/apis/image-generation> ·
  <https://deepinfra.com/pricing>
