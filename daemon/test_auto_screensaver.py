import unittest

from auto_screensaver import Controller, NONE, START, STOP


class FakeScreensaver:
    def __init__(self, active=False, locked=False):
        self._active = active
        self._locked = locked
        self.calls = []

    def active(self):
        return self._active

    def locked(self):
        return self._locked

    def start(self):
        self.calls.append("start")
        self._active = True

    def stop(self):
        self.calls.append("stop")
        self._active = False


class ControllerTest(unittest.TestCase):
    def make(self, present, active=False, locked=False, stay_awake=False, absent_polls=3):
        ss = FakeScreensaver(active=active, locked=locked)
        ctl = Controller(
            presence=lambda: present,
            screensaver=ss,
            stay_awake=lambda: stay_awake,
            absent_polls=absent_polls,
        )
        return ctl, ss

    def test_no_webcam_does_nothing_and_resets_streak(self):
        ctl, ss = self.make(present=None)
        ctl.absent_streak = 2
        self.assertEqual(ctl.step(), NONE)
        self.assertEqual(ctl.absent_streak, 0)
        self.assertEqual(ss.calls, [])

    def test_locked_session_does_nothing(self):
        ctl, ss = self.make(present=True, active=True, locked=True)
        self.assertEqual(ctl.step(), NONE)
        ctl, ss = self.make(present=False, active=False, locked=True, absent_polls=1)
        self.assertEqual(ctl.step(), NONE)
        self.assertEqual(ss.calls, [])

    def test_person_and_active_screensaver_stops_it(self):
        ctl, ss = self.make(present=True, active=True)
        self.assertEqual(ctl.step(), STOP)
        self.assertEqual(ss.calls, ["stop"])

    def test_person_and_inactive_screensaver_does_nothing(self):
        ctl, ss = self.make(present=True, active=False)
        self.assertEqual(ctl.step(), NONE)
        self.assertEqual(ss.calls, [])

    def test_absent_needs_consecutive_polls_before_starting(self):
        ctl, ss = self.make(present=False, active=False, absent_polls=3)
        self.assertEqual(ctl.step(), NONE)
        self.assertEqual(ctl.step(), NONE)
        self.assertEqual(ctl.step(), START)
        self.assertEqual(ss.calls, ["start"])

    def test_presence_resets_absent_streak(self):
        ss = FakeScreensaver()
        seq = iter([False, False, True, False])
        ctl = Controller(presence=lambda: next(seq), screensaver=ss,
                         stay_awake=lambda: False, absent_polls=3)
        ctl.step(); ctl.step()
        self.assertEqual(ctl.absent_streak, 2)
        ctl.step()
        self.assertEqual(ctl.absent_streak, 0)
        ctl.step()
        self.assertEqual(ctl.absent_streak, 1)
        self.assertEqual(ss.calls, [])

    def test_absent_with_active_screensaver_does_nothing(self):
        ctl, ss = self.make(present=False, active=True, absent_polls=1)
        self.assertEqual(ctl.step(), NONE)
        self.assertEqual(ss.calls, [])

    def test_stay_awake_blocks_start_but_not_stop(self):
        ctl, ss = self.make(present=False, active=False, stay_awake=True, absent_polls=1)
        self.assertEqual(ctl.step(), NONE)
        ctl, ss = self.make(present=True, active=True, stay_awake=True)
        self.assertEqual(ctl.step(), STOP)

    def test_dry_run_reports_action_without_acting(self):
        ctl, ss = self.make(present=True, active=True)
        ctl.dry_run = True
        self.assertEqual(ctl.step(), STOP)
        self.assertEqual(ss.calls, [])


if __name__ == "__main__":
    unittest.main()


