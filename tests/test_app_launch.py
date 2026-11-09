"""Start-up helpers: shortcuts launch through pythonw, and a stuck copy on the
UI port is stopped instead of being 'opened'."""
import socket, subprocess, sys, time, unittest
from sipphone import app


class ShortcutTarget(unittest.TestCase):
    def test_shortcut_runs_the_module_not_the_exe_launcher(self):
        target, args = app.shortcut_target(["--no-browser"])
        self.assertTrue(target.endswith(("python", "python3", "pythonw.exe", "python.exe")) or sys.executable in target)
        self.assertEqual(args, "-m sipphone.app --no-browser")
        self.assertNotIn("sipphone-app", target)


class StuckInstance(unittest.TestCase):
    def test_unresponsive_python_on_port_is_stopped(self):
        port = 18088
        p = subprocess.Popen([sys.executable, "-c",
            "import socket,time\ns=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
            f"s.bind(('127.0.0.1',{port}));s.listen(1);time.sleep(60)"])
        try:
            for _ in range(50):
                if app._port_in_use(port): break
                time.sleep(0.1)
            pid, name = app.port_owner(port)
            self.assertEqual(pid, p.pid)
            note = app.kill_stuck_instance(port)
            self.assertIn("stopped", note)
            self.assertFalse(app._port_in_use(port))
        finally:
            if p.poll() is None: p.kill()

    def test_other_programs_are_left_alone(self):
        # nothing we own on this port -> report, don't kill
        self.assertIn("nothing", app.kill_stuck_instance(18099))
