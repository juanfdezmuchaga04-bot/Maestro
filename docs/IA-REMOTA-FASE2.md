# Fase 2 — Ejecución remota de IA (Opción B): contrato e implementación

**Decisión tomada:** **Opción B** — el equipo modesto (Lenovo A9-9425 / Radeon R5 /
8 GB) ejecuta el **backend ligero** (interfaz, proyectos, cola durable) y la
generación pesada se **delega a otro Maestro con GPU**.

**Estado:** implementada la capa de delegación, probada, y medida en lo que este
entorno permite medir. La Fase 1 no tocó código; esta fase sí, y solo en las
costuras previstas.

---

## 0. Resumen de una página

- **Qué se construyó:** `services.remote_engine` — el portátil encola un trabajo
  con motor `node:<peer_id>`, ese trabajo viaja por HTTP a la **cola propia** de
  otro Maestro (autenticado con su `MAESTRO_ACCESS_TOKEN` como *bearer*), se sigue
  su progreso real, y el resultado se **descarga al workspace local** y se marca
  `completed` con un fichero real en disco.
- **Qué NO se reescribió:** frontend, cola, persistencia JSON, agentes, flujos de
  Films/Editor, registro de capacidades, nodos de cómputo, adaptador ComfyUI.
- **Qué se añadió alrededor:** autenticación máquina-a-máquina en el gate, entrega
  de resultados por índice (con `Range`), registro de pares como proveedores de
  capacidad, y un estado de motor que ya **informa** de que hay un motor remoto
  disponible (`remote_sources` / `remote_generation_available`). Habilitar el envío
  desde las pantallas es la Fase 3.
- **Verificación:** **259 pruebas OK** (24 nuevas de esta capa, 2 nuevas del gate),
  incluida delegación real sobre HTTP contra un peer real (FastAPI + uvicorn + el
  router de cola + el gate de acceso auténtico).
- **Medición (bloqueo B7 de la Fase 1):** la capa de delegación cuesta **+11 MB**
  sobre Python desnudo; el motor (torch/diffusers/transformers) **371 MB** solo por
  importarse, y `import wgp` llega a **≈710 MB** antes de cargar ningún modelo.
  Ese número es la razón de peso de la Opción B.

---

## 1. Contrato de la Fase 2

### 1.1 Qué se queda local y qué se va al peer

| Pieza | Dónde vive en Opción B |
| --- | --- |
| Interfaz React (bundle servido por el backend) | Equipo modesto |
| Proyectos, brief, storyboard, medios, `outputs/` | Equipo modesto |
| Cola durable, estados, reintentos, cancelación | Equipo modesto (es la dueña de la verdad) |
| Acceso privado (Tailscale) y PWA | Equipo modesto |
| **Generación** (vídeo/imagen/audio) | **Peer con GPU** (otro Maestro, o su ComfyUI) |
| Montaje y export (FFmpeg) | Equipo modesto (ya funciona sin GPU) |
| LLM (Director, escritores) | Donde esté configurado: local o `remote`/`openai`/`anthropic` |

El equipo modesto **no necesita CUDA, ni kernels NVIDIA, ni pesos**: solo Python,
la app y la red privada. Nada del cliente cambia.

### 1.2 La interfaz entre los dos equipos (lo que se fija por escrito)

Un **par** (*peer*) es **otro Maestro completo**, alcanzable por HTTP, con su
propio motor instalado y su propia cola. No se inventa un protocolo nuevo: se usa
el que ya existe (`/api/v1/production/queue`).

