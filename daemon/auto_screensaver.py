#!/usr/bin/env python3
"""auto-screensaver: webcam presence drives the Omarchy screensaver.

Every few seconds grab one frame from the webcam and look for a face.
  - No webcam (or busy)                       -> do nothing
  - Nobody for N polls, screensaver inactive  -> start the screensaver
  - Somebody present, screensaver active      -> stop the screensaver
Purpose: OLED burn-in prevention while you're away from the desk.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

os.environ.setdefault("OMARCHY_PATH", "/usr/share/omarchy")
os.environ.setdefault("OPENCV_LOG_LEVEL", "OFF")
os.environ.setdefault("OPENCV_VIDEOIO_DEBUG", "0")

HERE = Path(__file__).resolve().parent
MODEL_PATH = HERE / "models" / "face_detection_yunet_2023mar.onnx"
# Generated Wayland bindings (protocols/) and the venv live outside the plugin
# checkout: omarchy-shell watches ~/.config/omarchy/plugins recursively and a
# venv in there would trigger endless plugin reloads.
DATA_DIR = Path(os.environ.get("AUTO_SCREENSAVER_DATA")
                or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "auto-screensaver")
for _p in (DATA_DIR, HERE):
    if (_p / "protocols").is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
STAY_AWAKE_FLAG = Path.home() / ".local/state/omarchy/indicators/stay-awake"
SCREENSAVER_CLASS = "org.omarchy.screensaver"

NONE, START, STOP = "none", "start", "stop"

# Seconds between polls, by state. Nothing is urgent while somebody is at the
# desk; dismissing an active screensaver is what has to feel instant.
DEFAULT_INTERVALS = {"present": 12.0, "absent": 3.0, "active": 2.0, "no_webcam": 15.0}

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "auto-screensaver" / "config.toml"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "auto-screensaver"
DISABLED_FLAG = STATE_DIR / "disabled"  # the bar switch: present = camera presence off
DEFAULT_CONFIG = {"absent_polls": 3, "score": 0.6, "device": None, "capture_timeout": 8.0, "idle_timeout": 30.0}

log = logging.getLogger("auto-screensaver")


# --------------------------------------------------------------------------- #
# Decision logic (pure, unit tested)
# --------------------------------------------------------------------------- #
class Controller:
    """presence() -> True / False / None (None = no webcam available)."""

    def __init__(self, presence, screensaver, stay_awake, absent_polls=3, dry_run=False, intervals=None,
                 disabled=lambda: False):
        self.presence = presence
        self.screensaver = screensaver
        self.stay_awake = stay_awake
        self.disabled = disabled
        self.absent_polls = max(1, int(absent_polls))
        self.dry_run = dry_run
        self.intervals = dict(DEFAULT_INTERVALS, **(intervals or {}))
        self.absent_streak = 0
        self.last_present = None
        self.last_active = False
        self.stepped = False
        self.paused = False

    def configure(self, intervals=None, absent_polls=None) -> None:
        if intervals:
            self.intervals.update({k: float(v) for k, v in intervals.items() if k in DEFAULT_INTERVALS})
        if absent_polls is not None:
            self.absent_polls = max(1, int(absent_polls))

    def next_interval(self) -> float:
        """Seconds to wait before the next poll, based on the last observed state."""
        if not self.stepped:
            return self.intervals["absent"]
        if self.paused or self.last_present is None:
            return self.intervals["no_webcam"]
        if self.last_active:
            return self.intervals["active"]
        return self.intervals["present" if self.last_present else "absent"]

    def step(self) -> str:
        self.stepped = True

        # The bar switch: off means hands off, no camera, no actions. Keyboard
        # still dismisses the screensaver through omarchy-screensaver itself.
        if self.disabled():
            if not self.paused:
                log.info("camera presence switched off")
                self.paused = True
                self.absent_streak = 0
                self.last_present = None
            return NONE
        if self.paused:
            log.info("camera presence switched on")
            self.paused = False

        # Screensaver state first: presence() consults it to decide whether
        # keyboard/mouse activity can be trusted (see make_presence). It must
        # reflect the truth even when something else launched the screensaver.
        active = self.last_active = self.screensaver.active()

        present = self.presence()
        if present != self.last_present:
            log.info("presence: %s", {True: "person", False: "nobody", None: "no webcam"}[present])
            self.last_present = present

        if present is None:
            self.absent_streak = 0
            return NONE

        self.absent_streak = self.absent_streak + 1 if not present else 0

        if self.screensaver.locked():
            log.debug("session locked, skipping")
            return NONE

        action = NONE
        if present and active:
            action = STOP
        elif not present and not active and self.absent_streak >= self.absent_polls and not self.stay_awake():
            action = START

        if action != NONE:
            log.info("%s screensaver%s", action, " (dry run)" if self.dry_run else "")
            if not self.dry_run:
                getattr(self.screensaver, action)()
                active = action == START
        self.last_active = active
        return action


# --------------------------------------------------------------------------- #
# Configuration file (~/.config/auto-screensaver/config.toml)
# --------------------------------------------------------------------------- #
def load_config(path: Path = CONFIG_PATH) -> dict:
    """Defaults overlaid with the TOML file. Unknown keys are ignored; an
    unreadable or malformed file is reported once and treated as absent."""
    cfg = {"intervals": dict(DEFAULT_INTERVALS), **DEFAULT_CONFIG}
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return cfg
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("config %s ignored: %s", path, exc)
        return cfg
    for key in DEFAULT_CONFIG:
        if key in data:
            cfg[key] = data[key]
    for key, value in (data.get("intervals") or {}).items():
        if key in DEFAULT_INTERVALS:
            cfg["intervals"][key] = float(value)
    return cfg


class Switch:
    """On/off switch shared with the bar widget through a flag file, the way
    Omarchy's Stay Awake works: file present = camera presence disabled."""

    def __init__(self, path: Path = DISABLED_FLAG):
        self.path = Path(path)

    def disabled(self) -> bool:
        return self.path.exists()

    def enabled(self) -> bool:
        return not self.disabled()

    def set_enabled(self, on: bool) -> bool:
        if on:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()
        return on

    def toggle(self) -> bool:
        return self.set_enabled(not self.enabled())


