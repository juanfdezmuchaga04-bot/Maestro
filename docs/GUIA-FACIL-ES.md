# Maestro en tu ordenador y en tu móvil — guía fácil

Esta guía está escrita para alguien que nunca ha instalado nada parecido. Cada paso
termina con una frase del tipo **«vas bien si…»** para que sepas que no te has perdido.

> Los documentos técnicos completos están en inglés: [`DEPLOY-ANDROID.md`](DEPLOY-ANDROID.md),
> [`TAILSCALE_REMOTE_ACCESS.md`](TAILSCALE_REMOTE_ACCESS.md), [`COMFYUI-LOCAL.md`](COMFYUI-LOCAL.md).
> Este es el resumen fácil.

---

## Antes de empezar: la idea en 3 frases

1. **Maestro vive en tu ordenador.** No es una web en la nube: el programa, tus
   proyectos y (si tu tarjeta puede) el motor que crea vídeos están en tu PC.
2. **El móvil es un mando a distancia.** Sirve para mirar, preparar y lanzar cosas.
3. **Para generar vídeo, el ordenador tiene que estar encendido** con Maestro abierto.
   Si está apagado, el móvil se abre igual pero te dirá que no llega al ordenador.

Y lo importante: **no hay que crear ninguna cuenta de Maestro, no se paga por generar y
no se sube nada tuyo a internet.** Si tu ordenador no puede generar por sí mismo (no
tiene tarjeta NVIDIA, o se la presta a otro equipo), Maestro busca solo una **máquina
pública y gratuita** en internet (Parte 4B) o manda el trabajo al ordenador con tarjeta
que tú le indiques — en los dos casos sin que tengas que hacer nada.

## Lo que necesitas (comprueba esto primero)

| Cosa | Mínimo | Mejor |
| --- | --- | --- |
| Ordenador | Windows 10/11, Linux o macOS | Windows 11 (o un Mac con Apple Silicon, macOS 14+) |
| Tarjeta gráfica | NVIDIA con 6 GB de memoria (VRAM); 8 GB o más para el motor local | RTX 3090 / 4090 / 5090 (24 GB) |
| Memoria RAM | 16 GB | 32 GB o más |
| Disco libre | 150 GB | 300–500 GB |
| Tiempo | 20–30 min para instalar y algo de descarga | — |

- **Ni un Mac ni una tarjeta AMD/Intel generan**: el motor necesita NVIDIA. Pero eso **ya no
  impide instalar ni arrancar Maestro**: en un ordenador sin NVIDIA se instala el *runtime
  ligero de CPU* y funciona todo menos la generación local (interfaz, Studio, Films, editor,
  cola y motores remotos con GPU). Detalles en [INSTALAR-SIN-NVIDIA.md](INSTALAR-SIN-NVIDIA.md)
  y, si es un Mac, en [INSTALAR-EN-MAC.md](INSTALAR-EN-MAC.md) (requiere Apple Silicon y
  macOS 14 o superior).
- Cuanta menos memoria tenga la tarjeta, más lentas serán las generaciones. Con 6–8 GB
  funciona, pero con clips cortos y resolución baja.

---

# PARTE 1 — El ordenador (se hace una sola vez)

### Paso 1. Instala Pinokio

1. Entra en **<https://pinokio.computer>** y descarga Pinokio para tu sistema.
2. Instálalo como cualquier otro programa (siguiente, siguiente, finalizar).

**Vas bien si…** al abrir Pinokio ves una ventana con pestañas.

### Paso 2. Instala Maestro

1. En Pinokio, abre la pestaña **Discover**.
2. Busca **Maestro** y pulsa **Install**.

> **Ojo si usas una copia propia (un fork).** El botón **Discover** instala el Maestro
> original, no tus cambios. Para instalar **tu** repositorio: pestaña **Download**, pega la
> dirección de tu copia (por ejemplo `https://github.com/tu-usuario/Maestro.git`) y pulsa
> **Download**. Comprueba antes en GitHub que tus cambios están **subidos** (los commits que
> ves en el panel *Changes* tienen que estar publicados); si no, Pinokio instalará la versión
> antigua y seguirá apareciendo el aviso de que hace falta una GPU NVIDIA.

3. Espera. Tarda entre **10 y 20 minutos**. Verás texto pasando: es normal.
4. Mientras instala, deja el ordenador encendido y **que no se suspenda** (si se
   duerme, al volver pulsa **Update** o **Install** otra vez: no se rompe nada).
5. Si tu ordenador no tiene NVIDIA verás un aviso («No NVIDIA GPU reported…»). **Es solo
   informativo**: la instalación sigue y crea el runtime ligero de CPU.

