# Proveedores externos de imagen y vídeo (opt-in)

Maestro genera en local, en una computadora tuya o en cómputo público gratuito
([PUBLIC-COMPUTE.md](PUBLIC-COMPUTE.md)). Este documento cubre el **cuarto camino**:
enviar una generación a una **API comercial** con tu propia cuenta. Está apagado por
defecto y no participa en el enrutado automático (`engine_providers.resolve()` sigue
con `allow_commercial=False`). Solo se usa desde el modo **Cloud** de la interfaz.

Fecha de verificación de la documentación de los proveedores: **2026-10-14**.

## Proveedores integrados

| Proveedor | Modalidad | Modelo | Coste (estimación) | Cuenta | Cancelación |
|---|---|---|---|---|---|
| **Cloudflare Workers AI** | Imagen | `@cf/black-forest-labs/flux-1-schnell` (Apache-2.0) | **Gratis dentro de 10 000 Neurons/día** (reinicio 00:00 UTC). 1024×1024 a 4 pasos ≈ 57,6 Neurons (4,80/tesela 512² + 9,60/paso) → ~170 imágenes/día. Con Workers Paid, $0,011 por 1 000 Neurons extra | Cuenta Cloudflare (plan Free sin tarjeta), **Account ID** y **API token** con permiso *Workers AI* | **Solo local**: la llamada es síncrona y no hay endpoint de cancelación; Maestro deja de esperar y lo dice (puede consumir cuota) |
| **Replicate** | Imagen | `black-forest-labs/flux-schnell` (Apache-2.0) | ≈ **$0,003/imagen** (página del modelo) | Cuenta Replicate **con facturación** (crédito o tarjeta) y **API token** | **Remota**: `POST /v1/predictions/{id}/cancel` |
| **Replicate** | Vídeo | `wan-video/wan-2.2-t2v-fast` (Apache-2.0) | ≈ **$0,05 a 480p, $0,10 a 720p** por vídeo de 81 fotogramas (precio de lanzamiento); más fotogramas se estiman proporcionalmente | Igual que arriba | **Remota** |

Límites del vídeo (esquema del modelo): 81–100 fotogramas, 5–24 fps (16 por defecto
→ ≈ 5,1 s), 480p (832×480 en 16:9) o 720p, 16:9 o 9:16, **MP4 sin audio**. Tiempo
máximo de espera en Maestro: 20 min (vídeo), 5 min (imagen Replicate), 120 s
(Cloudflare). Al vencer, Maestro cancela la predicción en Replicate y marca el trabajo
como `timeout`.

**Los precios cambian.** Cada estimación muestra su base y el enlace a la página
donde comprobar el precio vigente. Lo que se cobra es lo que factura el proveedor.

## Por qué estos y no otros

Comparativa realizada antes de integrar (detalle de fuentes en
[COMPUTO-GRATUITO.md](COMPUTO-GRATUITO.md)):

| Opción | Imagen | Vídeo | ¿Gratis? | API / cancelación | Decisión |
|---|---|---|---|---|---|
| **Cloudflare Workers AI** | Sí (FLUX schnell) | No | **Sí, cuota diaria sin tarjeta** | REST síncrono, token propio; sin cancelación | **Integrado (imagen)** — única cuota gratuita recurrente verificada sin tarjeta |
| **Replicate** | Sí | Sí (Wan 2.2, etc.) | No, pago por uso | REST documentado: crear, consultar, **cancelar**; `GET /v1/account` permite comprobar el token sin coste; 402 sin crédito | **Integrado (imagen + vídeo)** — un solo token, ciclo de vida y cancelación explícitos |
| fal.ai | Sí | Sí | No (créditos gratis solo en el *playground*) | Cola con estado y cancelación | Buen candidato futuro; no añade nada que Replicate no cubra hoy |
| Google Gemini (Imagen / Veo) | Sí | Sí | No verificado para estos modelos | Operación larga sin cancelación documentada; el tier gratuito puede usar datos para mejorar productos | No integrado |
| Together AI | Sí | Sí | No (compra mínima $15) | Asíncrono con sondeo | No integrado |
| HF Inference Providers | Sí | Sí | No para cuentas gratuitas | — | No integrado; ZeroGPU ya cubre el tier gratuito |

