import json
import os
import signal
import socket
import subprocess
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

CONFIG = (
    Path(__file__).resolve().parents[1] / "development/.devcontainer/devcontainer.json"
)
HOOK = json.loads(CONFIG.read_text())["postStartCommand"][-1]
CODE = HOOK.split("<<'BENCH_START'\n", 1)[1].split("\nBENCH_START\n", 1)[0]
MODULE = types.ModuleType("bench_start")
exec(compile(CODE, str(CONFIG), "exec"), MODULE.__dict__)


class BenchStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bench = self.root / "bench"
        self.bench.mkdir()
        (self.bench / "sites").mkdir()
        (self.bench / "sites/common_site_config.json").write_text("{}")
        self.proc = self.root / "proc"
        self.proc.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def process(self, pid, args, cwd):
        path = self.proc / str(pid)
        path.mkdir()
        (path / "cmdline").write_bytes("\0".join(args).encode() + b"\0")
        (path / "cwd").symlink_to(cwd, target_is_directory=True)

    def test_process_detection_is_scoped_to_the_bench_and_actual_command(self):
        self.process(1, ["python", "/bin/honcho", "start"], self.bench)
        self.process(2, ["python", "-c", "honcho start"], self.bench)
        self.process(3, ["python", "/bin/honcho", "start"], self.root)
        self.process(4, ["/bin/honcho", "check"], self.bench)
        self.process(5, ["python", "-m", "honcho", "start"], self.bench)
        self.assertEqual(set(MODULE.bench_processes(self.bench, self.proc)), {1, 5})

    def test_manual_manager_is_reused_without_launching_another(self):
        with (
            patch.object(MODULE, "bench_processes", return_value=[42]),
            patch.object(MODULE, "listening", return_value=True),
            patch.object(MODULE.subprocess, "Popen") as launch,
        ):
            self.assertEqual(MODULE.start_bench(self.bench), 42)
        launch.assert_not_called()

    def test_readiness_honors_existing_custom_ports(self):
        (self.bench / "sites/common_site_config.json").write_text(
            '{"webserver_port":8010,"socketio_port":9002}'
        )
        with (
            patch.object(MODULE, "bench_processes", return_value=[42]),
            patch.object(MODULE, "listening", return_value=True) as listening,
        ):
            self.assertEqual(MODULE.start_bench(self.bench), 42)
        self.assertEqual(
            [call.args[0] for call in listening.call_args_list], [8010, 9002]
        )

    def test_port_conflict_is_reported_without_starting_or_stopping_processes(self):
        with (
            patch.object(MODULE, "bench_processes", return_value=[]),
            patch.object(MODULE, "listening", return_value=True),
            patch.object(MODULE.subprocess, "Popen") as launch,
        ):
            with self.assertRaisesRegex(RuntimeError, "occupied"):
                MODULE.start_bench(self.bench)
        launch.assert_not_called()

    def test_multiple_managers_are_left_untouched_for_repair(self):
        with (
            patch.object(MODULE, "bench_processes", return_value=[42, 43]),
            patch.object(MODULE.subprocess, "Popen") as launch,
        ):
            with self.assertRaisesRegex(RuntimeError, "Multiple"):
                MODULE.start_bench(self.bench)
        launch.assert_not_called()

    def test_timeout_does_not_stop_an_adopted_manager(self):
        with (
            patch.object(MODULE, "bench_processes", return_value=[42]),
            patch.object(MODULE, "listening", return_value=False),
            patch.object(MODULE.subprocess, "Popen") as launch,
        ):
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                MODULE.start_bench(self.bench, timeout=0)
        launch.assert_not_called()

    def test_failed_launch_cleans_up_only_its_own_process(self):
        with (
            patch.object(MODULE, "bench_processes", return_value=[]),
            patch.object(MODULE, "listening", return_value=False),
            patch.object(MODULE.subprocess, "Popen") as launch,
        ):
            launch.return_value.poll.return_value = None
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                MODULE.start_bench(self.bench, timeout=0)
        launch.return_value.send_signal.assert_called_once_with(signal.SIGTERM)
        launch.return_value.wait.assert_called_once_with(timeout=10)

    def test_simultaneous_starts_launch_one_detached_manager_and_reuse_it(self):
        # A small stand-in exercises real locks, process discovery and sockets.
        # It never starts Frappe or connects to an application database.
        reservations = [socket.socket(), socket.socket()]
        try:
            for sock in reservations:
                sock.bind(("127.0.0.1", 0))
            ports = tuple(sock.getsockname()[1] for sock in reservations)
        finally:
            for sock in reservations:
                sock.close()
        binary = self.root / "bin"
        binary.mkdir()
        manager = binary / "honcho"
        manager.write_text(
            "import os, selectors, socket\n"
            "from pathlib import Path\n"
            "Path('starts').open('a').write('start\\n')\n"
            "loop = selectors.DefaultSelector()\n"
            "sockets = []\n"
            "for port in os.environ['TEST_PORTS'].split(','):\n"
            "    sock = socket.socket()\n"
            "    sock.bind(('127.0.0.1', int(port)))\n"
            "    sock.listen()\n"
            "    sockets.append(sock)\n"
            "    loop.register(sock, selectors.EVENT_READ)\n"
            "while True:\n"
            "    for key, _ in loop.select():\n"
            "        connection, _ = key.fileobj.accept()\n"
            "        connection.close()\n"
        )
        command = binary / "bench"
        command.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            f"os.execv(sys.executable, [sys.executable, {str(manager)!r}, 'start'])\n"
        )
        command.chmod(0o755)
        environment = {
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "TEST_PORTS": ",".join(map(str, ports)),
        }
        # Suppress ResourceWarnings from intentionally detached Popen instances.
        # Keep them to reap the test process once both hooks have returned.
        launched = []
        popen = subprocess.Popen

        def launch(*args, **kwargs):
            process = popen(*args, **kwargs)
            launched.append(process)
            return process

        try:
            with (
                patch.dict(os.environ, environment),
                patch.object(MODULE.subprocess, "Popen", side_effect=launch),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                futures = [
                    pool.submit(MODULE.start_bench, self.bench, 10, ports)
                    for _ in range(2)
                ]
                pids = [future.result(timeout=15) for future in futures]
            self.assertEqual(pids[0], pids[1])
            self.assertEqual(len(launched), 1)
            self.assertEqual(os.getsid(pids[0]), pids[0])
            self.assertEqual((self.bench / "starts").read_text(), "start\n")
            self.assertEqual(MODULE.start_bench(self.bench, 10, ports), pids[0])
            self.assertEqual(
                (self.bench / "logs/devcontainer-bench.log").stat().st_mode & 0o777,
                0o600,
            )
        finally:
            for process in launched:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