**Vas bien si…** el botón deja de decir «Installing» y aparece **Start** (en un equipo sin
NVIDIA dice «Start — CPU / light runtime»).

> Nota: instalar la app **no** descarga los modelos de vídeo. Esos se bajan la primera
> vez que generas algo (unos 18 GB) y solo hay que esperarlos una vez.

### Paso 3. Arranca Maestro

1. Pulsa **Start** en la página de Maestro en Pinokio. Si no hay NVIDIA, el menú avisa
   («CPU / light runtime») y Maestro arranca igual; la generación local necesita un equipo
   con GPU ([INSTALAR-SIN-NVIDIA.md](INSTALAR-SIN-NVIDIA.md)).
2. Se abre una ventana con texto que se mueve sin parar. **No la cierres**: es el motor
   arrancando. La primera vez tarda un poco.
3. Cuando el motor esté listo aparece un botón azul: **Open Web UI**. Púlsalo.

**Vas bien si…** se abre Maestro en tu navegador y ves la pantalla con Director,
Studio, Films y Editor.

### Paso 4 (opcional). El chequeo de salud

Si algo va raro, hay un comando que revisa todo (GPU, CUDA, disco, ffmpeg, contraseña
y la dirección del móvil) y te dice con claridad qué falta. Está en inglés, pero se lee
en un segundo: cada línea empieza por `[ok]` (bien), `[warn]` (aviso) o `[fail]` (falta
algo).

1. Abre la **carpeta de Maestro** en tu ordenador (donde están `start.js`, `app` y `ui`;
   suele estar dentro de `pinokio/api` en tu carpeta de usuario).
2. Abre una consola **dentro de esa carpeta**:
   - **Windows:** haz clic en la barra de direcciones de la carpeta (arriba), borra lo
     que pone, escribe `cmd` y pulsa Enter. Se abre una ventana negra en esa carpeta.
   - **Linux:** clic derecho dentro de la carpeta → *Abrir en terminal*.
3. Pega esto y pulsa Enter:

   ```bash
   python scripts/maestro_doctor.py
   ```

4. Lee el resultado: cada línea `[ok]` está bien; cada `[fail]` te dice qué falta.

**Vas bien si…** ves varias líneas `[ok]` y al final una lista de «Next steps».

---

# PARTE 2 — La contraseña (1 minuto, antes de tocar el móvil)

Maestro no pide contraseña mientras solo se usa **en el mismo ordenador**. En cuanto el
móvil puede llegar a él, hay que poner una, porque si no, cualquiera que alcance ese
puerto podría manejar tu estudio.

1. En la misma consola de la carpeta de Maestro, escribe:

   ```bash
   python scripts/maestro_access.py enable
   ```

2. Te mostrará una contraseña **una sola vez**. Cópiala en el móvil, en una nota o en tu
   gestor de contraseñas. Maestro solo guarda un hash: si la pierdes, no se puede
   recuperar, pero sí crear una nueva.

**Vas bien si…** ves la frase `Sign-in gate enabled.` y, justo debajo, la contraseña.

Si te dice que `python` no existe (pasa en algunos Windows), usa el Python que Pinokio
ya instaló, cambiando solo la primera parte:

```bash
app\env-sol\Scripts\python.exe scripts\maestro_access.py enable
```

y si tampoco funciona, prueba con `app\env-rtx50\Scripts\python.exe`,
`app\env\Scripts\python.exe` o `app\env-light\Scripts\python.exe` — exactamente **una** de
esas carpetas existe en tu instalación, y `env-light` es la de un equipo sin NVIDIA
(Windows/Linux sin NVIDIA, o un Mac). En Linux y macOS la ruta equivalente usa
`bin/python` en lugar de `Scripts\python.exe`.

Otros comandos útiles, siempre desde la carpeta de Maestro:

| Quiero | Comando |
| --- | --- |
| Ver si está activada (no muestra la contraseña) | `python scripts/maestro_access.py status` |
| Crear una contraseña nueva (cierra la sesión en todos los dispositivos) | `python scripts/maestro_access.py reset` |
| Quitarla (solo si el ordenador no está expuesto a la red) | `python scripts/maestro_access.py disable` |

---

# PARTE 3 — El móvil (se hace una sola vez)

Lo que buscamos: que tu móvil tenga una **dirección privada y segura** para llegar a tu
ordenador. La forma fácil y gratis es **Tailscale**: crea un túnel solo para tus
dispositivos, con candado (HTTPS), y **no publica nada en internet**.

### Paso 1. Instala Tailscale en los dos sitios

1. En el **ordenador**: descarga Tailscale de <https://tailscale.com/download>, instálalo
   e inicia sesión con la cuenta que crees.
