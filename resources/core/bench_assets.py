"""Save and restore compiled assets for the stock Frappe and ERPNext v16 image."""

import hashlib
import io
import json
import platform
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

OUTPUTS = (
    "apps/frappe/frappe/public/dist",
    "apps/frappe/frappe/public/studio",
    "apps/erpnext/erpnext/public/dist",
    "apps/erpnext/erpnext/public/banking",
    "apps/erpnext/erpnext/www/banking.html",
    "sites/assets",
)


class BenchAssets:
    def __init__(self, bench, states, run):
        self.bench = bench
        self.manifest = {
            "format": 1,
            "apps": {
                name: state["resolution"]["commit_hash"]
                for name, state in states.items()
            },
            "os": platform.freedesktop_os_release().get("PRETTY_NAME"),
            "architecture": platform.machine(),
            "python": sys.version,
            "node": run("node", "--version", capture=True).strip(),
            "yarn": run("yarn", "--version", capture=True).strip(),
        }
        self.encoded_manifest = json.dumps(self.manifest, sort_keys=True).encode()
        self.key = hashlib.sha256(self.encoded_manifest).hexdigest()

    def archive_path(self, directory):
        return directory / f"{self.key}.tar.gz"

    def restore(self, directory):
        archive = self.archive_path(directory)
        checksum = archive.with_suffix(".gz.sha256")
        if not archive.is_file() or not checksum.is_file():
            print(f"No prebuilt bench assets for {self.key}", flush=True)
            return False
        with archive.open("rb") as stream:
            if (
                hashlib.file_digest(stream, "sha256").hexdigest()
                != checksum.read_text().strip()
            ):
                raise ValueError(f"Asset archive checksum mismatch: {archive.name}")
        with tempfile.TemporaryDirectory(dir=self.bench) as temporary:
            root = Path(temporary)
            with tarfile.open(archive, mode="r:gz") as package:
                outputs = tuple(map(PurePosixPath, OUTPUTS))
                seen = set()
                for member in package.getmembers():
                    path = PurePosixPath(member.name)
                    if (
                        path in seen
                        or ".." in path.parts
                        or not (member.isdir() or member.isfile())
                        or (
                            member.name != "manifest.json"
                            and not any(
                                path.is_relative_to(output) for output in outputs
                            )
                        )
                    ):
                        raise ValueError(
                            f"Unexpected asset archive entry: {member.name!r}"
                        )
                    seen.add(path)
                if json.load(package.extractfile("manifest.json")) != self.manifest:
                    raise ValueError(
                        f"Asset archive inputs do not match: {archive.name}"
                    )
                package.extractall(root, filter="data")
            for relative in OUTPUTS:
                if not (root / relative).exists() or (self.bench / relative).exists():
                    raise ValueError(f"Cannot restore asset output: {relative}")
            for relative in OUTPUTS:
                target = self.bench / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                (root / relative).rename(target)
        print(f"Restored prebuilt bench assets from {archive.name}", flush=True)
        return True

    def save(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        target = self.archive_path(directory)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            archive = Path(temporary) / target.name
            with tarfile.open(
                archive, mode="w:gz", compresslevel=1, dereference=False
            ) as package:
                manifest = tarfile.TarInfo("manifest.json")
                manifest.size = len(self.encoded_manifest)
                package.addfile(manifest, io.BytesIO(self.encoded_manifest))
                for relative in OUTPUTS:
                    package.add(
                        self.bench / relative,
                        arcname=relative,
                        filter=lambda entry: (
                            entry if entry.isfile() or entry.isdir() else None
                        ),
                    )
            checksum = archive.with_suffix(".gz.sha256")
            with archive.open("rb") as stream:
                checksum.write_text(
                    hashlib.file_digest(stream, "sha256").hexdigest() + "\n"
                )
            archive.replace(target)
            checksum.replace(target.with_suffix(".gz.sha256"))
        print(f"Saved prebuilt bench assets to {target.name}", flush=True)


def finish_restored_assets(bench, python, run, env, *, translate=()):
    # Studio runs a Vite build when users publish, even if its editor is prebuilt.
    studio = bench / "apps/frappe/studio/frontend"
    if (studio / "package.json").is_file():
        run("yarn", "install", "--frozen-lockfile", cwd=studio)
    run(
        str(python),
        "-c",
        """import sys
import frappe
from frappe.bundler import setup, make_asset_dirs

frappe.init("")
setup()
make_asset_dirs()

if sys.argv[1:]:
    from frappe.gettext.translate import compile_translations
    for app in sys.argv[1:]:
        compile_translations(app, force=True)
""",
        *translate,
        cwd=bench / "sites",
        env=env,
    )
    for name in ("assets.json", "assets-rtl.json"):
        urls = json.loads((bench / "sites/assets" / name).read_text())
        for url in urls.values():
            if url.startswith("/assets/") and not (bench / "sites" / url[1:]).is_file():
                raise ValueError(f"Restored asset could not be found: {url}")
