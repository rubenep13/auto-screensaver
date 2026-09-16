import QtQuick
import Quickshell
import Quickshell.Io

// Supervises the auto-screensaver daemon from inside omarchy-shell.
//
// Loaded once (keepLoaded) with the shell. On load it runs the launcher, which
// prepares the python environment on first use and then execs the daemon. If
// the daemon dies it is restarted with a growing backoff. When the systemd unit
// from install.sh is active we stand aside instead of running a second copy.
Item {
  id: root
  width: 0
  height: 0
  visible: false

  property var shell: null
  property var manifest: null
  property string omarchyPath: ""

  readonly property string launcher: Qt.resolvedUrl("bin/auto-screensaver").toString().replace(/^file:\/\//, "")

  // starting | setup | running | systemd | failed | stopped
  property string state: "starting"
  property int restarts: 0
  property int backoffMs: 5000
  property string lastLine: ""
  property bool stopping: false

  function log(message) {
    console.log("auto-screensaver: " + message)
  }

  function start() {
    if (root.stopping || systemdProbe.running || daemon.running) return
    systemdProbe.running = true
  }

  property bool inTraceback: false

  function onDaemonLine(line) {
    line = String(line)
    if (line.indexOf("WARN:0@") !== -1) return  // OpenCV DNN chatter
    root.lastLine = line
    if (line.indexOf(" INFO started:") !== -1) {
      root.state = "running"
      root.backoffMs = 5000
    }
    // Everything before the daemon reports "started" is setup output or a
    // crash; afterwards only warnings, errors and full tracebacks matter.
    if (line.indexOf("Traceback") !== -1) root.inTraceback = true
    if (root.state !== "running" || root.inTraceback
        || line.indexOf(" WARNING ") !== -1 || line.indexOf(" ERROR ") !== -1)
      root.log(line)
  }

  Process {
    id: systemdProbe
    command: ["systemctl", "--user", "is-active", "--quiet", "auto-screensaver.service"]
    onExited: function(exitCode) {
      if (exitCode === 0) {
        root.state = "systemd"
        root.log("systemd unit is active; not spawning a second daemon")
        recheckTimer.restart()
        return
      }
      root.state = "setup"
      daemon.command = [root.launcher, "run"]
      daemon.running = true
    }
  }

  Process {
    id: daemon
    environment: ({ "AUTO_SCREENSAVER_LOG": "journal" })
    stdout: SplitParser { onRead: function(line) { root.onDaemonLine(line) } }
    stderr: SplitParser { onRead: function(line) { root.onDaemonLine(line) } }
    onStarted: root.log("launcher started, pid " + daemon.processId)
    onExited: function(exitCode, exitStatus) {
      if (root.stopping) {
        root.state = "stopped"
        return
      }
      root.state = "failed"
      root.restarts += 1
      root.log("daemon exited (code " + exitCode + "), restarting in " + Math.round(root.backoffMs / 1000) + "s")
      restartTimer.interval = root.backoffMs
      restartTimer.restart()
      root.backoffMs = Math.min(root.backoffMs * 2, 60000)
    }
  }

  Timer {
    id: restartTimer
    repeat: false
    onTriggered: root.start()
  }

  // While the systemd unit owns the daemon, look again every minute in case it
  // gets disabled later.
  Timer {
    id: recheckTimer
    interval: 60000
    repeat: false
    onTriggered: root.start()
  }

  IpcHandler {
    target: "rubenep13.auto-screensaver"

    function status(): string {
      return JSON.stringify({
        state: root.state,
        restarts: root.restarts,
        pid: daemon.running ? daemon.processId : 0,
        lastLine: root.lastLine
      })
    }

    function restart(): string {
      root.backoffMs = 5000
      if (daemon.running) daemon.running = false
      else root.start()
      return "ok"
    }
  }

  Component.onCompleted: root.start()
  Component.onDestruction: {
    root.stopping = true
    daemon.running = false
  }
}
