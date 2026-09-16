# auto-screensaver

Prototipo para Omarchy: revisa la webcam cada pocos segundos y usa la presencia
de una persona para arrancar o apagar el salvapantallas. Objetivo: prevenir
burn-in en paneles OLED sin depender solo del temporizador de inactividad.

Reglas:

1. Sin webcam, o webcam ocupada por otra app: no hace nada.
2. Nadie delante durante 3 lecturas seguidas (~9 s) y salvapantallas inactivo: lo arranca.
3. Alguien delante y salvapantallas activo: lo apaga.

Además respeta el bloqueo de sesión y el modo "stay awake" de Omarchy
(no arranca el salvapantallas en ninguno de los dos casos). El interruptor
`omarchy toggle screensaver` también se respeta porque la activación pasa por
`omarchy-launch-screensaver`.

## Cómo funciona

- Actividad de teclado y ratón: mientras haya input en los últimos `idle_timeout`
  segundos (30 por defecto) se asume presencia y la cámara no se toca. Se usa el
  protocolo Wayland `ext-idle-notify-v1` vía pywayland (bindings generados por
  `install.sh` en `protocols/`). Respeta los inhibidores de inactividad: un vídeo a
  pantalla completa cuenta como actividad, igual que para el idle de Omarchy. Si el
  compositor no ofrece el protocolo, se usa solo la cámara.
- Mientras el salvapantallas está activo, el input no cuenta: lanzarlo (ventanas
  nuevas, foco saltando de monitor) hace que Hyprland reporte actividad del asiento
  y la notificación de idle queda en "activo" durante todo un `idle_timeout`. En ese
  estado decide solo la cámara. Las teclas siguen cerrándolo directamente porque
  `omarchy-screensaver` las lee él mismo.
- Detección de persona: OpenCV `FaceDetectorYN` (modelo YuNet, ~230 KB, CPU, 2 hilos).
  La cámara se abre y se cierra en cada lectura para no bloquearla a otras apps, y si
  otro proceso ya la tiene abierta (videollamada) se cede sin tocarla y se trata como
  "sin webcam". Se comprueba recorriendo `/proc/*/fd`, sin herramientas externas.
- Cadencia adaptativa según estado:

  | Estado | Intervalo |
  |---|---|
  | Persona delante, salvapantallas apagado | 12 s |
  | Nadie, contando ausencias | 3 s |
  | Salvapantallas activo | 2 s |
  | Sin webcam o cámara cedida | 15 s |

  Cada lectura cuesta ~900 ms de espera por la propia cámara (arranque del stream y
  cierre), así que sondear menos es lo que ahorra CPU.
- Salvapantallas activo: hay ventanas con clase `org.omarchy.screensaver` en `hyprctl clients`.
  No se usa `pgrep -f` a propósito: cualquier shell cuya línea de comandos contenga ese
  texto contaría como activo y sería víctima del `pkill`.
- Arrancar: `omarchy-launch-screensaver`.
- Parar: SIGTERM al script `omarchy-screensaver` hijo de cada ventana, cuyo trap restaura el cursor y
  cierra las terminales en todos los monitores.

## Interruptor en la barra

`plugin/` es un plugin del shell de Omarchy con un widget de barra: un icono de
cámara junto a los indicadores (Stay Awake, etc.). Un clic apaga o encience la
vigilancia por cámara. El estado es un fichero,
`~/.local/state/auto-screensaver/disabled`, igual que hace Omarchy con Stay Awake:
con el fichero presente el daemon no consulta la cámara ni toca el salvapantallas.
También desde la terminal:

```bash
.venv/bin/python auto_screensaver.py --toggle   # o --enable / --disable
```

Instalación del widget (hasta que el repo sea el plugin):

```bash
ln -sfn "$PWD/plugin" ~/.config/omarchy/plugins/ruben.auto-screensaver
omarchy-shell shell rescanPlugins
omarchy plugin enable ruben.auto-screensaver
omarchy bar move ruben.auto-screensaver --section center --index 1
```

## Configuración

`~/.config/auto-screensaver/config.toml`, creado por `install.sh` a partir de
[config.example.toml](config.example.toml). Se recarga solo al guardarlo.

```toml
absent_polls = 3        # lecturas seguidas sin nadie antes de arrancar
score = 0.6             # confianza mínima del detector de caras
capture_timeout = 8.0   # segundos antes de dar la cámara por colgada
idle_timeout = 30       # segundos sin teclado/ratón antes de consultar la cámara (0 = siempre cámara)
# device = "/dev/video0"

[intervals]             # segundos entre lecturas según estado
present = 12
absent = 3
active = 2
no_webcam = 15
```

Los flags de línea de comandos (`--interval-present`, `--absent-polls`, `--score`,
`--device`, `--capture-timeout`, `--idle-timeout`, `--config`) tienen prioridad sobre el fichero.

## Cámara colgada

Las webcam UVC como la C920 pueden quedarse colgadas a nivel USB tras muchos
ciclos de abrir y cerrar; el kernel lo muestra como
`uvcvideo: Failed to set UVC probe control : -110`. Un proceso leyendo en ese
momento se queda en estado D y ni SIGKILL lo saca. Por eso la lectura de la
cámara va en un proceso hijo con timeout: si no responde en `capture_timeout`
segundos, el daemon lo mata, avisa una vez en el log y sigue sondeando a la
cadencia de `no_webcam`. La cámara se recupera desconectándola y conectándola,
o con un reset USB:

```bash
# <port> es el directorio de la cámara, p. ej. 1-8 (ver `ls -l /sys/bus/usb/devices`)
pkexec sh -c 'echo 0 > /sys/bus/usb/devices/<port>/authorized; sleep 2; echo 1 > /sys/bus/usb/devices/<port>/authorized'
```

Además el servicio es `Type=notify` con `WatchdogSec=120`: si el bucle principal
se parase por cualquier otra causa, systemd lo reinicia.

## Instalación

```bash
./install.sh          # venv + modelo + servicio systemd de usuario
journalctl --user -u auto-screensaver -f
./uninstall.sh
```

## Pruebas manuales

```bash
.venv/bin/python auto_screensaver.py --once        # una lectura, sin actuar
.venv/bin/python auto_screensaver.py --dry-run -v  # bucle, solo registra
.venv/bin/python -m unittest -q test_auto_screensaver
```

`--once` también imprime qué fichero de configuración se está usando y los valores efectivos.

## Limitaciones del prototipo

- Detecta caras, no cuerpos. Si estás de espaldas o muy lejos cuenta como ausente.
- El LED de la webcam parpadea en cada lectura, pero solo cuando llevas `idle_timeout` segundos sin tocar teclado ni ratón.
- Tiempo hasta que salta el salvapantallas al irte: `idle_timeout` + hasta un intervalo `present` + `absent_polls × absent`. Con los valores por defecto, entre 40 y 55 s. Baja `idle_timeout` si lo quieres más rápido.
- Con poca luz la detección empeora; baja `--score` si hay falsos negativos.
