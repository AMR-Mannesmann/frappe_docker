"""Check opt-in behavior, authentication seeding, and T3 process ownership."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "development"))
import agent_tools


class AgentToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = agent_tools.Config(
            codex_home=self.root / "codex",
            t3_home=self.root / "t3",
            bin_dir=self.root / "bin",
            project_dir=self.root,
        )
        runtime = mock.patch.object(
            agent_tools.Config,
            "runtime_dir",
            new_callable=mock.PropertyMock,
            return_value=self.root / "run",
        )
        runtime.start()
        self.addCleanup(runtime.stop)

    def write_process_record(self, pid, start_time):
        self.config.runtime_dir.mkdir(exist_ok=True)
        (self.config.runtime_dir / "server.json").write_text(
            json.dumps({"pid": pid, "start_time": start_time})
        )

    def test_disabled_setup_does_not_touch_agent_state(self):
        with mock.patch.dict(os.environ, {"FRAPPE_DEV_AGENTS": "0"}):
            with mock.patch.object(sys, "argv", ["agent_tools.py", "setup"]):
                self.assertEqual(agent_tools.main(), 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_auth_is_seeded_privately_and_never_overwritten(self):
        self.config.codex_home.mkdir()
        source = self.root / "source.json"
        initial = {"tokens": {"access_token": "test-fixture-only"}}
        source.write_text(json.dumps(initial))
        with mock.patch.dict(os.environ, {"FRAPPE_CODEX_AUTH_SOURCE": str(source)}):
            agent_tools.seed_codex_auth(self.config)
            source.write_text(
                json.dumps({"tokens": {"access_token": "changed-fixture"}})
            )
            agent_tools.seed_codex_auth(self.config)
        destination = self.config.codex_home / "auth.json"
        self.assertEqual(json.loads(destination.read_text()), initial)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_stop_does_not_signal_a_recycled_pid(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            self.write_process_record(child.pid, "old-process-identity")
            agent_tools.stop(self.config)
            self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_missing_process_with_missing_identity_is_not_owned(self):
        self.write_process_record(999999999, None)
        self.assertIsNone(agent_tools.managed_process(self.config))

    def test_occupied_port_does_not_start_another_server(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with mock.patch.object(agent_tools, "T3_PORT", port):
                with mock.patch.object(agent_tools, "ensure_tools"):
                    with self.assertRaisesRegex(RuntimeError, "already occupied"):
                        agent_tools.start(self.config)
            self.assertFalse((self.config.runtime_dir / "server.json").exists())
            self.assertEqual(listener.getsockname()[1], port)


if __name__ == "__main__":
    unittest.main()