def notify(text: str, icon: str = "󰄀") -> None:
    if shutil.which("omarchy-notification-send"):
        _run(["omarchy-notification-send", "-g", icon, text], timeout=5)


def sd_notify(message: str) -> None:
    """Tell systemd we're alive (no-op outside a Type=notify service)."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode(), addr)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Omarchy screensaver control
# --------------------------------------------------------------------------- #
def _run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("command failed %s: %s", cmd, exc)
        return None


def _cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        return []


def _children(pid: int) -> list[int]:
    r = _run(["pgrep", "-P", str(pid)])
    return [int(p) for p in (r.stdout.split() if r else [])]


def _kill(pid: int, sig=signal.SIGTERM) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except OSError as exc:
        log.warning("kill %s failed: %s", pid, exc)


class OmarchyScreensaver:
    """Screensaver state comes from Hyprland's window list, never from process
    name matching: a shell whose command line merely mentions the class must
    not count as an active screensaver (nor get killed by stop())."""

    def windows(self) -> list[dict]:
        r = _run(["hyprctl", "clients", "-j"], timeout=5)
        if not r or r.returncode != 0:
            return []
        try:
            clients = json.loads(r.stdout)
        except ValueError:
            return []
        return [c for c in clients if SCREENSAVER_CLASS in (c.get("class"), c.get("initialClass"))]

    def active(self) -> bool:
        return bool(self.windows())

    def locked(self) -> bool:
        if shutil.which("omarchy-shell"):
            r = _run(["omarchy-shell", "lock", "isLocked"], timeout=5)
            if r and r.returncode == 0 and r.stdout.strip() in ("true", "false"):
                return r.stdout.strip() == "true"
        r = _run(["omarchy-hyprland-session-locked"], timeout=5)
        return bool(r and r.returncode == 0)

    def start(self) -> None:
        r = _run(["omarchy-launch-screensaver"], timeout=20)
        if r and r.returncode != 0:
            log.warning("omarchy-launch-screensaver exit %s: %s", r.returncode, (r.stderr or r.stdout).strip())

    def stop(self) -> None:
        # SIGTERM the omarchy-screensaver script running inside each screensaver
        # terminal: its trap restores the cursor and closes every terminal.
        terminal_pids = {int(c["pid"]) for c in self.windows() if int(c.get("pid", 0)) > 0}
        scripts = [child for pid in terminal_pids for child in _children(pid)
                   if (_cmdline(child) or [""])[-1].endswith("omarchy-screensaver")]
        for pid in scripts:
            _kill(pid, signal.SIGTERM)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not self.active():
                return
            time.sleep(0.1)

        log.warning("screensaver still alive after SIGTERM, closing its terminals")
        for pid in terminal_pids:
            _kill(pid, signal.SIGTERM)
        _run(["hyprctl", "keyword", "cursor:invisible", "false"])


def omarchy_stay_awake() -> bool:
    return STAY_AWAKE_FLAG.exists()


# --------------------------------------------------------------------------- #
# Webcam + face detection
# --------------------------------------------------------------------------- #
def device_in_use(dev: str) -> int | None:
    """PID of another process holding `dev` open, or None. Scans /proc so it
    works without external tools; processes of other users are simply invisible."""
    target = os.path.realpath(dev)
    me = os.getpid()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            for fd in os.scandir(f"/proc/{entry.name}/fd"):
                try:
                    if os.readlink(fd.path) == target:
                        return int(entry.name)
                except OSError:
                    continue
        except OSError:
            continue
    return None


def _process_name(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm") as fh:
            return fh.read().strip()
    except OSError:
        return "?"


def _configure_cv2(cv2):
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except AttributeError:
        pass
    cv2.setNumThreads(2)  # the model is tiny; 28 threads only add wakeups


def _read_exact(fd: int, n: int, deadline: float) -> bytes:
    chunks, got = [], 0
    while got < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise TimeoutError
        chunk = os.read(fd, min(1 << 20, n - got))
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _read_line(fd: int, deadline: float) -> bytes:
    line = bytearray()
    while not line.endswith(b"\n"):
        line += _read_exact(fd, 1, deadline)
    return bytes(line)


class CaptureWorker:
    """Runs the actual V4L2 reads in a child process. A UVC camera that hangs
    at USB level leaves the reader stuck in the kernel (state D, immune even to
    SIGKILL); keeping that reader out of the daemon means the daemon survives,
    reports the problem and carries on when the camera comes back."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None

    def _ensure(self) -> subprocess.Popen:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--capture-worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            log.debug("capture worker pid %d", self.proc.pid)
        return self.proc

    def grab(self, dev: str, timeout: float, width=640, height=480, warmup=2):
        """ndarray frame, None if the device gave nothing, TimeoutError if it hung."""
        import numpy as np
        proc = self._ensure()
        deadline = time.monotonic() + timeout
        try:
            proc.stdin.write(f"{dev} {width} {height} {warmup}\n".encode())
            proc.stdin.flush()
            out = proc.stdout.fileno()
            header = _read_line(out, deadline).split()
            if header[:1] != [b"OK"]:
                return None
            h, w, c = (int(x) for x in header[1:4])
            raw = _read_exact(out, h * w * c, deadline)
        except TimeoutError:
            raise  # TimeoutError is an OSError; the caller handles it explicitly
        except (EOFError, OSError, ValueError):
            log.warning("capture worker died, restarting it")
            self.kill()
            return None
        return np.frombuffer(raw, dtype=np.uint8).reshape(h, w, c)

    def kill(self) -> None:
        if self.proc is not None:
            _kill(self.proc.pid, signal.SIGKILL)
            self.proc = None


