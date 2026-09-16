# Roadmap

Estado de partida (16 sep 2026): prototipo funcional instalado como servicio de
usuario. Sondea la webcam cada 3 s, detecta caras con YuNet y arranca o apaga el
salvapantallas de Omarchy. Medido en producción:

| Métrica | Hoy | Objetivo tras fase 1 | Objetivo tras fase 2 |
|---|---|---|---|
| CPU media | 4,6 % de un núcleo | < 1 % | ~0 % mientras hay actividad de teclado/ratón |
| Captura por lectura | 1030 ms | < 500 ms | igual |
| Tiempo con la cámara abierta | 33 % | < 5 % | solo tras 30 s de inactividad |
| Conflicto con videollamadas | posible al abrir la app | ninguno | ninguno |
| Memoria | 117 MB | igual | igual |

Cada fase se cierra con: tests en verde, `--dry-run -v` durante unos minutos
sin comportamientos raros, y el servicio reinstalado con `./install.sh`.

---

## Fase 1 · Eficiencia y convivencia con la cámara — HECHA (16 sep 2026)

Cambios pequeños, todos en `auto_screensaver.py`. Se hicieron juntos.

**Resultado medido** (servicio en producción, 2 min en el estado más rápido, 2 s
por lectura con salvapantallas activo):

| Métrica | Antes (3 s fijo) | Después (2 s, peor caso) |
|---|---|---|
| CPU media | 4,6 % de un núcleo | 1,2 % de un núcleo |
| CPU por lectura | ~140 ms | ~24 ms |
| Memoria | 117 MB | 114 MB |

En el estado "persona delante" (12 s) el consumo esperado queda alrededor del 0,2 %.
La mayor parte del ahorro por lectura vino de limitar OpenCV a 2 hilos; el resto,
de sondear menos.

- 1.1 y 1.2 tal como se describen abajo. 18 tests en verde.
- 1.3 **descartado tras medir**: MJPG y 640×480 frente a 320×240 no cambian nada.
  El coste es el arranque del stream (~600 ms hasta el primer frame) y el cierre
  (~220 ms) de la C920, no el formato. Se dejó YUYV y 2 frames de calentamiento
  (la exposición es correcta, comprobado mirando el frame).
- 1.4 hecho.

### 1.1 Cadencia adaptativa
El intervalo depende del estado, no es fijo:

| Estado | Intervalo | Motivo |
|---|---|---|
| Persona delante, salvapantallas apagado | 12 s | No hay decisión urgente; la histéresis ya es de 3 lecturas |
| Nadie, salvapantallas apagado (contando ausencias) | 3 s | Queremos llegar al umbral en ~9 s como ahora |
| Salvapantallas activo | 2 s | Apagarlo rápido al volver |
| Sin webcam | 15 s | Nada que hacer; ahorrar intentos de apertura |

- `Controller.step()` devuelve también el intervalo recomendado, o expone `next_interval()`.
- Tests: uno por fila de la tabla.

### 1.2 Ceder la cámara a otras apps
Antes de abrir `/dev/videoN`, comprobar si otro proceso lo tiene abierto
(`fuser` o recorrer `/proc/*/fd`). Si es así, tratarlo como "sin webcam".
Elimina el fallo de "cámara en uso" al arrancar Meet o Zoom mientras el daemon
está leyendo.

- Registrar en INFO la primera vez que se cede y cuándo se recupera.
- Test con un `Webcam` falso que simule dispositivo ocupado.

### 1.3 Captura más barata
- Pedir formato MJPG (`CAP_PROP_FOURCC`) en vez de YUYV; la C920 lo soporta y
  reduce ancho de banda USB y tiempo de arranque del stream.
- Calentamiento de 4 frames a 2. Verificar con imágenes reales que la
  exposición sigue siendo suficiente para detectar; si no, volver a 3.
- Medir antes y después con el mismo snippet de `time.perf_counter()` usado hoy.

### 1.4 Limitar hilos de OpenCV
`cv2.setNumThreads(2)` al importar. La red es minúscula, 28 hilos solo añaden
contención y wakeups.

**Criterio de cierre:** CPU media < 1 % medido con `systemctl --user show
auto-screensaver -p CPUUsageNSec` durante 10 min con persona delante.

---

## Incidente 16 sep 2026 · cámara colgada — RESUELTO

A las 19:56 la C920 se colgó a nivel USB (`uvcvideo: Failed to set UVC probe
control : -110`). El daemon quedó en estado D dentro de `cap.read()`, inmune a
SIGKILL, y dejó de sondear durante 1,5 h: el salvapantallas no saltó al
ausentarse el usuario. Cambios:

- La captura corre en un proceso hijo (`--capture-worker`) con timeout
  (`capture_timeout`, 8 s). Si se cuelga, se mata, se avisa una vez y el daemon
  sigue a la cadencia `no_webcam`. Coste extra por lectura: ~1 MB por pipe, despreciable.
