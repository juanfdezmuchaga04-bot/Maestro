# Maestro en tu iPhone o iPad — puesta en marcha

Maestro **no es una web con servidor en la nube**. La aplicación, tus proyectos y la cola de
trabajo viven en **tu ordenador**; el iPhone o iPad es un cliente. De ahí se derivan todas
las reglas de esta guía: la app necesita una ruta *hacia tu ordenador* y nada funciona con
ese ordenador apagado.

| Pieza | Dónde corre | Notas |
| --- | --- | --- |
| Interfaz (React) + API (FastAPI) | tu ordenador, un solo proceso | `app/launch.py`, puerto de `SERVER_PORT` (7860 por defecto) |
| Proyectos, medios, cola de trabajos | tu ordenador, en `outputs/` | nada se sube a ningún sitio |
| Motor de generación | GPU NVIDIA del ordenador | en un **Mac** o un PC sin NVIDIA no hay generación local: el trabajo se envía a un nodo con GPU |
| Cliente iPhone / iPad | Safari, instalado como app | se añade a la pantalla de inicio; sesión con contraseña de acceso |

No hay cuenta ni servidor que alquilar. Las dos piezas de terceros son opcionales y gratuitas
para uso personal: Tailscale (acceso privado) y las notificaciones push del propio navegador.

---

## 1. Instala Maestro en el ordenador