```
laptop (cola, proyectos, interfaz)                peer (GPU + ComfyUI)
  trabajo con motor "node:desk-1"
    ├─ GET  /api/v1/engines                      → qué puede hacer de verdad
    ├─ POST /api/v1/production/queue/jobs        → el trabajo, con dedupe_key
    ├─ GET  /api/v1/production/queue/jobs/{id}   → seguirlo; reenviar su progreso
    ├─ GET  …/jobs/{id}/outputs                  → qué produjo (por índice)
    ├─ GET  …/jobs/{id}/outputs/{i}              → descargarlo (reanudable)
    └─ POST …/jobs/{id}/cancel                   → pararlo, y esperar confirmación
  completed, con el fichero en el workspace local
```

**Autenticación.** El peer exige a sus llamantes su propio `MAESTRO_ACCESS_TOKEN`
(modo token del gate), enviado como `Authorization: Bearer <token>`. En la laptop
se guarda **solo ese token**, en `app/settings/remote_peers/remote_peers.json`
(gitignored, permisos `0600`) y **nunca se devuelve** por la API (`token_set: true`).
El gate **solo** acepta el bearer en modo token: en modo contraseña, el hash
PBKDF2 almacenado no es un credencial de acceso y por tanto no vale como cabecera.

**Identificación del par.** `peer_id` (mismo patrón que un nodo:
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`) y el motor resultante es `node:<peer_id>`, igual
que el `provider_id` que ya usa `engine_providers`.

**Capacidades.** Se **declaran** al registrar el par o se **miden** desde el
informe del propio peer (`/api/v1/engines`); nunca se deducen del nombre ni del
hardware. Un par "online" lo está solo mientras una sonda reciente lo confirme
(TTL 3600 s), y un trabajo delega siempre al par que nombra: no hay *fallback*
silencioso a otro motor.

### 1.3 Reglas del transporte (idénticas a las que ya rigen el motor local)

| Regla | Comportamiento |
| --- | --- |
| El ordenador está apagado | El trabajo se **aplaza** (`deferred`): vuelve a `pending`, no consume intento, no registra error |
| Se reintenta un trabajo ya renderizado | Se **adopta** el trabajo existente del peer por `dedupe_key = maestro:<job_id>`; no se renderiza dos veces |
| Progreso | Se reenvía **el que midió el motor del peer**; si no informa, el porcentaje queda en `null` |
| Cifras contradictorias del peer | Gana el porcentaje que reportó el motor; el conteo de pasos se omite (no se re-deriva aquí una cifra que nadie midió) |
| Cancelación | Se pide al peer y **se espera confirmación**; solo entonces se marca `cancelled`. Si no confirma, se registra el hecho en vez de mentir |
| Resultado | `completed` **solo** con un fichero descargado cuyo tamaño coincide con el declarado por el peer |
| Transferencia cortada | Se aplaza y **se reanuda** desde el offset (`Range`), no desde cero |
| Token erróneo | Falla de inmediato con el motivo (`retryable=False`), no se reintenta en bucle |
| Acceso | El peer se consulta por **índice** de salida; nunca se le pide una ruta |

---

## 2. Qué se implementó (fichero a fichero)

### 2.1 Nuevo

| Fichero | Qué hace |
| --- | --- |
| `app/services/remote_engine.py` | Registro de pares (almacén atómico 0600, token solo como credencial de salida), cliente HTTP del peer (stdlib `urllib`, sin dependencias), y el **handler de cola** para `node:<peer_id>` con aplazamiento, adopción, progreso reenviado, cancelación confirmada y descarga reanudable |
| `tests/test_remote_engine.py` | 24 pruebas: almacén y validaciones, entrega de resultados (incluido `Range`), y delegación real sobre HTTP contra un peer vivo |

### 2.2 Modificado (mínimo y por costura)

| Fichero | Cambio |
| --- | --- |
| `app/services/access_gate.py` | Acepta `Authorization: Bearer <MAESTRO_ACCESS_TOKEN>` **solo en modo token** (`accepts_bearer()`), comparado con `hmac.compare_digest` |
| `app/services/production_queue_api.py` | Dos endpoints nuevos: `GET …/jobs/{id}/outputs` (lista por índice: nombre, tipo, tamaño, presente) y `GET …/jobs/{id}/outputs/{index}` (streaming en trozos acotados, `206` con `Range`, `416` si el rango no es válido). La ruta nunca la propone el cliente |
| `app/services/film_api.py` | `register_generation_source()`: el estado de motor reporta las **fuentes remotas** que responden (`remote_sources`, `remote_generation_available`) sin cambiar el significado de `generation_possible`, que sigue midiendo si *este* flujo puede lanzar un render. Así un portátil sin GPU dice la verdad (no puede él, pero hay un equipo configurado) y **no** se habilita un botón que fallaría |
| `app/launch.py` | Registro del handler remoto al arrancar; los pares entran en `_film_engine_registry` como proveedores `compute_node` (con `node_id = peer_id`); fuente de generación remota registrada; y seis endpoints nuevos (ver §3) |
| `tests/test_access_gate.py` | 2 pruebas nuevas: el bearer autentica en modo token (y no crea sesión de navegador) y **nunca** se acepta en modo contraseña |
| `tests/test_production_queue_api.py` | La superficie documentada pasa de 19 a 21 rutas, con las dos de entrega de resultados |
| `docs/STUDIO-QUEUE.md`, `docs/STUDIO-NODES.md` | Documentados los endpoints de salida y la delegación a otro Maestro |

---

## 3. Superficie HTTP nueva

| Método | Ruta | Para qué |
| --- | --- | --- |
| GET | `/api/v1/engines/remote/peers` | Pares configurados, estado medido, motores que enrutan a ellos y fuentes de generación |
| POST | `/api/v1/engines/remote/peers` | Registrar un par, enrutar su motor y **sondearlo una vez** |
| PATCH | `/api/v1/engines/remote/peers/{peer_id}` | Dirección, token, motor o capacidades declaradas |
| DELETE | `/api/v1/engines/remote/peers/{peer_id}` | Olvidarlo aquí (no cambia nada en el otro equipo) |
| POST | `/api/v1/engines/remote/peers/{peer_id}/probe` | Volver a preguntarle qué puede hacer ahora |
| POST | `/api/v1/engines/remote/queue-job` | Encolar en **esta** máquina un trabajo para un par (espera si el par está apagado) |
| GET | `/api/v1/production/queue/jobs/{id}/outputs` \| `/{index}` | Entrega de resultados (la usa el equipo que delegó) |

---

## 4. Cómo se usa (flujo completo)

### 4.1 En el equipo **con GPU** (el peer)

```bash
# 1. Un token fijo para llamantes máquina a máquina (no escribe nada en disco)
#    En Windows: app/env/ENVIRONMENT o la variable de entorno del sistema.
export MAESTRO_ACCESS_TOKEN="<token largo y aleatorio>"

# 2. Arrancar Maestro (app/launch.py) y, en su Studio, dejar el motor local
#    registrado (ComfyUI + prueba funcional pasada), como ya se hace hoy.

# 3. Arrancar el worker de su cola, que es quien cogerá el trabajo delegado:
curl -X POST http://127.0.0.1:7860/api/v1/production/queue/worker/start
```

Nota: en modo token, la propia interfaz de ese equipo pide ese token como
contraseña de acceso. Es el modo que ya existía para automatización; el bearer es
un añadido para llamantes máquina a máquina.

### 4.2 En el **portátil** (el que dirige)

```bash
# 1. Registrar el par (queda sondado de inmediato: capacidades medidas, no supuestas)
curl -X POST http://127.0.0.1:7860/api/v1/engines/remote/peers \
  -H 'Content-Type: application/json' \
  -d '{"peer_id":"desk-1","label":"PC con RTX","base_url":"https://pc.tailnet.ts.net",
       "token":"<el mismo token>","engine":"comfyui"}'

