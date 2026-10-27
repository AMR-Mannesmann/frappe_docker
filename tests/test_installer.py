"""Check installer failure propagation and existing workspace preservation."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "development"))
import installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.previous_directory = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.previous_directory)
        self.args = installer.get_args_parser().parse_args([])

    def make_existing_bench(self):
        bench = self.root / self.args.bench_name
        (bench / "apps/frappe/frappe").mkdir(parents=True)
        (bench / "apps/frappe/frappe/__init__.py").write_text("")
        (bench / "env/bin").mkdir(parents=True)
        (bench / "env/bin/python").symlink_to(sys.executable)
        (bench / "sites").mkdir()
        (bench / "sites/apps.txt").write_text("frappe\n")
        (bench / "sites/common_site_config.json").write_text(
            '{"developer_mode": 1, "custom_setting": "preserve"}\n'
        )
        return bench

    def make_existing_site(self):
        bench = self.root / self.args.bench_name
        (bench / "apps" / "frappe").mkdir(parents=True)
        site = bench / "sites" / self.args.site_name
        site.mkdir(parents=True)
        return site

    def test_existing_bench_is_not_reinitialized(self):
        bench = self.make_existing_bench()
        marker = bench / "local-work"
        marker.write_text("preserve")
        config = (bench / "sites/common_site_config.json").read_bytes()
        self.args.dev = False
        self.args.apps_json = "different-apps.json"
        with mock.patch.object(subprocess, "check_call") as command:
            installer.init_bench_if_not_exist(self.args)
            command.assert_not_called()
        self.assertEqual(marker.read_text(), "preserve")
        self.assertEqual((bench / "sites/common_site_config.json").read_bytes(), config)

    def test_incomplete_bench_stops_before_site_creation_without_modifying_it(self):
        bench = self.root / self.args.bench_name
        bench.mkdir()
        marker = bench / "local-work"
        marker.write_text("preserve")
        with mock.patch.object(subprocess, "check_call") as command:
            with mock.patch.object(installer, "create_site_in_bench") as create_site:
                with mock.patch.object(sys, "argv", ["installer.py"]):
                    with self.assertRaisesRegex(
                        RuntimeError, "Repair the existing Bench"
                    ):
                        installer.main()
                command.assert_not_called()
                create_site.assert_not_called()
        self.assertEqual(list(bench.iterdir()), [marker])
        self.assertEqual(marker.read_text(), "preserve")

    def test_missing_bench_components_are_rejected(self):
        for index, relative in enumerate(
            (
                "apps/frappe/frappe/__init__.py",
                "env/bin/python",
                "sites/apps.txt",
                "sites/common_site_config.json",
            )
        ):
            with self.subTest(component=relative):
                self.args.bench_name = f"bench-{index}"
                bench = self.make_existing_bench()
                (bench / relative).unlink()
                with mock.patch.object(subprocess, "check_call") as command:
                    with self.assertRaisesRegex(RuntimeError, "not a complete Bench"):
                        installer.init_bench_if_not_exist(self.args)
                    command.assert_not_called()

    def test_invalid_bench_config_or_registration_is_rejected(self):
        for index, (relative, contents) in enumerate(
            (
                ("sites/common_site_config.json", "{"),
                ("sites/common_site_config.json", "[]"),
                ("sites/apps.txt", "erpnext\n"),
            )
        ):
            with self.subTest(component=relative, contents=contents):
                self.args.bench_name = f"bench-{index}"
                bench = self.make_existing_bench()
                path = bench / relative
                path.write_text(contents)
                with mock.patch.object(subprocess, "check_call") as command:
                    with self.assertRaisesRegex(RuntimeError, "not a complete Bench"):
                        installer.init_bench_if_not_exist(self.args)
                    command.assert_not_called()
                self.assertEqual(path.read_text(), contents)

    def test_existing_file_or_broken_symlink_is_not_overwritten(self):
        path = self.root / self.args.bench_name
        path.write_text("preserve")
        with self.assertRaisesRegex(RuntimeError, "not a complete Bench"):
            installer.init_bench_if_not_exist(self.args)
        self.assertEqual(path.read_text(), "preserve")
        path.unlink()
        path.symlink_to(self.root / "missing-bench")
        with self.assertRaisesRegex(RuntimeError, "not a complete Bench"):
            installer.init_bench_if_not_exist(self.args)
        self.assertTrue(path.is_symlink())

    def test_existing_site_is_not_recreated(self):
        site = self.make_existing_site()
        config = site / "site_config.json"
        config.write_text('{"db_name": "existing-site-database"}')
        with mock.patch.object(
            subprocess,
            "check_output",
            return_value='{"development.localhost": ["frappe"]}',
        ):
            with mock.patch.object(subprocess, "check_call") as command:
                installer.create_site_in_bench(self.args)
                command.assert_not_called()
        self.assertEqual(config.read_text(), '{"db_name": "existing-site-database"}')

    def test_existing_site_with_unusable_database_stops_setup(self):
        self.make_existing_site()
        failure = subprocess.CalledProcessError(1, ["bench", "list-apps"])
        with mock.patch.object(subprocess, "check_output", side_effect=failure):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.create_site_in_bench(self.args)

    def test_existing_site_with_missing_apps_stops_setup(self):
        self.make_existing_site()
        (self.root / self.args.bench_name / "apps" / "erpnext").mkdir()
        with mock.patch.object(
            subprocess,
            "check_output",
            return_value='{"development.localhost": ["frappe"]}',
        ):
            with self.assertRaisesRegex(RuntimeError, "missing apps: erpnext"):
                installer.create_site_in_bench(self.args)

    def test_failed_bench_init_stops_before_site_creation(self):
        failure = subprocess.CalledProcessError(1, ["bench", "init"])
        with mock.patch.object(subprocess, "check_call", side_effect=failure):
            with mock.patch.object(installer, "create_site_in_bench") as create_site:
                with mock.patch.object(sys, "argv", ["installer.py"]):
                    with self.assertRaises(subprocess.CalledProcessError):
                        installer.main()
                create_site.assert_not_called()

    def test_failed_site_creation_is_not_reported_as_success(self):
        apps = self.root / self.args.bench_name / "apps" / "frappe"
        apps.mkdir(parents=True)

        def command(arguments, **kwargs):
            if arguments[:2] == ["bench", "new-site"]:
                raise subprocess.CalledProcessError(1, arguments)

        with mock.patch.object(subprocess, "check_call", side_effect=command):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.create_site_in_bench(self.args)


class InstallerCommandTests(unittest.TestCase):
    """Exercise the real Bash boundary without invoking Bench or network setup."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        previous_directory = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous_directory)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "bench-calls.jsonl"
        self.node_calls = self.root / "node-calls.json"
        self.make_executable(
            "bench",
            """import json
import os
import sys
from pathlib import Path

with open(os.environ["BENCH_TEST_CALLS"], "a") as handle:
    handle.write(json.dumps({"args": sys.argv[1:], "python": os.environ.get("PYENV_VERSION")}) + "\\n")
if sys.argv[1] == "init":
    Path(sys.argv[-1]).mkdir()
""",
        )
        shell_config = self.root / "bashrc"
        shell_config.write_text("""nvm() {
  printf '%s\\n' "$@" > "$BENCH_TEST_NODE_CALLS"
  return "${BENCH_TEST_NODE_STATUS:-0}"
}
""")
        environment = mock.patch.dict(
            os.environ,
            {
                "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                "BENCH_TEST_CALLS": str(self.calls),
                "BENCH_TEST_NODE_CALLS": str(self.node_calls),
                "BASH_ENV": "/dev/null",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        check_call = subprocess.check_call

        def controlled_shell(command, **kwargs):
            if command[0] == "/bin/bash":
                command = [command[0], "--rcfile", str(shell_config), *command[1:]]
            return check_call(command, **kwargs)

        shell = mock.patch.object(
            subprocess, "check_call", side_effect=controlled_shell
        )
        shell.start()
        self.addCleanup(shell.stop)

    def make_executable(self, name, source):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)

    def run_init(self, arguments):
        args = installer.get_args_parser().parse_args(arguments)
        installer.init_bench_if_not_exist(args)
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_development_dependencies_are_enabled_by_default(self):
        calls = self.run_init([])
        self.assertIn("--dev", calls[0]["args"])
        self.assertFalse(any("developer_mode" in call["args"] for call in calls[1:]))

    def test_no_dev_omits_the_flag_and_does_not_enable_developer_mode_later(self):
        calls = self.run_init(["--no-dev"])
        self.assertNotIn("--dev", calls[0]["args"])
        self.assertFalse(any("developer_mode" in call["args"] for call in calls[1:]))

    def test_arguments_reach_bench_and_nvm_literally(self):
        values = {
            "--bench-name": "bench with spaces; touch BENCH_INJECTED",
            "--apps-json": "apps' $(touch APPS_INJECTED).json",
            "--frappe-repo": "https://example.invalid/a repo.git; touch REPO_INJECTED",
            "--frappe-branch": "branch; touch BRANCH_INJECTED",
            "--node-version": "24; touch NODE_INJECTED",
            "--py-version": "3.14; touch PYTHON_INJECTED",
        }
        arguments = [item for pair in values.items() for item in pair]
        calls = self.run_init(arguments + ["--verbose"])
        init = calls[0]
        self.assertEqual(init["args"][-1], values["--bench-name"])
        self.assertIn(f"--apps_path={values['--apps-json']}", init["args"])
        self.assertIn(f"--frappe-path={values['--frappe-repo']}", init["args"])
        self.assertIn(f"--frappe-branch={values['--frappe-branch']}", init["args"])
        self.assertIn("--verbose", init["args"])
        self.assertEqual(init["python"], values["--py-version"])
        self.assertEqual(
            self.node_calls.read_text().splitlines(), ["use", values["--node-version"]]
        )
        for name in ("BENCH", "APPS", "REPO", "BRANCH", "NODE", "PYTHON"):
            self.assertFalse((self.root / f"{name}_INJECTED").exists())

    def test_failed_node_selection_never_calls_bench_or_creates_a_site(self):
        with mock.patch.dict(os.environ, {"BENCH_TEST_NODE_STATUS": "17"}):
            with mock.patch.object(installer, "create_site_in_bench") as create_site:
                with mock.patch.object(
                    sys, "argv", ["installer.py", "--node-version", "missing-version"]
                ):
                    with self.assertRaises(subprocess.CalledProcessError) as failure:
                        installer.main()
                create_site.assert_not_called()
        self.assertEqual(failure.exception.returncode, 17)
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.root / "frappe-bench").exists())

    def test_devcontainer_forwards_the_development_opt_out(self):
        config = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "development/.devcontainer/devcontainer.json"
            ).read_text()
        )
        hook = (
            config["postCreateCommand"][-1].split('\n  cd "$FRAPPE_BENCH_NAME"', 1)[0]
            + "\nfi\n"
        )
        hook = hook.replace(
            "cd /workspace/development", f"cd {shlex.quote(str(self.root))}"
        )
        captured = self.root / "installer-arguments.json"
        self.make_executable("sudo", "")
        self.make_executable(
            "python",
            f"import json, sys\nfrom pathlib import Path\nPath({str(captured)!r}).write_text(json.dumps(sys.argv[1:]))\n",
        )
        environment = {
            "FRAPPE_DEV_AGENTS": "1",
            "FRAPPE_APPS_JSON": "apps with spaces.json",
            "FRAPPE_BENCH_NAME": "my-bench",
            "FRAPPE_SITE_NAME": "test.localhost",
        }
        for setting in ("1", "0", ""):
            with self.subTest(setting=setting):
                with mock.patch.dict(
                    os.environ, {**environment, "FRAPPE_BENCH_DEV": setting}
                ):
                    subprocess.run(
                        ["bash", "-ec", hook], check=True, capture_output=True
                    )
                arguments = json.loads(captured.read_text())
                self.assertEqual("--no-dev" in arguments, setting == "0")
                self.assertEqual(
                    arguments[arguments.index("--apps-json") + 1],
                    "apps with spaces.json",
                )


if __name__ == "__main__":
    unittest.main()