- Servicio `Type=notify` con `WatchdogSec=120` como segunda red.
- Configuración en `~/.config/auto-screensaver/config.toml` con recarga en
  caliente (adelantado de la fase 4). Permite subir `active` si la cadencia de
  2 s resulta agresiva para la cámara.

Pendiente de observar: si el cuelgue se repite, probar `active = 4` o mantener
el stream abierto solo mientras el salvapantallas está activo.

---

## Fase 2 · Cámara solo tras inactividad — HECHA (16 sep 2026)

**Implementado** con la opción 2 (cliente propio): pywayland en el venv y bindings
generados en `install.sh` desde el XML del sistema. swayidle no estaba en los
repos sincronizados y /dev/input requiere el grupo `input`. Probado en vivo:
`idled`/`resumed` llegan de Hyprland con el timeout configurado. `idle_timeout`
va en el config con recarga en caliente; 0 desactiva la fuente de input.

Corrección tras la primera prueba orgánica: al lanzar el salvapantallas Hyprland
emite `resumed` 65 ms después (artefacto por el mapeo de ventanas y el foco), la
siguiente lectura asumía presencia y lo apagaba al segundo. Mientras el
salvapantallas está activo la fuente de input se ignora y decide la cámara.

Idea: si hay actividad de teclado o ratón, la persona está delante por
definición. La cámara solo hace falta cuando llevas un rato sin tocar nada.

### 2.1 Fuente de inactividad
Omarchy expone `omarchy-shell idle status`, pero su umbral es el de
`idle.screensaver` (300 s), demasiado grueso. Opciones:

1. **swayidle como subproceso** (recomendada). `omarchy pkg add swayidle` y
   lanzarlo desde el daemon con `timeout 30 <marca idle> resume <marca activo>`,
   leyendo su salida por pipe. Usa `ext-idle-notify-v1`, que Hyprland soporta.
   Poco código, ya probado en el ecosistema.
2. **Cliente Wayland propio** con `pywayland` y el XML del protocolo. Sin
   dependencia externa pero más código y más frágil ante cambios del protocolo.

### 2.2 Comportamiento
- Activo (input en los últimos 30 s): `presence()` devuelve `True` sin abrir la
  cámara. Si el salvapantallas estuviera activo, se apaga igualmente
  (`omarchy-screensaver` ya lo hace con teclado, pero así también con ratón).
- Inactivo (≥ 30 s): se activa el sondeo de cámara de la fase 1.
- Si swayidle no está o falla: degradar a comportamiento de fase 1 y avisar en el log una vez.

### 2.3 Tests
`Controller` recibe un `idle_source` inyectable. Casos: activo sin cámara,
transición activo→inactivo arranca sondeo, swayidle caído degrada sin excepción.

**Criterio de cierre:** con uso normal del equipo, el LED de la webcam no se
encendería en ningún momento; solo tras 30 s de inactividad.

---

## Fase 3 · Detección más robusta

Solo si en el día a día aparecen falsos "nadie" (cabeza baja leyendo, de perfil,
poca luz).

### 3.1 Movimiento como segunda señal
Guardar el último frame en gris y reducido (160×120, blur). En cada lectura,
fracción de píxeles que cambian por encima de un umbral. Si supera un porcentaje
configurable, contar como presencia aunque no haya cara.
- Ojo: al reabrir la cámara cambia la exposición entre lecturas; normalizar
  brillo antes de comparar o aplicar un umbral generoso.
- Fase 2 hace esto menos necesario, porque solo se mira la cámara cuando ya no hay input.

### 3.2 Umbral de confianza dinámico
Bajar `score` con poca luz (media de brillo del frame) y subirlo con buena luz.

### 3.3 Detector de cuerpo (opcional)
Modelo ligero de personas en ONNX (nanodet o similar) como respaldo cuando no hay
cara. Coste de CPU 10 a 20 veces mayor que YuNet; solo compensa si 3.1 no basta.

**Criterio de cierre:** sesión de 30 min leyendo con la cabeza baja sin que
arranque el salvapantallas.

---

## Fase 4 · Plugin de Omarchy con interruptor en la barra

Objetivo final: un repo instalable con `omarchy plugin add <url>` que ponga un
indicador junto a Stay Awake para activar o desactivar la vigilancia por cámara,
y que lleve el daemon dentro. Investigado el 16 sep 2026 sobre el shell de Omarchy:

**Lo que permite el sistema de plugins**
- Un plugin es un repo git con `manifest.json` en la raíz, `kinds` entre
  `bar-widget`, `service`, `panel`... y QML de entrada. Se instala en
  `~/.config/omarchy/plugins/<id>/`, llega deshabilitado y el código se
  recarga al guardar. El instalador solo clona: sin hooks, sin sudo.
