import fcntl
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

CONFIG = (
    Path(__file__).resolve().parents[1] / "development/.devcontainer/devcontainer.json"
)
HOOK = json.loads(CONFIG.read_text())["postStartCommand"][-1]


def hook_module(marker, name):
    code = HOOK.split(f"<<'{marker}'\n", 1)[1].split(f"\n{marker}\n", 1)[0]
    module = types.ModuleType(name)
    exec(compile(code, str(CONFIG), "exec"), module.__dict__)
    return module


MODULE = hook_module("T3_PREPARE", "t3_provisioning")
READINESS = hook_module("T3_READY", "t3_readiness")


class T3HookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {"T3CODE_HOME": str(self.root), "FRAPPE_BENCH_NAME": "test-bench"},
        )
        self.environment.start()
        self.lifecycle = MODULE.T3Provisioning()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_write_once_preserves_pending_update_byte_for_byte(self):
        state = self.root / "runtime/service-state.json"
        state.parent.mkdir()
        original = (
            b'{"protocol":3,"activeVersion":"0.1.2","update":{"status":"pending"}}\n'
        )
        state.write_bytes(original)
        self.lifecycle.write_once(state, {"protocol": 3, "activeVersion": "0.1.3"})
        self.assertEqual(state.read_bytes(), original)

    def test_atomic_initialization_is_private(self):
        state = self.root / "runtime/service-state.json"
        self.lifecycle.write_once(state, {"protocol": 3, "activeVersion": "0.1.3"})
        self.assertEqual(json.loads(state.read_text())["activeVersion"], "0.1.3")
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)

    def test_incompatible_or_unsafe_state_is_retained_for_repair(self):
        state = self.root / "runtime/service-state.json"
        state.parent.mkdir()
        for value in (
            [],
            {"protocol": 4, "activeVersion": "0.1.3"},
            {"protocol": 3, "activeVersion": "../../userdata"},
            {"protocol": 3, "activeVersion": "0.1.3", "update": None},
        ):
            state.write_text(json.dumps(value))
            original = state.read_bytes()
            with self.assertRaises(MODULE.LifecycleError):
                self.lifecycle.read_state()
            self.assertEqual(state.read_bytes(), original)

    def test_existing_settings_are_never_reseeded(self):
        destination = self.root / "userdata/settings.json"
        destination.parent.mkdir()
        destination.write_text('{"branchNamePrefix":"stack-specific"}\n')
        original = destination.read_bytes()
        self.lifecycle.seed_settings()
        self.assertEqual(destination.read_bytes(), original)

    def test_runtime_preflight_rejects_incompatible_binary(self):
        version = "0.1.3"
        directory = self.root / "runtime/versions" / version
        (directory / "client").mkdir(parents=True)
        (directory / "client/index.html").write_text("fixture")
        (directory / "resource-monitor").mkdir()
        (directory / "node_modules").mkdir()
        (directory / ".install-complete").write_text(version)
        (directory / "t3").write_text("fixture")
        (directory / "t3").chmod(0o755)
        result = type(
            "Result",
            (),
            {"stdout": json.dumps({"status": "blocked", "version": version})},
        )()
        with patch.object(self.lifecycle, "command", return_value=result):
            with self.assertRaises(MODULE.LifecycleError):
                self.lifecycle.runtime(version)
        self.assertFalse(self.lifecycle.state_file.exists())

    def test_running_launcher_does_not_reinitialize_persisted_state(self):
        state = self.root / "runtime/service-state.json"
        state.parent.mkdir()
        original = (
            b'{"protocol":3,"activeVersion":"0.1.3","update":{"status":"committed"}}\n'
        )
        state.write_bytes(original)
        self.lifecycle.log.parent.mkdir()
        with (
            self.lifecycle.server_lock.open("a") as lock,
            patch.object(self.lifecycle, "bootstrap_version") as bootstrap,
            patch.object(self.lifecycle, "seed_settings") as seed,
            patch.object(self.lifecycle, "runtime") as runtime,
        ):
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.lifecycle.prepare()
        bootstrap.assert_not_called()
        seed.assert_not_called()
        runtime.assert_called_once_with("0.1.3", preflight=False)
        self.assertEqual(state.read_bytes(), original)

    def test_launcher_lock_detects_the_running_owner_and_releases_the_probe(self):
        self.lifecycle.log.parent.mkdir()
        self.assertFalse(self.lifecycle.server_running())
        with self.lifecycle.server_lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(self.lifecycle.server_running())
        self.assertFalse(self.lifecycle.server_running())

    def test_stopped_launcher_checks_selected_runtime_without_reseeding_preferences(
        self,
    ):
        state = self.root / "runtime/service-state.json"
        state.parent.mkdir()
        original = b'{"protocol":3,"activeVersion":"0.1.3"}\n'
        state.write_bytes(original)
        destination = self.root / "userdata/settings.json"
        destination.parent.mkdir()
        preferences = b'{"branchNamePrefix":"keep-existing"}\n'
        destination.write_bytes(preferences)
        executable = self.root / "runtime/versions/0.1.3/t3"
        with (
            patch.object(self.lifecycle, "bootstrap_version") as bootstrap,
            patch.object(self.lifecycle, "runtime", return_value=executable) as runtime,
        ):
            selected = self.lifecycle.prepare()
        self.assertEqual(selected, executable)
        bootstrap.assert_not_called()
        runtime.assert_called_once_with("0.1.3", preflight=True)
        self.assertEqual(state.read_bytes(), original)
        self.assertEqual(destination.read_bytes(), preferences)

    def test_pending_update_defers_database_preflight_and_settings(self):
        state = self.root / "runtime/service-state.json"
        state.parent.mkdir()
        original = (
            b'{"protocol":3,"activeVersion":"0.1.2","update":{"status":"pending"}}\n'
        )
        state.write_bytes(original)
        with (
            patch.object(self.lifecycle, "runtime") as runtime,
            patch.object(self.lifecycle, "seed_settings") as seed,
            patch.object(self.lifecycle, "bootstrap_version") as bootstrap,
        ):
            self.lifecycle.prepare()
        runtime.assert_called_once_with("0.1.2", preflight=False)
        seed.assert_not_called()
        bootstrap.assert_not_called()
        self.assertEqual(state.read_bytes(), original)

    def test_readiness_waits_for_recovery_and_selects_recovered_runtime(self):
        state = self.root / "runtime/service-state.json"
        state.parent.mkdir()
        state.write_text(
            json.dumps(
                {
                    "protocol": 3,
                    "activeVersion": "0.1.2",
                    "update": {"status": "pending"},
                }
            )
        )

        def recovered():
            state.write_text(json.dumps({"protocol": 3, "activeVersion": "0.1.3"}))
            return json.dumps(
                {
                    "serverVersion": "0.1.3",
                    "capabilities": {"serverSelfUpdate": "boot-service"},
                }
            ).encode()

        with (
            patch.object(READINESS.http.client, "HTTPConnection") as connection,
            patch.object(READINESS.time, "sleep") as sleep,
        ):
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.side_effect = recovered
            runtime = READINESS.wait_ready(self.root, 3773)
        self.assertEqual(runtime, self.root / "runtime/versions/0.1.3/t3")
        sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