# 2. Arrancar el worker de la cola local (es quien entrega el trabajo al par)
curl -X POST http://127.0.0.1:7860/api/v1/production/queue/worker/start

# 3. Encolar un trabajo para ese par
curl -X POST http://127.0.0.1:7860/api/v1/engines/remote/queue-job \
  -H 'Content-Type: application/json' \
  -d '{"peer_id":"desk-1","task":"video","capability":"text_to_video",
       "params":{"prompt":"un barco de papel bajando por un arroyo, cinematográfico","seconds":5}}'
```

El trabajo aparece en la cola local, el worker lo envía al peer, se ve el progreso
que el peer realmente mide, y al terminar el clip queda **en el workspace local**
(el mismo que sirve `/api/v1/file`), así que Galería, Editor y Films lo ven sin
cambios. Si el peer está apagado, el trabajo **espera** en vez de fallar.

El motor con el que el peer ejecuta el trabajo delegado se elige con `engine` del
par (por defecto `comfyui`, su motor por defecto) o con `params.remote_engine` en
el trabajo.

---

## 5. Verificación (evidencia real de esta fase)

### 5.1 Pruebas ejecutadas

```bash
PYTHONPATH=app python3 -m unittest tests.test_remote_engine tests.test_access_gate \
  tests.test_production_queue_api tests.test_comfy_queue tests.test_compute_nodes \
  tests.test_engine_providers tests.test_queue_worker tests.test_local_engine \
  tests.test_local_engine_api tests.test_film_api