def _capture(cv2, dev: str, width: int, height: int, warmup: int):
    """One open/read/close cycle on `dev`. Runs inside the worker process."""
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    try:
        if not cap.isOpened():
            return None
        # Measured on a C920: ~600 ms until the first frame and ~220 ms to
        # release, whatever the pixel format. The per-grab cost is hardware
        # bound; polling less often (see DEFAULT_INTERVALS) is what saves CPU.
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        frame = None
        for _ in range(warmup):
            ok, f = cap.read()
            if ok and f is not None and f.size:
                frame = f
        return frame
    finally:
        cap.release()


def capture_worker_main() -> int:
    import cv2
    import numpy as np
    _configure_cv2(cv2)
    out = sys.stdout.buffer
    for line in sys.stdin:
        parts = line.split()
        if len(parts) != 4:
            continue
        dev, width, height, warmup = parts[0], int(parts[1]), int(parts[2]), int(parts[3])
        frame = _capture(cv2, dev, width, height, warmup)
        if frame is None:
            out.write(b"NONE\n")
        else:
            h, w, c = frame.shape
            out.write(f"OK {h} {w} {c}\n".encode())
            out.write(np.ascontiguousarray(frame).tobytes())
        out.flush()
    return 0


HUNG_HINT = ("no answer in %.0fs: the USB camera is probably hung. Unplug and replug it, or reset it with "
             "echo 0 > /sys/bus/usb/devices/<port>/authorized && echo 1 > /sys/bus/usb/devices/<port>/authorized")


