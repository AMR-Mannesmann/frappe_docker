"""Check installer failure propagation and existing workspace preservation."""

import os
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

    def make_existing_site(self):
        bench = self.root / self.args.bench_name
        (bench / "apps" / "frappe").mkdir(parents=True)
        site = bench / "sites" / self.args.site_name
        site.mkdir(parents=True)
        return site

    def test_existing_bench_is_not_reinitialized(self):
        bench = self.root / self.args.bench_name
        bench.mkdir()
        marker = bench / "local-work"
        marker.write_text("preserve")
        with mock.patch.object(subprocess, "check_call") as command:
            installer.init_bench_if_not_exist(self.args)
            command.assert_not_called()
        self.assertEqual(marker.read_text(), "preserve")

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


if __name__ == "__main__":
    unittest.main()
