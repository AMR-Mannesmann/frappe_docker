import json
import os
import shlex
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "development/.devcontainer"
HOOK = json.loads((TEMPLATE / "devcontainer.json").read_text())["postCreateCommand"][-1]
CODE = HOOK.split("<<'FRAPPECTL_SETUP'\n", 1)[1].split("\nFRAPPECTL_SETUP\n", 1)[0]
MODULE = types.ModuleType("frappectl_setup")
exec(compile(CODE, str(TEMPLATE / "devcontainer.json"), "exec"), MODULE.__dict__)


class FrappectlSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sites = self.root / "sites"
        self.site = self.sites / "test.localhost"
        self.site.mkdir(parents=True)
        self.home = self.root / "codex"
        self.home.mkdir()
        self.user = types.SimpleNamespace(
            api_key="existing-key",
            api_secret="********",
            get_password=Mock(return_value="existing-secret"),
            save=Mock(),
        )
        self.frappe = Mock()
        self.frappe.get_doc.return_value = self.user
        self.frappe.generate_hash.side_effect = ["new-key", "new-secret"]
        self.modules = patch.dict("sys.modules", {"frappe": self.frappe})
        self.modules.start()

    def tearDown(self):
        self.modules.stop()
        self.temporary.cleanup()

    def configure(self):
        MODULE.configure_site("test.localhost", self.sites)
        return self.site / ".frappectl.env"

    def test_existing_credentials_are_reused_across_recreation(self):
        path = self.configure()
        original = path.read_bytes()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.configure().read_bytes(), original)
        self.user.save.assert_not_called()
        self.frappe.generate_hash.assert_not_called()
        self.assertEqual(self.frappe.destroy.call_count, 2)

    def test_connect_uses_bench_sites_directory_and_restores_working_directory(self):
        original = Path.cwd()
        self.frappe.connect.side_effect = lambda: self.assertEqual(
            Path.cwd(), self.sites
        )
        self.configure()
        self.assertEqual(Path.cwd(), original)

    def test_failed_connection_still_cleans_up_frappe(self):
        self.frappe.connect.side_effect = ConnectionError("Database unavailable")
        with self.assertRaises(ConnectionError):
            self.configure()
        self.frappe.destroy.assert_called_once()
        self.assertFalse((self.site / ".frappectl.env").exists())

    def test_only_missing_credentials_are_created(self):
        self.user.api_key = None
        path = self.configure()
        self.user.save.assert_called_once_with(ignore_permissions=True)
        self.frappe.generate_hash.assert_called_once_with(length=15)
        self.assertIn("FRAPPE_API_KEY=new-key\n", path.read_text())
        self.assertIn("FRAPPE_API_SECRET=existing-secret\n", path.read_text())

    def test_fresh_credentials_are_committed_before_being_published(self):
        self.user.api_key = self.user.api_secret = None
        self.frappe.db.commit.side_effect = lambda: self.assertFalse(
            (self.site / ".frappectl.env").exists()
        )
        path = self.configure()
        self.user.get_password.assert_not_called()
        self.assertIn("FRAPPE_API_KEY=new-key\n", path.read_text())
        self.assertIn("FRAPPE_API_SECRET=new-secret\n", path.read_text())

    def test_unreadable_secret_does_not_rotate_credentials_or_replace_file(self):
        path = self.site / ".frappectl.env"
        path.write_text("keep existing file")
        self.user.get_password.side_effect = ValueError("Encryption key mismatch")
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(path.read_text(), "keep existing file")
        self.frappe.generate_hash.assert_not_called()
        self.user.save.assert_not_called()
        self.frappe.db.commit.assert_not_called()
        self.frappe.destroy.assert_called_once()

    def test_replaces_old_symlink_without_writing_to_its_target(self):
        other = self.root / "untouched"
        other.write_text("keep me")
        (self.site / ".frappectl.env").symlink_to(other)
        path = self.configure()
        self.assertFalse(path.is_symlink())
        self.assertEqual(other.read_text(), "keep me")

    def test_instruction_updates_preserve_custom_content_and_are_idempotent(self):
        path = self.home / "AGENTS.md"
        path.write_text("My instructions\n")
        source = TEMPLATE / "agent-instructions.md"
        MODULE.install_instructions(self.home, source)
        first = path.read_bytes()
        MODULE.install_instructions(self.home, source)
        self.assertEqual(path.read_bytes(), first)
        self.assertTrue(path.read_text().startswith("My instructions\n"))
        self.assertEqual(path.read_text().count("frappe-devcontainer:begin"), 1)

    def test_existing_global_override_receives_guidance(self):
        normal, override = self.home / "AGENTS.md", self.home / "AGENTS.override.md"
        normal.write_text("Base instructions\n")
        override.write_text("Override instructions\n")
        MODULE.install_instructions(self.home, TEMPLATE / "agent-instructions.md")
        self.assertEqual(normal.read_text(), "Base instructions\n")
        self.assertIn("frappectl guide", override.read_text())

    def test_damaged_instruction_markers_leave_original_untouched(self):
        path = self.home / "AGENTS.md"
        original = "My instructions\n<!-- frappe-devcontainer:begin -->\n"
        path.write_text(original)
        with self.assertRaises(RuntimeError):
            MODULE.install_instructions(self.home, TEMPLATE / "agent-instructions.md")
        self.assertEqual(path.read_text(), original)


class FrappectlCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        site = self.root / "workspace/test-bench/sites/test.localhost"
        site.mkdir(parents=True)
        (site / ".frappectl.env").write_text(
            "FRAPPE_SITE=http://test.localhost:8000\n"
            f"FRAPPE_API_KEY={shlex.quote('local key')}\n"
            f"FRAPPE_API_SECRET={shlex.quote('local secret')}\n"
        )
        binary = self.root / ".local/lib/frappectl/bin/frappectl"
        binary.parent.mkdir(parents=True)
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "print(json.dumps({key: os.environ.get(key) for key in "
            "['FRAPPE_SITE', 'FRAPPE_API_KEY', 'FRAPPE_API_SECRET', "
            "'FRAPPE_READ_ONLY', 'NO_PROXY', 'no_proxy']}))\n"
        )
        binary.chmod(0o755)
        self.wrapper = self.root / "frappectl"
        # Emulate the container's workspace mount without requiring Docker here.
        self.wrapper.write_text(
            (TEMPLATE / "frappectl")
            .read_text()
            .replace("/workspace/development", str(self.root / "workspace"))
        )
        self.environment = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "FRAPPE_BENCH_NAME": "test-bench",
            "FRAPPE_SITE_NAME": "test.localhost",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, *args, **environment):
        result = subprocess.run(
            ["bash", str(self.wrapper), *args],
            env=self.environment | environment,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertNotIn("local secret", result.stderr)
        return json.loads(result.stdout)

    def test_noninteractive_commands_receive_local_credentials_and_skip_proxy(self):
        result = self.command("auth", "whoami", NO_PROXY="company.example")
        self.assertEqual(result["FRAPPE_SITE"], "http://test.localhost:8000")
        self.assertEqual(result["FRAPPE_API_KEY"], "local key")
        self.assertEqual(result["FRAPPE_API_SECRET"], "local secret")
        self.assertIn("company.example", result["NO_PROXY"])
        self.assertIn("test.localhost", result["NO_PROXY"])
        self.assertIn("test.localhost", result["no_proxy"])

    def test_explicit_profiles_do_not_receive_local_credentials(self):
        for args in (
            ["-s", "other", "auth", "whoami"],
            ["--site=other", "auth", "whoami"],
            ["auth", "whoami", "--site", "other"],
            ["-s=other", "auth", "whoami"],
        ):
            with self.subTest(args=args):
                result = self.command(*args)
                self.assertIsNone(result["FRAPPE_SITE"])
                self.assertIsNone(result["FRAPPE_API_SECRET"])

    def test_explicit_or_partial_credentials_are_never_mixed_with_local_ones(self):
        result = self.command("auth", "whoami", FRAPPE_SITE="https://other.example")
        self.assertEqual(result["FRAPPE_SITE"], "https://other.example")
        self.assertIsNone(result["FRAPPE_API_KEY"])
        self.assertIsNone(result["FRAPPE_API_SECRET"])

    def test_read_only_environment_option_remains_effective(self):
        result = self.command("doc", "list", "ToDo", FRAPPE_READ_ONLY="1")
        self.assertEqual(result["FRAPPE_READ_ONLY"], "1")
        self.assertEqual(result["FRAPPE_API_KEY"], "local key")


if __name__ == "__main__":
    unittest.main()