class Webcam:
    """Finds a working /dev/video*, opens it per poll so other apps (video
    calls) can still use it, and never opens it while somebody else has it: a
    call app that is starting up must not find the device busy because of us."""

    def __init__(self, device: str | None = None, width=640, height=480, warmup=2,
                 in_use=device_in_use, worker=None, capture_timeout=8.0):
        self.device = device
        self.width, self.height, self.warmup = width, height, warmup
        self.in_use = in_use
        self.worker = worker or CaptureWorker()
        self.capture_timeout = capture_timeout
        self.last_good: str | None = None
        self.busy_pid: int | None = None
        self.busy_dev: str | None = None
        self.hung = False

    def candidates(self) -> list[str]:
        if self.device:
            return [self.device]
        devs = sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p) or 0))
        if self.last_good in devs:
            devs.remove(self.last_good)
            devs.insert(0, self.last_good)
        return devs

    def grab(self):
        for dev in self.candidates():
            if not os.access(dev, os.R_OK | os.W_OK):
                continue
            holder = self.in_use(dev)
            if holder:
                if (self.busy_dev, self.busy_pid) != (dev, holder):
                    log.info("webcam %s in use by %s (pid %d), yielding", dev, _process_name(holder), holder)
                    self.busy_dev, self.busy_pid = dev, holder
                continue
            if self.busy_dev == dev:
                log.info("webcam %s free again", dev)
                self.busy_dev, self.busy_pid = None, None

            try:
                frame = self.worker.grab(dev, self.capture_timeout, self.width, self.height, self.warmup)
            except TimeoutError:
                self.worker.kill()
                if not self.hung:
                    log.warning("webcam %s " + HUNG_HINT, dev, self.capture_timeout)
                self.hung = True
                self.last_good = None
                return None

            if frame is not None:
                if self.hung:
                    log.info("webcam %s answering again", dev)
                self.hung = False
                if self.last_good != dev:
                    log.info("using webcam %s", dev)
                    self.last_good = dev
                return frame

        if self.last_good is not None:
            log.info("webcam unavailable")
            self.last_good = None
        return None