**Con Pinokio (recomendado).** Instala [Pinokio](https://pinokio.computer), busca Maestro,
pulsa **Install** y luego **Start**.

- En un **Mac** (Apple Silicon, macOS 14+): [instalación en un Mac](INSTALAR-EN-MAC.md).
- En un PC **sin NVIDIA** (AMD/Intel, o sin GPU dedicada): [instalar sin NVIDIA](INSTALAR-SIN-NVIDIA.md).
- En un PC **con NVIDIA**: `README.md` → *Install*. Es la única configuración que genera
  localmente.

Si no vas a generar en ese ordenador, aquí no falta nada: la instalación ligera crea la
interfaz, la cola, el editor y el cliente de nodos remotos.

## 2. Pon la contraseña de acceso (antes de exponer nada)

En `localhost` Maestro no pide login: el navegador y el servidor son el mismo usuario. En
cuanto otro dispositivo puede alcanzar el puerto, eso cambia:

```bash
python scripts/maestro_access.py enable
```

Imprime una contraseña generada **una sola vez** (guárdala). Se guarda un hash PBKDF2, nunca
la contraseña. Comportamiento: cada petición necesita una cookie de sesión firmada (30 días);
un visitante anónimo recibe `401` en `/api/...` y la página de acceso en una navegación;
manifiesto, iconos, service worker y página offline siguen siendo públicos —por eso la app
puede instalarse y explicar que el ordenador está apagado—. Cinco fallos desde una misma
dirección bloquean temporalmente esa dirección. `python scripts/maestro_access.py status`
muestra el estado sin revelar secretos, y `/api/v1/access/logout` cierra la sesión de un
dispositivo.

## 3. Da al iPhone una dirección HTTPS privada

iOS exige HTTPS para instalar la app como web-app con notificaciones, así que este paso no es
opcional. Tailscale es la vía más barata: mantiene el puerto cerrado a Internet y emite un
certificado válido.

1. Instala Tailscale en el ordenador y en el iPhone/iPad, con **tu propia cuenta** (plan
   Personal gratuito) en ambos.
2. Arranca Maestro. En el menú de Pinokio elige **Secure Remote Access (Tailscale)**; si tu
   sistema no permite elevación, usa **Settings → Notifications → Private HTTPS access**.
   (En un Mac esto funciona aunque no instales los comandos de línea de órdenes de Tailscale:
   el CLI se busca también dentro de `Tailscale.app`.)
3. Maestro muestra la dirección privada: `https://<equipo>.<tu-tailnet>.ts.net`. Ésa es tu URL
   en todos tus dispositivos.

Detalle y problemas: [`TAILSCALE_REMOTE_ACCESS.md`](TAILSCALE_REMOTE_ACCESS.md).
`python scripts/maestro_doctor.py` imprime la misma dirección en *«Reach it from your
phone»* y avisa si la ruta falta.

## 4. Añádelo a la pantalla de inicio

1. Abre la dirección `https://…ts.net` **en Safari** (no en Chrome).
2. **Compartir → Añadir a pantalla de inicio**.
3. Abre el icono **Maestro** instalado e inicia sesión con la contraseña de acceso.

Qué pasa en ese momento, y por qué importa:

- Se abre **sin barras del navegador** (el manifiesto declara `display: standalone`, y la app
  mantiene también la etiqueta `apple-mobile-web-app-capable` para versiones antiguas).
- El icono es un **`apple-touch-icon` de 180×180** propio (iOS ignora los iconos del
  manifiesto para la pantalla de inicio). Se genera desde el arte canónico con
  `scripts/generate_pwa_icons.py`, así que pesa ~33 KB en lugar de varios MB.
- La barra de estado se fija en opaco (`apple-mobile-web-app-status-bar-style = black`) para
  que su sombreado no tape la cabecera de la aplicación.
- Si abres la misma URL en una pestaña normal de Safari o Chrome, **no** podrás pedir permiso
  de notificaciones: WebKit sólo lo concede a web-apps instaladas desde iOS/iPadOS 16.4.

## 5. Activa las notificaciones (opcional)

En la app instalada: **Ajustes → Notifications → System notifications**, y toca el botón para
conceder el permiso. Es un gesto real del usuario, no un aviso automático: iOS lo exige.

- Requiere **iOS/iPadOS 16.4 o superior** y HTTPS (paso 3).
- El panel informa del estado real: si estás en una pestaña en lugar de la app instalada, dirá
  exactamente qué falta en lugar de fallar en silencio.
- Hay un botón de **notificación de prueba** para comprobar el circuito completo.
- Los avisos llegan aunque la app esté cerrada: el ordenador (que debe seguir encendido y
  despierto) envía el push al servicio del navegador con las claves VAPID guardadas en
  `app/settings/web_push.json`.

## Qué puedes hacer desde el iPhone/iPad

| Área | Estado |
| --- | --- |
| Interfaz completa, Studio, Films, editor, biblioteca, ajustes | Sí |
| Ver, ampliar y compartir resultados; visor con gestos, zoom y comparación | Sí (pensado para táctil, con áreas seguras respetadas) |
| Enviar imágenes de la galería o fotos como entrada de un trabajo | Sí |
| Cola de producción: ver estados, cancelar, reintentar | Sí |
| Dirigir la generación en un **nodo / motor con GPU** | Sí |
| Generar **en el propio teléfono** | No: el móvil nunca renderiza |
| Trabajo en segundo plano en el teléfono | No: iOS suspende la pestaña; la generación vive en el ordenador |

## Problemas frecuentes

- **Safari no ofrece «Añadir a pantalla de inicio»** → estás en una URL `http://`, o en un
  navegador embebido (por ejemplo, dentro de otra app). Usa Safari con la dirección `https://`.
- **«Las notificaciones necesitan HTTPS» o faltan las opciones** → paso 3 sin completar, o la
  estás usando desde una pestaña. Borra cualquier acceso directo antiguo, añádela de nuevo y
  abre **esa** app instalada.
- **Me pide la contraseña una y otra vez** → la cookie de sesión no es `Secure` porque la URL
  es `http://`; vuelve a la dirección `https://…ts.net`.
- **«Maestro is not reachable»** → el ordenador está apagado, dormido o sin Tailscale. El
  service worker muestra la página offline en lugar de una pestaña muerta.
- **Los avisos no llegan con la app cerrada** → el ordenador debe estar encendido y despierto;
  las notificaciones las envía él. Revisa *Ajustes → Notifications* y usa la prueba.
- **Nada carga tras una actualización** → abre *Ajustes → Notifications → Private HTTPS
  access* una vez más para que la ruta adopte el puerto nuevo, y recarga.

## Límites conocidos

- El teléfono es un cliente: **sin el ordenador encendido no hay Maestro**.
- iOS suspende las pestañas en segundo plano; los avisos dependen de Web Push, no de que la
  app siga abierta.
- Subir vídeos muy grandes desde el móvil tarda lo que tarde tu red local o tu tailnet.
- Esta guía describe el despliegue, no el rendimiento: la velocidad de generación la fija la
  máquina que tenga la GPU.