class AdaptiveIntervalTest(unittest.TestCase):
    INTERVALS = {"present": 12, "absent": 3, "active": 2, "no_webcam": 15}

    def make(self, present, active=False, absent_polls=3):
        ss = FakeScreensaver(active=active)
        ctl = Controller(presence=lambda: present, screensaver=ss, stay_awake=lambda: False,
                         absent_polls=absent_polls, intervals=self.INTERVALS)
        return ctl, ss

    def test_before_first_step_uses_absent_interval(self):
        ctl, _ = self.make(present=True)
        self.assertEqual(ctl.next_interval(), 3)

    def test_person_and_inactive_polls_slowly(self):
        ctl, _ = self.make(present=True, active=False)
        ctl.step()
        self.assertEqual(ctl.next_interval(), 12)

    def test_nobody_and_inactive_polls_at_absent_rate(self):
        ctl, _ = self.make(present=False, active=False)
        ctl.step()
        self.assertEqual(ctl.next_interval(), 3)

    def test_active_screensaver_polls_fast(self):
        ctl, _ = self.make(present=False, active=True)
        ctl.step()
        self.assertEqual(ctl.next_interval(), 2)

    def test_after_start_polls_fast(self):
        ctl, _ = self.make(present=False, active=False, absent_polls=1)
        self.assertEqual(ctl.step(), START)
        self.assertEqual(ctl.next_interval(), 2)

    def test_after_stop_polls_slowly(self):
        ctl, _ = self.make(present=True, active=True)
        self.assertEqual(ctl.step(), STOP)
        self.assertEqual(ctl.next_interval(), 12)

    def test_no_webcam_polls_rarely(self):
        ctl, _ = self.make(present=None)
        ctl.step()
        self.assertEqual(ctl.next_interval(), 15)


class RecordingWorker:
    def __init__(self):
        self.devs = []
    def grab(self, dev, timeout, *a):
        self.devs.append(dev)
        return None
    def kill(self):
        pass


class WebcamBusyTest(unittest.TestCase):
    def test_device_in_use_by_other_process_is_skipped(self):
        from auto_screensaver import Webcam
        worker = RecordingWorker()
        cam = Webcam(device="/dev/null", in_use=lambda dev: 4242, worker=worker)
        self.assertIsNone(cam.grab())
        self.assertEqual(worker.devs, [])
        self.assertEqual(cam.busy_pid, 4242)

    def test_device_free_is_opened(self):
        from auto_screensaver import Webcam
        worker = RecordingWorker()
        cam = Webcam(device="/dev/null", in_use=lambda dev: None, worker=worker)
        self.assertIsNone(cam.grab())
        self.assertEqual(worker.devs, ["/dev/null"])


class ConfigTest(unittest.TestCase):
    def test_defaults_when_file_missing(self):
        from auto_screensaver import load_config, DEFAULT_INTERVALS
        import pathlib
        cfg = load_config(pathlib.Path("/nonexistent/config.toml"))
        self.assertEqual(cfg["intervals"], DEFAULT_INTERVALS)
        self.assertEqual(cfg["absent_polls"], 3)
        self.assertEqual(cfg["score"], 0.6)
        self.assertIsNone(cfg["device"])
        self.assertEqual(cfg["capture_timeout"], 8.0)

    def test_file_values_override_defaults_and_unknown_keys_are_ignored(self):
        from auto_screensaver import load_config
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "config.toml"
            p.write_text('absent_polls = 5\nscore = 0.4\ndevice = "/dev/video2"\nbogus = 1\n'
                         '[intervals]\npresent = 30\nactive = 1\n')
            cfg = load_config(p)
        self.assertEqual(cfg["absent_polls"], 5)
        self.assertEqual(cfg["score"], 0.4)
        self.assertEqual(cfg["device"], "/dev/video2")
        self.assertEqual(cfg["intervals"]["present"], 30)
        self.assertEqual(cfg["intervals"]["active"], 1)
        self.assertEqual(cfg["intervals"]["absent"], 3)  # untouched default

    def test_invalid_file_falls_back_to_defaults(self):
        from auto_screensaver import load_config, DEFAULT_INTERVALS
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "config.toml"
            p.write_text("this is = not [ toml")
            cfg = load_config(p)
        self.assertEqual(cfg["intervals"], DEFAULT_INTERVALS)

    def test_controller_applies_new_intervals(self):
        ss = FakeScreensaver()
        ctl = Controller(presence=lambda: True, screensaver=ss, stay_awake=lambda: False)
        ctl.step()
        self.assertEqual(ctl.next_interval(), 12)
        ctl.configure(intervals={"present": 40}, absent_polls=7)
        self.assertEqual(ctl.next_interval(), 40)
        self.assertEqual(ctl.absent_polls, 7)


