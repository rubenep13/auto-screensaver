# auto-screensaver

Plugin de Omarchy: revisa la webcam y usa la presencia de una persona para
arrancar o apagar el salvapantallas. Objetivo: prevenir burn-in en paneles OLED
sin depender solo del temporizador de inactividad.

Reglas:

1. Con teclado o ratón activos en los últimos 30 s, estás delante: la cámara no se toca.
2. Sin webcam, o webcam ocupada por otra app (videollamada): no hace nada.
3. Nadie delante durante 3 lecturas seguidas y salvapantallas inactivo: lo arranca.
4. Alguien delante y salvapantallas activo: lo apaga.

Respeta el bloqueo de sesión, el modo "Stay Awake" de Omarchy y el interruptor
propio de la barra (no arranca el salvapantallas en ninguno de los tres casos).
`omarchy toggle screensaver` también se respeta porque la activación pasa por
`omarchy-launch-screensaver`.

## Instalación

```bash
omarchy plugin add https://<tu-remoto>/auto-screensaver.git --enable
omarchy bar move ruben.auto-screensaver --section center --index 1   # junto a los indicadores
```

Al habilitarse, el servicio del plugin (`Service.qml`) ejecuta `bin/auto-screensaver run`.
La primera vez crea un entorno Python en `~/.local/share/auto-screensaver`
(OpenCV, pywayland, bindings del protocolo idle) y después arranca el daemon; si
el daemon muere lo relanza con espera creciente. Requisitos del sistema: `python3`
con `venv`, `wayland-protocols` (para la detección de inactividad; sin él funciona
solo con cámara) y una webcam UVC.

Operación:

```bash
journalctl --user -t auto-screensaver -f          # log del daemon
omarchy-shell ruben.auto-screensaver status       # estado del supervisor (JSON)
omarchy-shell ruben.auto-screensaver restart      # relanzar el daemon
bin/auto-screensaver status                       # resumen: interruptor, systemd, shell, venv
```

Si el plugin sustituye a un enlace simbólico que había en la misma ruta, el shell
puede conservar en caché el listado antiguo del directorio y fallar al cargar
`Service.qml` con "File name case mismatch"; `omarchy restart shell` lo arregla.

### Modo headless (systemd)

Sin el shell de Omarchy, o si prefieres el watchdog y el journal de systemd:

```bash
./install.sh      # unidad de usuario auto-screensaver.service
./uninstall.sh
```

Si la unidad está activa, `Service.qml` lo detecta y no lanza un segundo daemon.

### Desarrollo

`~/.config/omarchy/plugins/ruben.auto-screensaver` es un checkout git normal:
`omarchy plugin update ruben.auto-screensaver` trae los cambios. Para editar en
caliente, sustitúyelo por un enlace a tu clon; el shell recarga al guardar.
El venv nunca va dentro del plugin: el shell vigila ese directorio de forma
recursiva.

```bash
bin/auto-screensaver test      # tests unitarios
bin/auto-screensaver once      # una lectura, sin actuar
bin/auto-screensaver run --dry-run -v
```

## Interruptor en la barra

Un icono de cámara junto a los indicadores (Stay Awake, etc.). Un clic apaga o
enciende la vigilancia por cámara. El estado es un fichero,
`~/.local/state/auto-screensaver/disabled`, igual que hace Omarchy con Stay Awake:
con el fichero presente el daemon no consulta la cámara ni toca el salvapantallas.
También desde la terminal: `bin/auto-screensaver toggle` (o `enable` / `disable`).

## Configuración

`~/.config/auto-screensaver/config.toml`, a partir de
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

Los flags de `bin/auto-screensaver run` (`--interval-present`, `--absent-polls`,
`--score`, `--device`, `--capture-timeout`, `--idle-timeout`, `--config`) tienen
prioridad sobre el fichero.

## Cómo funciona

- Actividad de teclado y ratón: protocolo Wayland `ext-idle-notify-v1` vía
  pywayland. Respeta los inhibidores de inactividad: un vídeo a pantalla completa
  cuenta como actividad, igual que para el idle de Omarchy.
- Mientras el salvapantallas está activo, el input no cuenta: lanzarlo hace que
  Hyprland reporte actividad del asiento. En ese estado decide solo la cámara.
  Las teclas siguen cerrándolo porque `omarchy-screensaver` las lee él mismo.
- Detección de persona: OpenCV `FaceDetectorYN` (modelo YuNet, ~230 KB, CPU, 2 hilos).
  La cámara se abre y se cierra en cada lectura, y si otro proceso la tiene abierta
  se cede sin tocarla (comprobado recorriendo `/proc/*/fd`).
- La lectura de la cámara va en un proceso hijo con timeout (`capture_timeout`).
  Las webcam UVC pueden colgarse a nivel USB tras muchos ciclos
  (`uvcvideo: Failed to set UVC probe control : -110`); un proceso leyendo queda
  en estado D inmune a SIGKILL. El daemon lo mata, avisa una vez y sigue. La
  cámara se recupera desconectándola o con un reset USB:

  ```bash
  pkexec sh -c 'echo 0 > /sys/bus/usb/devices/<port>/authorized; sleep 2; echo 1 > /sys/bus/usb/devices/<port>/authorized'
  ```

- Salvapantallas activo: ventanas con clase `org.omarchy.screensaver` en
  `hyprctl clients`. No se usa `pgrep -f`: cualquier shell cuya línea de comandos
  contenga ese texto contaría como activo y sería víctima del `pkill`.
- Arrancar: `omarchy-launch-screensaver`. Parar: SIGTERM al script
  `omarchy-screensaver` hijo de cada ventana, cuyo trap restaura el cursor.
- Cadencia adaptativa: 12 s con persona delante, 3 s confirmando ausencia, 2 s con
  salvapantallas activo, 15 s sin cámara. Cada lectura cuesta ~900 ms de espera por
  la propia cámara, así que sondear menos es lo que ahorra CPU (~1 % en el peor caso).

## Estructura

```
manifest.json, Service.qml, Widget.qml   plugin del shell (supervisor + interruptor)
bin/auto-screensaver                     lanzador: setup | run | once | toggle | status | test
daemon/auto_screensaver.py               el daemon (lógica testeada en test_auto_screensaver.py)
daemon/models/                           modelo YuNet
systemd/, install.sh, uninstall.sh       modo headless
```

## Limitaciones

- Detecta caras, no cuerpos. De espaldas o muy girado cuenta como ausente.
- Tiempo hasta que salta el salvapantallas al irte: `idle_timeout` + hasta un
  intervalo `present` + `absent_polls × absent`; entre 40 y 55 s por defecto.
- El LED de la webcam parpadea en cada lectura, solo tras `idle_timeout` sin input.