## Garantías de transparencia y consentimiento

1. **Nada sale del equipo sin dos acciones explícitas**: activar el proveedor (casilla
   *Enabled*) y guardar tu clave. Todo está desactivado al instalar.
2. **Cada trabajo se confirma.** *Review cost and data* muestra: proveedor, modelo y
   licencia, host al que se envía, **lista exacta de datos enviados** (texto de la
   descripción, parámetros, la clave como cabecera de autorización — ningún archivo
   ni contenido de la galería), coste estimado con su base, requisitos de cuenta,
   límites, retención de datos, comportamiento de cancelación y enlaces a términos y
   documentación. Solo tras marcar *I want to send…* se habilita *Confirm and start*.
3. **La confirmación está ligada a la petición.** El servidor calcula un
   `confirm_token` (SHA-256 de la petición normalizada + la divulgación). Si cambias
   cualquier parámetro, el token deja de valer y `POST /jobs` responde
   `409 confirmation_required`. Un cliente no puede saltarse la revisión.
4. **Un trabajo solo es `succeeded` si hay un archivo real**: descargado o decodificado,
   reconocido por sus bytes como imagen/vídeo del tipo pedido y escrito en el
   workspace activo (con `.meta.json`). JSON, HTML, vacío o una imagen donde se pidió
   vídeo → `failed / invalid_output`.

## Claves API

Las claves **no** se guardan en `server_config` (donde viven las demás claves de
servicios). `app/services/external_secrets.py` usa, por orden:

1. Variables de entorno (solo lectura): `REPLICATE_API_TOKEN`, `CLOUDFLARE_API_TOKEN`,
   `CLOUDFLARE_ACCOUNT_ID`.
2. **Almacén de credenciales del sistema** vía `keyring` (Administrador de
   credenciales de Windows, Llavero de macOS, Secret Service en Linux).
3. Sin `keyring` en Windows: archivo **cifrado con DPAPI** para el usuario actual.
4. En otro caso: archivo con permisos `0600` en `app/settings/external_providers/`.
   La interfaz indica cuál se usa; no se presenta como más seguro de lo que es.

Ninguna respuesta de la API devuelve una clave: solo si está puesta, su origen y los
4 últimos caracteres.

## Errores que se distinguen

| `error.code` | Cuándo | HTTP de Maestro |
|---|---|---|
| `provider_disabled` | El proveedor no está activado | 409 |
| `missing_credentials` | Falta token o Account ID | 412 |
| `confirmation_required` | No hay confirmación o la petición cambió | 409 |
| `auth_failed` | El proveedor rechaza la clave (401/403) | 401 |
| `insufficient_credit` | Sin saldo (402 o mensaje de facturación) | 402 |
| `quota_exhausted` | Cuota gratuita agotada (p. ej. Cloudflare 4006) | 429 |
| `rate_limited` | Límite de peticiones (429, con `retry_after` si lo da) | 429 |
| `invalid_input` | 400/413/422 del proveedor o validación local | 400 |
| `not_found` | Modelo o predicción inexistente | 404 |
| `provider_unavailable` | 5xx del proveedor | 503 |
| `timeout` / `network_error` | Sin respuesta a tiempo / no se alcanza el host | 504 / 502 |
| `provider_failed` | La predicción terminó en `failed` (se muestra el motivo del proveedor) | — |
| `invalid_output` / `download_failed` | Resultado inexistente, no reconocible o de otro tipo | — |
| `interrupted` | Maestro se cerró esperando una respuesta síncrona | — |

Si Maestro se reinicia con una predicción de Replicate en curso, **retoma el sondeo**
con el id guardado en lugar de volver a cobrar o abandonarla.

## Configuración paso a paso

**Cloudflare Workers AI (imagen, gratis dentro de la cuota)**

1. Crea una cuenta en <https://dash.cloudflare.com/sign-up> (plan Free).
2. Copia el **Account ID** (panel → *Workers AI* o la barra lateral de la cuenta).
3. *My Profile → API Tokens → Create Token →* plantilla **Workers AI** (lectura y
   edición de Workers AI). Copia el token.
4. En Maestro: modo **Cloud** → tarjeta *Cloudflare Workers AI* → pega Account ID y
   token → **Save keys** → **Check key (free)** → marca **Enabled**.

