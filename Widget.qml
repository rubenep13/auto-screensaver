import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui

// Bar switch for the auto-screensaver daemon. Mirrors Omarchy's Stay Awake:
// the state is a flag file, ~/.local/state/auto-screensaver/disabled, that the
// daemon checks on every poll. Present = camera presence off.
BarWidget {
  id: root
  moduleName: "ruben.auto-screensaver"

  readonly property string home: Quickshell.env("HOME")
  readonly property string xdgState: Quickshell.env("XDG_STATE_HOME")
  readonly property string stateDir: (xdgState && xdgState !== "" ? xdgState : home + "/.local/state") + "/auto-screensaver"
  readonly property string flagPath: stateDir + "/disabled"

  property bool disabled: false
  property bool loaded: false

  // Our own service (the daemon supervisor), for the tooltip.
  readonly property var service: {
    var host = bar && bar.shell ? bar.shell : null
    return host && typeof host.serviceFor === "function" ? host.serviceFor(root.moduleName) : null
  }
  readonly property string daemonState: service && service.state ? String(service.state) : ""

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function refresh() {
    if (!probe.running) probe.running = true
  }

  function toggle() {
    if (toggler.running) return
    toggler.command = root.disabled
      ? ["rm", "-f", root.flagPath]
      : ["bash", "-c", "mkdir -p \"$1\" && touch \"$2\"", "--", root.stateDir, root.flagPath]
    toggler.running = true
  }

  Process {
    id: probe
    command: ["bash", "-c", "mkdir -p \"$1\"; [[ -f \"$2\" ]] && echo yes || echo no", "--", root.stateDir, root.flagPath]
    stdout: SplitParser {
      onRead: function(line) {
        root.disabled = String(line).trim() === "yes"
        root.loaded = true
      }
    }
  }

  Process {
    id: toggler
    onExited: function() {
      root.refresh()
      root.bar.run(root.disabled
        ? "omarchy-notification-send -g 󰄀 'Camera presence enabled'"
        : "omarchy-notification-send -g 󰗟 'Camera presence disabled'")
    }
  }

  // The daemon and the CLI (--toggle) write the same flag; follow them.
  FileView {
    path: root.stateDir
    watchChanges: true
    printErrors: false
    onFileChanged: root.refresh()
  }

  Component.onCompleted: root.refresh()

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.disabled ? "󰗟" : "󰄀"
    active: root.loaded && !root.disabled
    dimmed: root.disabled
    useActiveColor: false
    tooltipText: (root.disabled ? "Camera presence off (click to enable)" : "Camera presence on (click to disable)")
      + (root.daemonState !== "" && root.daemonState !== "running" ? " · daemon " + root.daemonState : "")
    onPressed: function() { root.toggle() }
  }
}
