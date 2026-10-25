import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "devcontainer-example/t3-entrypoint.py"
SPEC = importlib.util.spec_from_file_location("t3_entrypoint", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class T3LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {"T3CODE_HOME": str(self.root), "FRAPPE_BENCH_NAME": "test-bench"},
        )
        self.environment.start()
        self.lifecycle = MODULE.T3Lifecycle(ready_file=self.root / "ready")

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

    def test_ready_container_does_not_reinitialize_persisted_state(self):
        self.lifecycle.ready_file.touch()
        state = self.root / "runtime/service-state.json"
        state.parent.mkdir()
        original = (
            b'{"protocol":3,"activeVersion":"0.1.3","update":{"status":"committed"}}\n'
        )
        state.write_bytes(original)
        result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with (
            patch.object(self.lifecycle, "activate_bench"),
            patch.object(self.lifecycle, "bootstrap_version") as bootstrap,
            patch.object(self.lifecycle, "seed_settings") as seed,
            patch.object(self.lifecycle, "wait_ready"),
            patch.object(
                self.lifecycle,
                "runtime",
                return_value=self.root / "runtime/versions/0.1.3/t3",
            ),
            patch.object(MODULE.subprocess, "run", return_value=result),
        ):
            self.lifecycle.prepare()
        bootstrap.assert_not_called()
        seed.assert_not_called()
        self.assertEqual(state.read_bytes(), original)

    def test_pending_recovery_finishes_before_project_registration(self):
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
        events = []

        def recovered():
            events.append("recovery")
            self.assertTrue(self.lifecycle.ready_file.exists())
            state.write_text(json.dumps({"protocol": 3, "activeVersion": "0.1.3"}))

        def register(arguments, **kwargs):
            events.append("project")
            self.assertEqual(arguments[0], str(self.root / "runtime/versions/0.1.3/t3"))
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with (
            patch.object(self.lifecycle, "activate_bench"),
            patch.object(
                self.lifecycle,
                "runtime",
                side_effect=lambda version, **kw: self.root
                / f"runtime/versions/{version}/t3",
            ),
            patch.object(self.lifecycle, "seed_settings") as seed,
            patch.object(self.lifecycle, "bootstrap_version") as bootstrap,
            patch.object(self.lifecycle, "wait_ready", side_effect=recovered),
            patch.object(MODULE.subprocess, "run", side_effect=register),
        ):
            self.lifecycle.prepare()
        self.assertEqual(events, ["recovery", "project"])
        seed.assert_not_called()
        bootstrap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
