"""Restore Pilot-format assets published by the stock apps on GitHub."""

import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

ASSET_MAPS = ("assets.json", "assets-rtl.json")


def release_tag(state):
    ref = (state["resolution"].get("branch") or "").removeprefix("refs/heads/")
    if re.fullmatch(r"version-16(?:-[A-Za-z0-9._-]+)?", ref):
        return f"assets-{ref}"
    # Stable version tags and pinned commits are normally published from version-16.
    return "assets-version-16"


def download(archive, release):
    checksum = archive.with_suffix(".gz.sha256")
    for path in (checksum, archive):
        subprocess.run(
            [
                "curl",
                "-fsSL",
                "--connect-timeout",
                "10",
                "--max-time",
                "60",
                "--output",
                str(path),
                f"{release}/{path.name}",
            ],
            check=True,
        )
    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    expected = checksum.read_text().split(maxsplit=1)
    if not expected or actual != expected[0]:
        raise ValueError(f"Checksum mismatch for {archive.name}")


def unpack(archive, directory, app, commit):
    with tarfile.open(archive, "r:gz") as package:
        manifest_entry = package.getmember("manifest.json")
        if not manifest_entry.isfile():
            raise ValueError(f"Invalid manifest in {archive.name}")
        with package.extractfile(manifest_entry) as stream:
            manifest = json.load(stream)
        if not isinstance(manifest, dict) or (
            manifest.get("app"),
            manifest.get("commit"),
        ) != (app, commit):
            raise ValueError(f"Wrong app or commit in {archive.name}")
        paths = manifest.get("paths")
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(path, str) for path in paths)
        ):
            raise ValueError(f"Missing or invalid output paths in {archive.name}")
        outputs = [PurePosixPath(path) for path in paths]
        roots = (PurePosixPath(app, "public"), PurePosixPath(app, "www"))
        for index, path in enumerate(outputs):
            if ".." in path.parts or not any(root in path.parents for root in roots):
                raise ValueError(f"Unexpected asset output: {path}")
            if any(
                path.is_relative_to(other) or other.is_relative_to(path)
                for other in outputs[:index]
            ):
                raise ValueError(f"Overlapping asset output: {path}")
        seen = set()
        for member in package.getmembers():
            path = PurePosixPath(member.name)
            if (
                path in seen
                or ".." in path.parts
                or not (member.isfile() or member.isdir())
                or (
                    path != PurePosixPath("manifest.json")
                    and not any(path.is_relative_to(output) for output in outputs)
                )
            ):
                raise ValueError(f"Unexpected asset archive entry: {member.name}")
            seen.add(path)
        package.extractall(directory, filter="data")
    return manifest, outputs


def prepare(staging, bench, states):
    outputs = []
    maps = {name: {} for name in ASSET_MAPS}
    for app, state in states.items():
        commit = state["resolution"]["commit_hash"]
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError(f"Invalid Git commit for {app}")
        release = (
            f"https://github.com/frappe/{app}/releases/download/{release_tag(state)}"
        )
        archive = staging / f"{app}-{commit}.tar.gz"
        download(archive, release)
        extracted = staging / app
        manifest, paths = unpack(archive, extracted, app, commit)
        checkout = bench / "apps" / app

        for path in paths:
            source = extracted / path
            destination = checkout / path
            if (
                not source.exists()
                or destination.exists()
                or destination.resolve() != destination
            ):
                raise ValueError(f"Cannot install asset output: {path}")
            outputs.append((source, destination))

        prefix = f"/assets/{app}/"
        for name in ASSET_MAPS:
            entries = manifest.get(name)
            if not isinstance(entries, dict) or not all(
                isinstance(url, str) for url in entries.values()
            ):
                raise ValueError(f"Invalid {name} for {app}")
            if name == "assets.json" and not entries:
                raise ValueError(f"Downloaded assets contain no bundles for {app}")
            if maps[name].keys() & entries.keys():
                raise ValueError(f"Duplicate entry in {name}")
            for url in entries.values():
                if not url.startswith(prefix):
                    raise ValueError(f"Unexpected asset URL: {url}")
                path = PurePosixPath(url.removeprefix(prefix))
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not (extracted / app / "public" / path).is_file()
                ):
                    raise ValueError(f"Missing asset for URL: {url}")
            maps[name].update(entries)

    return outputs, maps


def restore_github_assets(bench, states):
    """Check both downloads before writing anything to the checkout."""
    with tempfile.TemporaryDirectory(dir=bench) as temporary:
        try:
            outputs, maps = prepare(Path(temporary), bench, states)
        except (
            OSError,
            EOFError,
            KeyError,
            ValueError,
            tarfile.TarError,
            subprocess.CalledProcessError,
        ) as error:
            print(
                f"GitHub assets unavailable ({error}). Building from source.",
                flush=True,
            )
            return False

        for source, destination in outputs:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
        assets = bench / "sites/assets"
        assets.mkdir(parents=True, exist_ok=True)
        for name, entries in maps.items():
            (assets / name).write_text(json.dumps(entries, indent=2) + "\n")

    print("Restored official Frappe and ERPNext assets from GitHub.", flush=True)
    return True