# → Ran 259 tests … OK (21.7 s)
```

Las 24 pruebas nuevas de `tests/test_remote_engine.py` **no simulan el transporte**:
levantan un peer real (FastAPI + uvicorn) con el **router real** de la cola y el
**gate real** con token, y comprueban sobre HTTP:

- un trabajo delegado termina con un fichero **byte a byte idéntico** al del peer, en el workspace local, y con el `external_job_id` del peer registrado;
- un peer apagado → `deferred`, `attempts == 0`, `errors == []`, sin perder el trabajo;
- token erróneo → fallo inmediato con el motivo (y mención a `MAESTRO_ACCESS_TOKEN`);
- un trabajo ya renderizado en el peer se **adopta** (no se re-renderiza: sigue habiendo un solo trabajo allí);
- el peer mide 25 % (paso 3/12) y eso es exactamente lo que muestra la cola local mientras corre; con cifras contradictorias (90 % y paso 1/100) se muestra 90 % y **nunca** el 1 % derivado;
- una cancelación se marca `cancelled` **solo** después de que el peer confirme que paró, y su propio trabajo queda `cancelled`;
- la descarga **se reanuda** desde un `.part` a medias y borra el parcial al terminar;
- la entrega de resultados: 401 sin credencial, 404/422 con índice o trabajo inexistentes, `206` con `Range` y `416` si el rango no es posible;
- el gate: el bearer autentica **solo** en modo token, y no se acepta en modo contraseña.

### 5.2 Medición del peso en RAM (bloqueo B7 de la Fase 1)

Medido con `resource.getrusage(RUSAGE_SELF).ru_maxrss` en procesos nuevos, en este
contenedor (Linux, ruedas **CPU**, sin contexto CUDA):

| Importación | maxRSS |
| --- | --- |
| Python desnudo | 8 MB |
| + `fastapi` + `uvicorn` (la API) | 45 MB |
| + `services.remote_engine` (la capa de delegación) | **19 MB** |
| + `engine_providers` + `compute_nodes` + `generation_queue` + `production_queue_api` + `remote_engine` | 44 MB |
| + `torch` | 197 MB |
| + `diffusers` + `transformers` | 341 MB |
| + `librosa` + `cv2` + `av` + `soundfile` | 372 MB |
| `import wgp` (con `git`/`easydict` sustitutos; falla en código de modelo) | **≈710 MB en ~7 s** |

Lectura: la capa de delegación cuesta **11 MB** más que Python desnudo y **no**
importa torch (por eso es importable en un equipo sin GPU), mientras que tener el
motor **en el mismo proceso** arranca desde ~370 MB y llega a ~0,7 GB solo por
importar, antes de cargar un solo peso. En un equipo de 8 GB eso no es imposible,
pero es exactamente el coste que la Opción B evita.

**Límites de la medición:** ruedas CPU y sin contexto CUDA (una instalación real
con GPU añade el contexto de CUDA); `wgp` no se importó completo (faltan
`git`/`easydict` en este contenedor), por lo que **710 MB es una cota inferior**, no
el valor final; y maxRSS es el pico de **importación**, no el consumo bajo carga de
generación (que no se puede medir aquí: no hay GPU).

---

## 6. Lo que **no** está hecho (declarado, no disimulado)

1. **La UI todavía no cablea la delegación en los flujos de Studio/Films.** El
   botón "generar planos" sigue enviando al motor **local**
   (`_prepare_generation_submission`), que en el portátil no puede ejecutar nada.
   Hoy la delegación se usa por API (`/api/v1/engines/remote/queue-job`) y la
   cola/estado ya son los definitivos. Cablear los flujos a `node:<peer_id>` y
   añadir el panel de pares es el siguiente paso (Fase 3).
2. **El worker de la cola no arranca solo.** Es explícito a propósito (igual que
   hoy): hay que llamar a `POST /api/v1/production/queue/worker/start` en los dos
   equipos. La respuesta de `queue-job` indica si está corriendo.
3. **No se ha contactado ningún peer real.** `launch.py` no arranca en este
   contenedor (faltan dependencias del runtime fijado: `git`, `easydict`, … y no hay
   GPU), así que los seis endpoints nuevos están **compilados y revisados**, pero no
   ejercitados en ejecución aquí. Sí lo están el módulo de servicio, el protocolo
   del peer y la entrega de resultados, sobre HTTP real.
4. **Sin multiusuario.** Sigue siendo un producto de un solo usuario; quien tenga
   el token del peer puede usarlo. La Fase 5 (endurecimiento) sigue pendiente:
   gate obligatorio fuera de loopback, cuotas, permisos por endpoint, aprobación
   humana para operaciones sensibles.
5. **Sin cifrado propio extremo a extremo.** El canal es el que ya existe: LAN o
   Tailscale (HTTPS con certificado). Si el peer se expone de otra forma, la
   seguridad del transporte es responsabilidad del despliegue.
6. **La reanudación de un render interrumpido en el peer** no se asume: si el peer
   se reinicia a mitad de un trabajo, el trabajo se re-cola allí por sus propias
   reglas; aquí se adopta el resultado cuando exista o se aplaza mientras no haya
   respuesta.

---

## 7. Siguiente fase propuesta (Fase 3)

1. **Cablear los flujos a la delegación**: cuando el motor local no pueda ejecutar
   (sin GPU o sin modelos), que Studio/Films encolen con `engine = node:<peer_id>`
   y capacidad declarada, sin cambiar la experiencia de usuario.
2. **Panel de pares** en *Studio → Compute & transfer*: alta, sonda, estado
   (online/offline), capacidades medidas y último trabajo; con el token en
   `*_set` y nunca devuelto al cliente.
3. **Adaptador de IA uniforme**: extender a todas las capacidades lo que hoy solo
   tienen algunas (timeouts, errores homogéneos, validación de entrada/salida,
   límites de consumo, telemetría sin secretos).
4. **Reanudación e idempotencia explícitas** por `job_id` en el worker remoto
   (la base ya está: `dedupe_key`), y política de reintentos por tipo de fallo.
5. **Endurecer** (gate obligatorio fuera de loopback, permisos por endpoint,
   cuotas, aprobación humana).

Cada paso, con su prueba de regresión y sin tocar React/Zustand ni los ficheros de
proyecto (los flujos siguen intactos).