2. En el **móvil**: instala **Tailscale** desde Google Play (Android) o la App Store
   (iPhone) e **inicia sesión con la misma cuenta**.
3. Comprueba que en los dos aparece **Connected** (conectado).

**Vas bien si…** en los dos dispositivos Tailscale dice conectado y con el mismo correo.

### Paso 2. Crea la dirección privada

1. Con Maestro **arrancado** (pulsa Start si no lo está), vuelve a la página de Maestro en
   Pinokio y pulsa **Secure Remote Access (Tailscale)**.
2. Acepta el permiso que pida el sistema (en Windows aparece una ventana de permisos: es
   **una sola vez**).
3. Si el navegador te pide activar HTTPS para tu red, acéptalo.

### Paso 3. Copia tu dirección

1. En Maestro, abre **Settings (el engranaje ⚙) → Notifications**.
2. Busca **Private phone access / Private HTTPS access**. Ahí está tu dirección, con esta
   pinta: `https://mi-pc.mi-red-privada.ts.net`

**Vas bien si…** ves una dirección que empieza por `https://` y termina en `.ts.net`.
Esa dirección es la tuya mientras el ordenador esté encendido y Maestro arrancado (si
algún día cambia el puerto, vuelve a pulsar **Secure Remote Access (Tailscale)** una vez
y listo). Puedes copiarla o escanear el código QR con el móvil.

### Paso 4. Ábrela en el móvil y entra

1. En el móvil, abre **Chrome** y escribe (o pega) esa dirección.
2. Escribe la contraseña de la Parte 2.

**Vas bien si…** ves Maestro en el móvil, con tus proyectos.

### Paso 5. Instálala como una app

- **Android (Chrome):** menú de los tres puntos **⋮** → **Install app** (o *Add to Home
  screen*).
- **iPhone/iPad (Safari):** botón **Compartir** → **Add to Home Screen** / *Añadir a
  pantalla de inicio*.

Luego ábrela desde el **icono de Maestro** del móvil: se ve a pantalla completa, como una
app normal, y ya no te pide la contraseña otra vez (dura 30 días).

### Paso 6 (opcional, pero muy cómodo). Avisos al terminar

En la app del móvil: **Settings → Notifications** → activa **System notifications** →
pulsa **Test notification**. Así el móvil te avisa cuando acaba un vídeo, aunque estés
haciendo otra cosa.

---

# PARTE 4 — Tu primer vídeo (todo desde la app)

1. En Maestro, arriba, elige **Studio** (o **Director** si quieres que la IA escriba el
   guion por ti).
2. Escribe qué quieres ver: por ejemplo *«un perro salchicha corriendo por una playa al
   atardecer, cámara lenta»*.
3. Elige un modelo de vídeo y pon una duración corta: si es tu primera vez, unos
   5 segundos (los clips largos tardan muchísimo).
4. Pulsa el botón de generar.

La primera vez tardará mucho más: está descargando el modelo (unos 18 GB). A partir de
ahí solo cuenta el tiempo de generación. Los resultados aparecen en la galería y quedan
en tu ordenador, en la carpeta `outputs/`.

**Truco:** si tu tarjeta es de 6–8 GB, empieza con clips cortos y resolución baja; el
propio Maestro ajusta los valores a tu tarjeta la primera vez.

---

# PARTE 4B — ¿Tu ordenador no puede generar? Cómputo público gratis

Si tu equipo no tiene una tarjeta NVIDIA (o es un Mac), Maestro **no se queda tirado**:
busca por su cuenta una máquina pública gratuita en internet y genera ahí. No hay que
instalar nada, ni crear ninguna cuenta, ni pegar ninguna clave.

1. Que Maestro esté en **Start** (como siempre).
2. Escribe tu prompt y pulsa generar, igual que en la Parte 4.
3. La primera generación puede tardar bastante: hay una **cola** de gente usando la misma
   máquina pública. El panel te va diciendo en qué punto estás.
4. El resultado aparece en tu galería y queda en tu ordenador, en `outputs/`.

**Vas bien si…** en **Studio → Compute & transfer → Public compute (internet)** aparece
una máquina pública conectada (con una bolita verde) y dice **free**.

### Tres cosas que conviene saber

- Es un motor **de otra persona**: usa su propio modelo, así que el vídeo se *parece* a lo
  que Maestro haría con tus ajustes, pero no es exactamente lo mismo. Por eso Maestro
  siempre prefiere tu ordenador o un ordenador con tarjeta que le indiques, y esta opción
  es la última.
- El tiempo gratis es **compartido y corto** (unos minutos por día). Si se agota, el
  trabajo **no se pierde**: se queda esperando y se reintenta solo.
