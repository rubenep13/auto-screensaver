# Camera presence

An [Omarchy](https://omarchy.org) plugin that uses your webcam to drive the
screensaver. Walk away and the screensaver comes up within seconds instead of
minutes; sit back down and it is gone before you touch the keyboard. Built to
keep OLED panels from burning in, without staring at you all day: the camera is
only consulted once you have stopped typing and moving the mouse.

<img src="preview.png" alt="The Omarchy bar with the camera presence switch between the indicators and the clock" width="720">

## Install

```bash
omarchy plugin add https://github.com/rubenep13/auto-screensaver.git --enable
omarchy bar move rubenep13.auto-screensaver --section center --index 1   # next to the indicators
```

On first start the plugin builds a Python environment in
`~/.local/share/auto-screensaver` (OpenCV, pywayland) and then runs the daemon.
That takes a minute with a cold pip cache; the bar icon shows `daemon setup`
in its tooltip meanwhile.

Needs `python3` with `venv`, a UVC webcam, and `wayland-protocols` (present on
a stock Omarchy) for the keyboard/mouse idle detection. Without it the plugin
still works, using the camera alone.

Remove with `omarchy plugin remove rubenep13.auto-screensaver`. That stops the
daemon; the Python environment stays in `~/.local/share/auto-screensaver`
until you delete it.

## What it does

Every few seconds the daemon decides between three states and acts only on a
change:

- **You are typing or moving the mouse.** You are obviously there. The camera
  is not touched.
- **No input for 30 s.** One frame is grabbed and run through a small face
  detector (YuNet, ~230 KB, CPU, a few milliseconds). Nobody in three
  consecutive frames and the screensaver is started through
  `omarchy-launch-screensaver`, exactly as Omarchy's own idle timer would.
- **Screensaver up and a face appears.** The screensaver is dismissed.

It stands aside when the session is locked, when Omarchy's *Stay Awake* is on,
when `omarchy toggle screensaver` has disabled the screensaver, and when
another application (a video call) has the camera open. It never keeps the
camera open: one grab, then release, so the LED blinks briefly rather than
staying lit.

## The bar switch

A camera icon sits next to the indicators. Click it to switch camera presence
off (dimmed, crossed-out camera) or back on. The state is a flag file,
`~/.local/state/auto-screensaver/disabled`, the same mechanism Omarchy uses for
*Stay Awake*, so the daemon, the widget and the command line always agree:

```bash
~/.config/omarchy/plugins/rubenep13.auto-screensaver/bin/auto-screensaver toggle   # or enable / disable
```

Off means hands off: no camera, no screensaver actions. Keyboard presses still
dismiss the screensaver, as they always do.

## Configuration

`~/.config/auto-screensaver/config.toml`, reloaded when it changes. Copy
[config.example.toml](config.example.toml) to start from the defaults:

```toml
idle_timeout = 30       # seconds without keyboard/mouse before the camera is consulted (0 = always)
absent_polls = 3        # consecutive empty frames before starting the screensaver
score = 0.6             # face detector confidence, 0-1; lower it for dim rooms
capture_timeout = 8.0   # seconds before a camera read counts as hung
# device = "/dev/video0"

[intervals]             # seconds between polls, by state
present = 12
absent = 3
active = 2
no_webcam = 15
```

Time from leaving the desk to screensaver is `idle_timeout` plus up to one
`present` interval plus `absent_polls × absent`: 40 to 55 s with the defaults.
Lower `idle_timeout` for a quicker reaction; raise `intervals.present` if the
LED blinking every 12 s bothers you.

## Operating it

```bash
journalctl --user -t auto-screensaver -f                       # daemon log
omarchy-shell rubenep13.auto-screensaver status                # supervisor state as JSON
omarchy-shell rubenep13.auto-screensaver restart               # relaunch the daemon
~/.config/omarchy/plugins/rubenep13.auto-screensaver/bin/auto-screensaver status
~/.config/omarchy/plugins/rubenep13.auto-screensaver/bin/auto-screensaver once  # one poll, no action
```

**CPU.** About 1 % of one core while the screensaver is up (polling every 2 s)
and near zero while you are working, because the camera is not used at all
then. Almost all of the per-poll cost is the camera starting its stream
(~600 ms on a Logitech C920); detection itself is 3 to 8 ms.

**If the camera hangs.** UVC webcams can lock up at USB level after many
open/close cycles; the kernel logs
`uvcvideo: Failed to set UVC probe control : -110`. The camera is read in a
child process with a timeout precisely for this: the daemon kills it, warns
once in the log and keeps going. Unplug and replug the camera, or reset it
from a root shell by de-authorizing and re-authorizing the USB port:

```bash
# <port> is the camera's directory under /sys/bus/usb/devices, e.g. 1-8
echo 0 > /sys/bus/usb/devices/<port>/authorized
sleep 2
echo 1 > /sys/bus/usb/devices/<port>/authorized
```

The plugin itself never elevates privileges: no sudo or pkexec is required or
used by any of its code.

## Headless mode

Without the Omarchy shell, or if you prefer systemd's watchdog and journal,
run the daemon as a user service instead:

```bash
./install.sh      # installs auto-screensaver.service; logs in journalctl --user -u auto-screensaver
./uninstall.sh
```

When that unit is active the plugin's service notices and does not start a
second daemon.

## How it is built

- `Service.qml` runs inside `omarchy-shell` and supervises `bin/auto-screensaver run`,
  restarting it with a growing backoff if it dies.
- `Widget.qml` is the bar switch, a `BarIconButton` like Omarchy's own indicators.
- `daemon/auto_screensaver.py` is the daemon. Its decision logic is a small,
  dependency-free class covered by unit tests (`bin/auto-screensaver test`).
- Keyboard/mouse activity comes from the Wayland `ext-idle-notify-v1` protocol
  via pywayland. It honours idle inhibitors, so a fullscreen video counts as
  activity, as it does for Omarchy's own idle. While the screensaver is up,
  input is ignored on purpose: launching it makes the compositor report
  activity, and the camera alone decides until it is gone.
- Screensaver state is read from Hyprland's window list, never from process
  names, and it is stopped by signalling the `omarchy-screensaver` script so
  its own cleanup (cursor, terminals on every monitor) runs.
- The Python environment lives outside the plugin directory because the shell
  watches `~/.config/omarchy/plugins` recursively.

## Security and privacy

Omarchy plugins run unsandboxed inside the shell; read the code before
enabling it. This plugin runs a Python daemon as your user, opens your webcam
for one frame at a time, and starts or stops the Omarchy screensaver. Frames
are processed in memory and never written to disk or sent anywhere. Network is
used once, by `pip`, to install OpenCV and pywayland into
`~/.local/share/auto-screensaver`.

## Limitations

- Detects faces, not bodies. Sitting sideways or with your head down for a
  long read counts as absent; raise `absent_polls` or lower `score` if that
  bites.
- One camera at a time: the first `/dev/video*` that returns a frame, unless
  `device` is set.
- Tested on Omarchy with Hyprland and a Logitech C920. Other UVC cameras
  should work; other compositors are not supported (it relies on `hyprctl`).

## License

[MIT](LICENSE)