- Los widgets de terceros pueden usar `qs.Ui` (`BarIndicator`, `BarIconButton`),
  los mismos componentes con los que está hecho `StayAwake.qml` (14 líneas).
  Referencia local: `~/.config/omarchy/plugins/jankeesvw.notification-center`,
  que combina `service` + `bar-widget` y lleva un binario en `bin/`.
- El widget `omarchy.indicators` carga cada indicador desde
  `../indicators/<Id>.qml` de su propio directorio, así que no admite
  indicadores externos sin clonarlo. Mejor no clonarlo: un widget propio se
  coloca al lado con `omarchy bar put <id> --after omarchy.indicators`.

**Diseño**
1. **Interruptor por fichero de estado**, como hace Omarchy con
   `~/.local/state/omarchy/indicators/stay-awake`: el daemon lee
   `~/.local/state/auto-screensaver/disabled` en cada ciclo. Con el fichero
   presente no arranca el salvapantallas ni consulta la cámara (sigue apagándolo
   si te ve, igual que Stay Awake). Sirve tanto con el daemon en systemd como
   dentro del plugin, y da un comando CLI de regalo (`auto_screensaver.py toggle`).
2. **`Widget.qml`**: un `BarIndicator` con icono de cámara, `active` ligado al
   fichero via `FileView { watchChanges: true }`, `onPressed` lo crea o borra.
   Tooltip "Camera presence off/on". Sección `center`, junto a los indicadores.
3. **`Service.qml`** (`keepLoaded: true`): dueño de un `Process` que lanza
   `bin/auto-screensaver run`. Al cargarse ejecuta `bin/auto-screensaver setup`,
   que crea el venv, instala `requirements.txt`, descarga el modelo y genera los
   bindings Wayland si faltan. Así el plugin se autoabastece sin hooks de
   instalación. Reinicia el proceso si muere. La unidad systemd pasa a ser
   opcional (modo sin shell).
4. **Repo = plugin**: mover `auto_screensaver.py`, `models/`, `requirements.txt`
   a `bin/` o `daemon/`; `manifest.json` con `id` tipo `ruben.auto-screensaver`,
   `kinds: ["service", "bar-widget"]`, `barWidget.schema` con los ajustes
   principales (idle_timeout, absent_polls, intervals) para tenerlos en
   Ajustes > Barra además del TOML. `omarchy plugin validate .` en CI.
5. `--status` y notificación al ceder la cámara quedan como extras.

**Estado (16 sep 2026)**: pasos 1 y 2 hechos. `Switch` en el daemon con flag
`~/.local/state/auto-screensaver/disabled`, CLI `--toggle/--enable/--disable`
con notificación, y `plugin/` (manifest + `Widget.qml` sobre `BarIconButton`)
enlazado en `~/.config/omarchy/plugins/ruben.auto-screensaver`, en el centro de
la barra tras los indicadores. Verificado con captura de pantalla en ambos
estados y con el daemon registrando "camera presence switched off/on".
Hallazgo: `omarchy bar put --after` no respetó el ancla; `omarchy bar move
--section center --index 1` sí.

Pasos 3 y 4 (16 sep 2026): el repo es el plugin. `manifest.json` con
`kinds: ["service","bar-widget"]` y `keepLoaded`, `Service.qml` supervisa
`bin/auto-screensaver run` (setup idempotente del venv en
`~/.local/share/auto-screensaver`, porque el shell vigila el directorio de plugins
con `inotifywait -r`; reinicio con backoff 5→60 s; se aparta si la unidad systemd
está activa; IPC `status`/`restart`). El daemon vive en `daemon/`, la unidad
systemd queda como modo headless. Repo inicializado en git e instalado con
`omarchy plugin add <ruta> --enable --yes`.

**Riesgos**: el `Process` del shell hereda el entorno de `omarchy-shell`, no de
la sesión de login (comprobar `OMARCHY_PATH`, `NOTIFY_SOCKET` desaparece: sin
watchdog de systemd, el `Service.qml` debe hacer de supervisor). Actualizaciones
de Omarchy pueden cambiar `qs.Ui`; el clon de notification-center es la
referencia de qué API es estable de facto.

---

## Descartado por ahora

- **Inferencia en GPU.** La detección son 3 a 8 ms; el cuello de botella es la cámara.
- **Mantener la cámara abierta.** Bajaría el coste por lectura, pero bloquea a
  otras apps y deja el LED fijo. Va contra el objetivo de convivencia.
- **Apagar el panel (DPMS) en vez de salvapantallas.** Distinto objetivo; el
  salvapantallas ya cumple con el burn-in y Omarchy gestiona el bloqueo a los 900 s.

## Orden y esfuerzo estimado

| Fase | Esfuerzo | Depende de |
|---|---|---|
| 1 Eficiencia | 1 sesión corta | nada |
| 2 Inactividad | 1 sesión | 1 (para medir bien la mejora) |
| 3 Robustez | 1 sesión, solo si hace falta | 2 |
| 4 Plugin de Omarchy | 2 o 3 sesiones, por pasos | 1 |