class CaptureTimeoutTest(unittest.TestCase):
    """The camera is read in a worker process; a hung USB device must not block the daemon."""

    def test_timeout_kills_worker_and_reports_no_frame(self):
        from auto_screensaver import Webcam

        class HungWorker:
            killed = 0
            def grab(self, dev, timeout, *a):
                raise TimeoutError
            def kill(self):
                HungWorker.killed += 1

        cam = Webcam(device="/dev/null", in_use=lambda dev: None, worker=HungWorker(), capture_timeout=0.1)
        self.assertIsNone(cam.grab())
        self.assertEqual(HungWorker.killed, 1)
        self.assertTrue(cam.hung)

    def test_successful_grab_clears_hung_flag(self):
        from auto_screensaver import Webcam

        class OkWorker:
            def grab(self, dev, timeout, *a):
                return "frame"
            def kill(self):
                pass

        cam = Webcam(device="/dev/null", in_use=lambda dev: None, worker=OkWorker())
        cam.hung = True
        self.assertEqual(cam.grab(), "frame")
        self.assertFalse(cam.hung)


class InputPresenceTest(unittest.TestCase):
    """Keyboard/mouse activity means somebody is there; the camera is only consulted when idle."""

    class FakeIdle:
        def __init__(self, available, idle):
            self.available, self.idle = available, idle

    def make(self, available, idle, camera_result=False):
        from auto_screensaver import make_presence
        calls = []

        class Cam:
            def grab(self):
                calls.append("grab")
                return None if camera_result is None else "frame"

        class Det:
            def faces(self, frame):
                return 1 if camera_result else 0

        return make_presence(Cam(), Det(), self.FakeIdle(available, idle)), calls

    def test_recent_input_means_present_without_camera(self):
        presence, calls = self.make(available=True, idle=False)
        self.assertIs(presence(), True)
        self.assertEqual(calls, [])

    def test_idle_consults_camera(self):
        presence, calls = self.make(available=True, idle=True, camera_result=False)
        self.assertIs(presence(), False)
        self.assertEqual(calls, ["grab"])

    def test_idle_with_face_is_present(self):
        presence, calls = self.make(available=True, idle=True, camera_result=True)
        self.assertIs(presence(), True)

    def test_unavailable_monitor_falls_back_to_camera(self):
        presence, calls = self.make(available=False, idle=False, camera_result=None)
        self.assertIsNone(presence())
        self.assertEqual(calls, ["grab"])

    def test_no_monitor_at_all_uses_camera(self):
        from auto_screensaver import make_presence
        calls = []

        class Cam:
            def grab(self):
                calls.append("grab")
                return "frame"

        class Det:
            def faces(self, frame):
                return 0

        self.assertIs(make_presence(Cam(), Det(), None)(), False)
        self.assertEqual(calls, ["grab"])

    def test_active_screensaver_ignores_input_and_uses_camera(self):
        # Launching the screensaver makes the compositor report seat activity;
        # while it is up only the camera may decide.
        from auto_screensaver import make_presence
        calls = []

        class Cam:
            def grab(self):
                calls.append("grab")
                return "frame"

        class Det:
            def faces(self, frame):
                return 0

        presence = make_presence(Cam(), Det(), self.FakeIdle(available=True, idle=False),
                                 screensaver_active=lambda: True)
        self.assertIs(presence(), False)
        self.assertEqual(calls, ["grab"])

    def test_controller_last_active_drives_camera_only_mode(self):
        from auto_screensaver import make_presence
        calls = []

        class Cam:
            def grab(self):
                calls.append("grab")
                return "frame"

        class Det:
            def faces(self, frame):
                return 0

        ss = FakeScreensaver(active=False)
        ctl = Controller(presence=None, screensaver=ss, stay_awake=lambda: False, absent_polls=1)
        ctl.presence = make_presence(Cam(), Det(), self.FakeIdle(available=True, idle=True),
                                     screensaver_active=lambda: ctl.last_active)
        self.assertEqual(ctl.step(), START)          # idle + nobody -> start
        ctl.presence = make_presence(Cam(), Det(), self.FakeIdle(available=True, idle=False),
                                     screensaver_active=lambda: ctl.last_active)
        calls.clear()
        self.assertEqual(ctl.step(), NONE)           # spurious "input" while active: camera says nobody, keep it
        self.assertEqual(calls, ["grab"])
        self.assertTrue(ss.active())

    def test_externally_started_screensaver_is_not_killed_by_input_artifact(self):
        # Omarchy's own idle timer launched the screensaver; the compositor then
        # reports "activity". The very next poll must already use the camera.
        from auto_screensaver import make_presence
        calls = []

        class Cam:
            def grab(self):
                calls.append("grab")
                return "frame"

        class Det:
            def faces(self, frame):
                return 0

        ss = FakeScreensaver(active=True)
        ctl = Controller(presence=None, screensaver=ss, stay_awake=lambda: False)
        self.assertFalse(ctl.last_active)  # daemon has never seen it yet
        ctl.presence = make_presence(Cam(), Det(), self.FakeIdle(available=True, idle=False),
                                     screensaver_active=lambda: ctl.last_active)
        self.assertEqual(ctl.step(), NONE)
        self.assertEqual(calls, ["grab"])
        self.assertTrue(ss.active())
        self.assertEqual(ss.calls, [])