**Replicate (imagen y vídeo, de pago)**

1. Crea una cuenta en <https://replicate.com/signin> y configura la facturación
   (<https://replicate.com/account/billing>). Sin crédito, Replicate responde 402 y
   Maestro lo muestra como `insufficient_credit`.
2. Crea un token en <https://replicate.com/account/api-tokens>.
3. En Maestro: modo **Cloud** → tarjeta *Replicate* → pega el token → **Save keys** →
   **Check key (free)** (usa `GET /v1/account`, no genera nada) → **Enabled**.

**Generar**: elige *Image* o *Video*, el proveedor, escribe la descripción, ajusta
parámetros → **Review cost and data** → revisa → marca la casilla → **Confirm and
start**. El trabajo aparece en *Jobs* con su estado (`queued → submitting → running →
downloading → succeeded`), su progreso, el botón **Cancel** mientras está activo y,
al terminar, la vista previa y el enlace de descarga. El archivo queda en el
workspace activo y aparece en la galería.

## API HTTP

```
GET    /api/v1/external/providers
PUT    /api/v1/external/providers/{id}            {"enabled": true, "credentials": {...}}
DELETE /api/v1/external/providers/{id}/credentials
POST   /api/v1/external/providers/{id}/test
POST   /api/v1/external/quote                      {"provider","modality","prompt",...}
POST   /api/v1/external/jobs                       {"params": {...}, "confirm_token": "..."}
GET    /api/v1/external/jobs[/{id}]
POST   /api/v1/external/jobs/{id}/cancel
```

## Qué se ha probado realmente y qué solo está preparado

**Probado (en el contenedor de desarrollo, sin red hacia los proveedores):**

- 33 pruebas automatizadas (`tests/test_external_media.py`) con un transporte HTTP
  que responde con la forma documentada de Replicate y Cloudflare: configuración
  apagada por defecto, claves que nunca se devuelven, DPAPI/keyring/0600,
  confirmación ligada a la petición, éxito de imagen y vídeo, 401/402/404/422/429/5xx,
  cuota 4006, error de red, timeout con cancelación remota, cancelación antes de
  enviar, durante el sondeo y durante una llamada síncrona, resultados HTML / tipo
  equivocado / sin salida, y reanudación tras reinicio. También la API HTTP con
  `TestClient`.
- La interfaz completa (modo Cloud) recorrida en Chromium contra el router real con
  un **proveedor simulado**: guardar claves, activar, revisar la divulgación,
  error de cuota mostrado con su código, vídeo con estados en vivo y archivo MP4
  reproducido desde la galería. Esto valida la interfaz, **no** a los proveedores.
- `npm run lint`, `npm run build`, ruff F821/F823 y las pruebas de lanzador.

**No probado (pendiente, requiere tus credenciales y tu confirmación de coste):**

- **Ninguna generación real** contra Cloudflare o Replicate. El entorno de desarrollo
  no tiene salida de red a `api.replicate.com` ni `api.cloudflare.com` (el proxy
  bloquea la conexión) ni claves de cuenta, y una generación de Replicate tiene coste.
- Los formatos exactos de error en vivo (p. ej. si Cloudflare devuelve 4006 con 429
  u otro estado) se basan en la documentación y en mensajes publicados; el
  clasificador busca el texto además del código de estado por eso.
- Precios: tomados de las páginas de modelo/precios; confirmar el vigente antes de usar.

**Prueba de aceptación pendiente (5 minutos, en tu equipo):**

1. Configura Cloudflare como arriba y genera una imagen (gratis dentro de la cuota).
   Verifica: estado `succeeded`, archivo `*_external_cloudflare_*.jpg` en el
   workspace, `sha256` en el trabajo (`GET /api/v1/external/jobs/{id}`) igual al del
   archivo.
2. Configura Replicate con crédito y genera un vídeo 480p de 81 fotogramas
   (≈ $0,05). Verifica `succeeded`, el MP4 reproducible y el id de predicción en el
   panel de Replicate (<https://replicate.com/predictions>).
3. Opcional: lanza otro vídeo y pulsa **Cancel**; el panel de Replicate debe mostrar
   la predicción como *canceled*.