- Si quieres más tiempo gratis, entra en el menú de Pinokio y pulsa **Connect Hugging Face
  (Optional)**: usa tu cuenta gratuita de Hugging Face (no es una cuenta de Maestro y no
  se paga). Todo sigue funcionando igual.

Detalle completo (en inglés): [`PUBLIC-COMPUTE.md`](PUBLIC-COMPUTE.md).

**Si tienes otro ordenador con tarjeta** y prefieres usarlo a él, es aún mejor: en
**Studio → Compute & transfer → Remote computers with a GPU** añade ese ordenador (su
nombre de Tailscale y la contraseña `MAESTRO_ACCESS_TOKEN` que pusiste en él). Maestro lo
usará automáticamente, le mandará tus fotos de referencia y te traerá el vídeo terminado.

---

# PARTE 5 (opcional, ya para nivel medio) — El motor local ComfyUI

Maestro puede además usar un motor propio (ComfyUI + Wan 2.2) que se instala en el mismo
ordenador. Es lo que usa la cola de trabajos de **Films**. Si solo quieres generar
vídeos sueltos, puedes saltarte esta parte.

1. En Maestro: **Films → Compute & transfer → Local engine**.
2. Pulsa **Re-detect**: mide tu equipo y te dice qué modelo te cabe.
3. Marca la casilla de los pesos (~18 GB) y pulsa **Install on this computer**. Nada se
   descarga si no marcas la casilla.
4. Copia el comando que el panel muestra para arrancar ComfyUI y pégalo en la consola de
   la carpeta de Maestro (como en la Parte 1, paso 4). Espera a que diga que escucha en
   `127.0.0.1:8188`.
5. Pulsa **Run the test**: genera un clip diminuto de verdad y lo reproduce. Esa es la
   prueba.
6. Pulsa **Register engine**. Ya puedes mandar trabajos a la cola: escribe el texto, elige
   los segundos y aparecerán en la lista de trabajos.

Detalle completo en [`COMFYUI-LOCAL.md`](COMFYUI-LOCAL.md).

---

# Si algo no funciona

| Lo que ves | Qué hacer |
| --- | --- |
| No aparece el botón **Open Web UI** | Espera un poco más y mira el texto de la ventana. Si dice «failed», pulsa **Update** en Pinokio y vuelve a **Start**. |
| El móvil dice que no puede llegar a tu ordenador | Comprueba tres cosas: ordenador **encendido**, Maestro en **Start** y Tailscale **conectado** en los dos dispositivos. |
| Olvidé la contraseña | Crea una nueva: `python scripts/maestro_access.py reset` (tendrás que volver a entrar en el móvil). |
| Chrome no me ofrece «Install app» | La dirección tiene que empezar por `https://` y hay que haber entrado con la contraseña antes. |
| Todo va lentísimo o da error de memoria | Tu tarjeta es pequeña: usa clips cortos, menos resolución y un modelo más ligero. |
| Un vídeo no se termina de montar | Seguramente falta **ffmpeg** en el ordenador. El doctor te lo dice y te da el enlace. |
| No tengo tarjeta NVIDIA y no sé qué hacer | No hagas nada: Maestro usa cómputo público gratis solo (Parte 4B). Si tarda, es la cola de la máquina pública, no un error. |
| Dice que se ha agotado el tiempo gratis | El trabajo se queda esperando y se reintenta solo. Para más tiempo gratis, **Connect Hugging Face (Optional)** en el menú de Pinokio. |
| Quiero empezar de cero | En Pinokio, **Reset**. Tus proyectos en `outputs/` se conservan. |
| Quiero ver qué está pasando | En Pinokio, la página **Logs** de Maestro. |

## La regla de oro

**Para generar: ordenador encendido + Maestro en Start + Tailscale conectado.**
Para mirar, editar o preparar cosas, el móvil se abre igual, aunque el ordenador esté
apagado (te lo dirá con un aviso claro). Los trabajos que se quedan esperando no se
pierden: se ejecutan cuando el motor vuelve.

---

# Resumen de una pantalla

1. Instala **Pinokio** → busca **Maestro** → **Install** → **Start** → **Open Web UI**.
2. Pon la contraseña: `python scripts/maestro_access.py enable` (desde la carpeta de Maestro).
3. Instala **Tailscale** en el ordenador y en el móvil con la misma cuenta.
4. En Pinokio pulsa **Secure Remote Access (Tailscale)** y copia la dirección `https://…ts.net`
   de **Settings → Notifications**.
5. Abre esa dirección en Chrome del móvil, entra con la contraseña y pulsa
   **⋮ → Install app**.
6. En Maestro, escribe un prompt y genera. La primera vez espera: baja el modelo (y, si
   tu ordenador no tiene tarjeta NVIDIA, genera en una máquina pública gratuita que
   Maestro conecta sola).