class FaceDetector:
    def __init__(self, model_path: Path = MODEL_PATH, score_threshold=0.6, max_width=640):
        import cv2
        _configure_cv2(cv2)
        if not model_path.exists():
            sys.exit(f"missing model {model_path}; run ./install.sh")
        self.cv2 = cv2
        self.max_width = max_width
        self.score = score_threshold
        self.det = cv2.FaceDetectorYN.create(str(model_path), "", (320, 320), score_threshold, 0.3, 5000)

    def set_score(self, score: float) -> None:
        if score != self.score:
            self.det.setScoreThreshold(float(score))
            self.score = score

    def faces(self, frame) -> int:
        h, w = frame.shape[:2]
        if w > self.max_width:
            scale = self.max_width / w
            frame = self.cv2.resize(frame, (self.max_width, int(h * scale)))
            h, w = frame.shape[:2]
        self.det.setInputSize((w, h))
        _, found = self.det.detect(frame)
        return 0 if found is None else len(found)


# --------------------------------------------------------------------------- #
# Keyboard/mouse idleness via Wayland ext-idle-notify-v1
# --------------------------------------------------------------------------- #
class WaylandIdleMonitor:
    """Background thread that asks the compositor whether the seat has been
    idle for `timeout` seconds. `available` is False until connected (or when
    the compositor lacks the protocol); callers then fall back to the camera.

    Uses get_idle_notification, which honours idle inhibitors: a fullscreen
    video keeps the seat "active", so no screensaver interrupts a movie. That
    matches Omarchy's own idle service."""

    def __init__(self, timeout: float):
        self.timeout = float(timeout)
        self.idle = False
        self.available = False
        self._pending: float | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned = False

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="idle-monitor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def set_timeout(self, timeout: float) -> None:
        with self._lock:
            self._pending = float(timeout)

    def _take_pending(self) -> float | None:
        with self._lock:
            pending, self._pending = self._pending, None
        return pending

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            if self.timeout <= 0:
                self.available = False
                pending = self._take_pending()
                if pending is not None:
                    self.timeout = pending
                else:
                    self._stop.wait(1.0)
                continue
            try:
                self._session()
                backoff = 2.0
            except Exception as exc:  # compositor gone, protocol missing, pywayland absent...
                self.available = False
                (log.debug if self._warned else log.warning)(
                    "idle monitor unavailable (%s: %s); using the camera only, retry in %.0fs",
                    type(exc).__name__, exc, backoff)
                self._warned = True
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)

    def _session(self) -> None:
        from pywayland.client import Display
        from protocols.ext_idle_notify_v1 import ExtIdleNotifierV1
        from protocols.wayland import WlSeat

        display = Display()
        display.connect()
        try:
            found: dict = {}

            def on_global(registry, name, interface, version):
                if interface == "ext_idle_notifier_v1":
                    found["notifier"] = registry.bind(name, ExtIdleNotifierV1, min(version, 1))
                elif interface == "wl_seat" and "seat" not in found:
                    found["seat"] = registry.bind(name, WlSeat, min(version, 7))

            registry = display.get_registry()
            registry.dispatcher["global"] = on_global
            display.roundtrip()
            if "notifier" not in found or "seat" not in found:
                raise RuntimeError("compositor offers no ext_idle_notifier_v1/wl_seat")

            notification = None

            def on_idled(*_):
                self.idle = True
                log.info("no input for %gs, watching the camera", self.timeout)

            def on_resumed(*_):
                self.idle = False
                log.info("input activity resumed")

            def arm(timeout: float):
                nonlocal notification
                if notification is not None:
                    notification.destroy()
                notification = found["notifier"].get_idle_notification(int(timeout * 1000), found["seat"])
                notification.dispatcher["idled"] = on_idled
                notification.dispatcher["resumed"] = on_resumed
                self.idle = False

            arm(self.timeout)
            display.flush()
            self.available = True
            self._warned = False
            log.info("idle monitor ready (idle_timeout=%gs)", self.timeout)

            fd = display.get_fd()
            while not self._stop.is_set():
                pending = self._take_pending()
                if pending is not None and pending != self.timeout:
                    self.timeout = pending
                    if pending <= 0:
                        return  # _run parks until a positive timeout arrives
                    arm(pending)
                    log.info("idle monitor re-armed (idle_timeout=%gs)", pending)
                display.flush()
                if select.select([fd], [], [], 0.5)[0]:
                    display.read()
                    display.dispatch(block=False)
        finally:
            self.available = False
            try:
                display.disconnect()
            except Exception:
                pass