class DisabledSwitchTest(unittest.TestCase):
    """The bar toggle writes a flag file; with it present the daemon does nothing at all."""

    def test_disabled_skips_camera_and_actions(self):
        calls = []
        ss = FakeScreensaver(active=False)
        ctl = Controller(presence=lambda: calls.append("presence") or False, screensaver=ss,
                         stay_awake=lambda: False, absent_polls=1, disabled=lambda: True)
        self.assertEqual(ctl.step(), NONE)
        self.assertEqual(calls, [])
        self.assertEqual(ss.calls, [])
        self.assertEqual(ctl.next_interval(), ctl.intervals["no_webcam"])

    def test_disabled_does_not_stop_an_active_screensaver(self):
        ss = FakeScreensaver(active=True)
        ctl = Controller(presence=lambda: True, screensaver=ss, stay_awake=lambda: False, disabled=lambda: True)
        self.assertEqual(ctl.step(), NONE)
        self.assertEqual(ss.calls, [])

    def test_reenabling_resets_absent_streak(self):
        flag = {"off": False}
        ctl = Controller(presence=lambda: False, screensaver=FakeScreensaver(), stay_awake=lambda: False,
                         absent_polls=3, disabled=lambda: flag["off"])
        ctl.step(); ctl.step()
        self.assertEqual(ctl.absent_streak, 2)
        flag["off"] = True
        ctl.step()
        self.assertEqual(ctl.absent_streak, 0)

    def test_flag_file_helpers(self):
        import tempfile, pathlib
        from auto_screensaver import Switch
        with tempfile.TemporaryDirectory() as d:
            sw = Switch(pathlib.Path(d) / "state" / "disabled")
            self.assertTrue(sw.enabled())
            self.assertFalse(sw.disabled())
            self.assertEqual(sw.set_enabled(False), False)
            self.assertTrue(sw.disabled())
            self.assertTrue(sw.path.exists())
            self.assertEqual(sw.toggle(), True)
            self.assertFalse(sw.path.exists())


class SmokeTest(unittest.TestCase):
    """Run the real entry point once, end to end, with no camera and no input monitor."""

    def test_once_runs_to_completion(self):
        import os, subprocess, sys, pathlib
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.skipTest("cv2 not installed")
        script = pathlib.Path(__file__).with_name("auto_screensaver.py")
        env = dict(os.environ, AUTO_SCREENSAVER_LOG="journal")
        r = subprocess.run([sys.executable, str(script), "--once", "--device", "/dev/null", "--idle-timeout", "0"],
                           capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no webcam", r.stdout)