def make_presence(webcam: Webcam, detector: FaceDetector, idle=None, screensaver_active=lambda: False):
    """Keyboard/mouse activity in the last idle_timeout seconds means somebody
    is there, no camera needed. Only once the seat has gone idle (or the idle
    monitor is unavailable) do we look through the webcam.

    While the screensaver is up, input is not trusted: launching it (windows
    mapping, focus hopping across monitors) makes the compositor report seat
    activity, and the idle notification then stays "active" for a whole
    idle_timeout. Omarchy's idle service works around the same artifact. The
    camera decides until the screensaver is gone; keyboard presses still
    dismiss it directly through omarchy-screensaver itself."""
    def presence():
        if idle is not None and idle.available and not idle.idle and not screensaver_active():
            log.debug("input activity, skipping camera")
            return True
        frame = webcam.grab()
        if frame is None:
            return None
        n = detector.faces(frame)
        log.debug("faces: %d", n)
        return n > 0
    return presence


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=f"Command-line options override {CONFIG_PATH}, which is reloaded when it changes.")
    p.add_argument("--config", type=Path, default=CONFIG_PATH, help="TOML config file (default %(default)s)")
    for state, default in DEFAULT_INTERVALS.items():
        p.add_argument(f"--interval-{state.replace('_', '-')}", type=float, default=None, dest=f"interval_{state}",
                       help=f"seconds between polls while state is '{state}' (default {default:g})")
    p.add_argument("--absent-polls", type=int, default=None, help="consecutive empty polls before starting (default 3)")
    p.add_argument("--score", type=float, default=None, help="face score threshold 0-1 (default 0.6)")
    p.add_argument("--device", default=None, help="force a webcam device, e.g. /dev/video0")
    p.add_argument("--capture-timeout", type=float, default=None, help="seconds before a camera read counts as hung (default 8)")
    p.add_argument("--idle-timeout", type=float, default=None,
                   help="seconds without keyboard/mouse before the camera is consulted; 0 = always use the camera (default 30)")
    switch_group = p.add_mutually_exclusive_group()
    switch_group.add_argument("--toggle", action="store_true", help="flip the camera presence switch and exit")
    switch_group.add_argument("--enable", action="store_true", help="switch camera presence on and exit")
    switch_group.add_argument("--disable", action="store_true", help="switch camera presence off and exit")
    p.add_argument("--once", action="store_true", help="poll once, print result, exit")
    p.add_argument("--dry-run", action="store_true", help="log actions without starting/stopping")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--capture-worker", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    if args.capture_worker:
        return capture_worker_main()

    switch = Switch()
    if args.toggle or args.enable or args.disable:
        on = switch.toggle() if args.toggle else switch.set_enabled(bool(args.enable))
        print("enabled" if on else "disabled")
        notify("Camera presence enabled" if on else "Camera presence disabled", "󰄀" if on else "󰗟")
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True)  # the widget watches this directory

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if os.environ.get("AUTO_SCREENSAVER_LOG") == "journal":
        # Supervised by omarchy-shell: also log to journald so that
        # `journalctl --user -t auto-screensaver -f` works like the systemd mode.
        import logging.handlers
        try:
            handler = logging.handlers.SysLogHandler(address="/dev/log")
            handler.ident = "auto-screensaver: "
            handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
            logging.getLogger().addHandler(handler)
        except OSError:
            pass

    def effective_config() -> dict:
        cfg = load_config(args.config)
        for state in DEFAULT_INTERVALS:
            if getattr(args, f"interval_{state}") is not None:
                cfg["intervals"][state] = getattr(args, f"interval_{state}")
        for key in DEFAULT_CONFIG:
            if getattr(args, key) is not None:
                cfg[key] = getattr(args, key)
        return cfg

    def config_mtime():
        try:
            return args.config.stat().st_mtime
        except OSError:
            return None

    def describe(cfg: dict) -> str:
        return (" ".join(f"{k}={v:g}s" for k, v in cfg["intervals"].items())
                + f" absent_polls={cfg['absent_polls']} score={cfg['score']} device={cfg['device'] or 'auto'}"
                + f" capture_timeout={cfg['capture_timeout']:g}s idle_timeout={cfg['idle_timeout']:g}s")

    cfg, seen_mtime = effective_config(), config_mtime()
    webcam = Webcam(device=cfg["device"], capture_timeout=cfg["capture_timeout"])
    detector = FaceDetector(score_threshold=cfg["score"])
    idle = WaylandIdleMonitor(cfg["idle_timeout"])
    idle.start()
    ctl = Controller(presence=None, screensaver=OmarchyScreensaver(),
                     stay_awake=omarchy_stay_awake, absent_polls=cfg["absent_polls"], dry_run=args.dry_run,
                     intervals=cfg["intervals"], disabled=switch.disabled)
    ctl.presence = make_presence(webcam, detector, idle, screensaver_active=lambda: ctl.last_active)

    def apply(cfg: dict) -> None:
        ctl.configure(intervals=cfg["intervals"], absent_polls=cfg["absent_polls"])
        webcam.device, webcam.capture_timeout = cfg["device"], cfg["capture_timeout"]
        detector.set_score(cfg["score"])
        idle.set_timeout(cfg["idle_timeout"])

    def shutdown() -> None:
        idle.stop()
        webcam.worker.kill()

    if args.once:
        ctl.dry_run = True
        deadline = time.monotonic() + 2.0
        while cfg["idle_timeout"] > 0 and not idle.available and time.monotonic() < deadline:
            time.sleep(0.05)
        present = ctl.presence()
        input_state = "input: n/a" if not idle.available else ("input: idle" if idle.idle else "input: active")
        print({True: "person", False: "nobody", None: "no webcam"}[present],
              "|", input_state,
              "| screensaver", "active" if ctl.screensaver.active() else "inactive",
              "| locked" if ctl.screensaver.locked() else "| unlocked",
              "| stay-awake" if omarchy_stay_awake() else "",
              "| hung" if webcam.hung else "",
              "| switch: off" if switch.disabled() else "| switch: on")
        print(f"config: {args.config}{'' if seen_mtime else ' (not found, defaults)'} | {describe(cfg)}")
        shutdown()
        return 0

    stopping = threading.Event()  # an Event wait, unlike time.sleep, ends as soon as a signal sets it

    def _stop(signum, _frame):
        log.info("received %s, stopping", signal.Signals(signum).name)
        stopping.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info("started: %s dry_run=%s config=%s%s", describe(cfg), args.dry_run, args.config,
             "" if seen_mtime else " (not found, using defaults)")
    sd_notify("READY=1")
    while not stopping.is_set():
        t0 = time.monotonic()
        sd_notify("WATCHDOG=1")
        mtime = config_mtime()
        if mtime != seen_mtime:
            seen_mtime = mtime
            cfg = effective_config()
            apply(cfg)
            log.info("config reloaded: %s", describe(cfg))
        try:
            ctl.step()
        except Exception:  # keep the daemon alive on unexpected errors
            log.exception("step failed")
        wait = ctl.next_interval() - (time.monotonic() - t0)
        log.debug("next poll in %.1fs", max(0.2, wait))
        stopping.wait(max(0.2, wait))
    shutdown()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
