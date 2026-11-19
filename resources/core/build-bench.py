"""Assemble a Frappe bench for the application image build."""

import argparse
import ast
import base64
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from bench.config.common_site_config import setup_config
from bench.utils.app import get_app_name_from_setup, get_pyproject
from semantic_version import SimpleSpec, Version

BENCH = Path("/home/frappe/frappe-bench")


def run(*command, cwd=None, capture=False, env=None):
    return subprocess.run(
        command, cwd=cwd, check=True, text=True, capture_output=capture, env=env
    ).stdout


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def project(path):
    return get_pyproject(str(path / "pyproject.toml")) or {}


def app_name(path):
    name = project(path).get("project", {}).get("name")
    if not name and (path / "setup.cfg").is_file():
        config = configparser.ConfigParser(interpolation=None)
        config.read(path / "setup.cfg", encoding="utf-8")
        name = config.get("metadata", "name", fallback=None)
    if not name and (path / "setup.py").is_file():
        name = get_app_name_from_setup((path / "setup.py").read_text(encoding="utf-8"))
    if not name or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", name):
        raise ValueError(
            "Could not determine a valid Frappe app name from the checkout"
        )
    return name


def git_source(source, ref):
    if "://" not in source and ":" not in source and not source.startswith((".", "/")):
        source, _, suffix = source.partition("@")
        ref = ref or suffix
        if "/" not in source:
            source = f"frappe/{source}"
        source = f"https://github.com/{source}"
    return source, ref


def repository_name(source):
    path = urlsplit(source).path.rstrip("/")
    return path.rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")


def clone(source, ref, checkout):
    url = urlsplit(source)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if url.scheme in {"http", "https"} and url.username is not None:
        user = unquote(url.username)
        password = unquote(url.password or "")
        source = urlunsplit(url._replace(netloc=url.netloc.rsplit("@", 1)[-1]))
        # Git persists the clone URL. Pass credentials in per-process config instead.
        auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        index = int(env.get("GIT_CONFIG_COUNT", "0"))
        env.update(
            {
                "GIT_CONFIG_COUNT": str(index + 1),
                f"GIT_CONFIG_KEY_{index}": f"http.{source}.extraHeader",
                f"GIT_CONFIG_VALUE_{index}": f"Authorization: Basic {auth}",
            }
        )
    try:
        if ref and (ref.startswith("refs/") or re.fullmatch(r"[0-9a-fA-F]{40}", ref)):
            run("git", "init", "--quiet", str(checkout), env=env, capture=True)
            run(
                "git",
                "remote",
                "add",
                "upstream",
                source,
                cwd=checkout,
                env=env,
                capture=True,
            )
            run(
                "git",
                "fetch",
                "--depth=1",
                "upstream",
                ref,
                cwd=checkout,
                env=env,
                capture=True,
            )
            run(
                "git",
                "checkout",
                "--quiet",
                "--detach",
                "FETCH_HEAD",
                cwd=checkout,
                capture=True,
            )
        else:
            branch = [f"--branch={ref}"] if ref else []
            run(
                "git",
                "clone",
                "--depth=1",
                "--origin",
                "upstream",
                *branch,
                "--",
                source,
                str(checkout),
                env=env,
                capture=True,
            )
        return {
            "commit_hash": run(
                "git", "rev-parse", "HEAD", cwd=checkout, capture=True
            ).strip(),
            "branch": ref
            or run(
                "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=checkout, capture=True
            ).strip(),
        }
    except subprocess.CalledProcessError:
        # Git's diagnostics may contain private repository URLs.
        raise ValueError(
            f"Git checkout failed for {repository_name(source)!r} at {ref or 'HEAD'!r}"
        ) from None


def required_apps(path, name):
    hooks = path / name / "hooks.py"
    if hooks.is_file():
        for node in ast.parse(hooks.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "required_apps"
                for target in node.targets
            ):
                try:
                    requirements = ast.literal_eval(node.value)
                except ValueError:
                    raise ValueError(
                        f"{name} must declare a literal required_apps list"
                    ) from None
                if not isinstance(requirements, (list, tuple)) or not all(
                    isinstance(requirement, str) and requirement
                    for requirement in requirements
                ):
                    raise ValueError(
                        f"{name} must declare a list of required app names"
                    )
                return requirements
    return []


def fetch(args):
    bench = args.bench
    apps = bench / "apps"
    sites = bench / "sites"
    for directory in (apps, sites, bench / "logs", bench / "config/pids"):
        directory.mkdir(parents=True, exist_ok=True)
    sources = list(args.app)
    if args.apps_file and args.apps_file.is_file() and args.apps_file.stat().st_size:
        sources.extend(
            (item["url"], item.get("branch"))
            for item in json.loads(args.apps_file.read_text(encoding="utf-8"))
        )

    states = {}
    repositories = {}

    def fetch_app(source, ref):
        source, ref = git_source(source, ref)
        with tempfile.TemporaryDirectory(dir=bench, prefix=".checkout-") as temp:
            checkout = Path(temp) / "source"
            resolution = clone(source, ref, checkout)
            name = app_name(checkout)
            target = apps / name
            if target.exists():
                raise ValueError(f"App {name!r} was listed more than once")
            checkout.rename(target)
        print(f"Fetched {name} at {resolution['commit_hash']}", flush=True)
        repositories[source.rstrip("/").removesuffix(".git")] = name
        states[name] = {"resolution": resolution, "is_repo": True, "required": []}
        return name

    for source, ref in sources:
        fetch_app(source, ref)
    if "frappe" not in states:
        raise ValueError("The application list must include Frappe")

    ordered = {"frappe": states["frappe"]}
    visiting = set()

    def add(name):
        if name in ordered:
            return
        if name in visiting:
            raise ValueError(f"Circular app dependency involving {name!r}")
        visiting.add(name)
        if args.resolve_dependencies:
            for requirement in required_apps(apps / name, name):
                source, ref = git_source(requirement, None)
                dependency = repositories.get(
                    source.rstrip("/").removesuffix(".git"), repository_name(source)
                )
                if dependency not in states:
                    dependency = fetch_app(source, ref)
                add(dependency)
                states[name]["required"].append(dependency)
        ordered[name] = states[name]
        visiting.remove(name)

    for name in list(states):
        add(name)
    for index, state in enumerate(ordered.values(), start=1):
        state["idx"] = index
    (sites / "apps.txt").write_text("\n".join(ordered) + "\n", encoding="utf-8")
    write_json(sites / "apps.json", ordered)
    setup_config(str(bench))
    (bench / "patches.txt").write_bytes(
        files("bench").joinpath("patches", "patches.txt").read_bytes()
    )


def assemble(args):
    bench = args.bench
    sites = bench / "sites"
    states_path = sites / "apps.json"
    states = json.loads(states_path.read_text(encoding="utf-8"))
    python = bench / "env/bin/python"
    if shutil.which("uv"):
        run(
            "uv",
            "venv",
            "--seed",
            "--python",
            sys.executable,
            str(bench / "env"),
            cwd=bench,
        )
        install = ("uv", "pip", "install", "--python", str(python))
    else:
        run(sys.executable, "-m", "venv", str(bench / "env"), cwd=bench)
        install = (str(python), "-m", "pip", "install")
    for name, state in states.items():
        path = bench / "apps" / name
        print(f"Installing {name}", flush=True)
        run(*install, "-e", str(path), cwd=bench)
        state["version"] = run(
            str(python),
            "-c",
            "import sys; from importlib.metadata import version; print(version(sys.argv[1]))",
            name,
            cwd=bench,
            capture=True,
        ).strip()
        if (path / "package.json").is_file():
            run("yarn", "install", "--check-files", cwd=path)
    for name in states:
        requirements = (
            project(bench / "apps" / name)
            .get("tool", {})
            .get("bench", {})
            .get("frappe-dependencies", {})
        )
        for dependency, spec in requirements.items():
            version = states.get(dependency, {}).get("version")
            if not version or Version.coerce(version) not in SimpleSpec(spec):
                print(
                    f"Warning: {name} requires {dependency} {spec}, found {version or 'no app'}",
                    flush=True,
                )
    write_json(states_path, states)
    run(
        str(python),
        "-m",
        "frappe.utils.bench_helper",
        "frappe",
        "build",
        "--force",
        "--production",
        cwd=sites,
        env={**os.environ, "FRAPPE_DOCKER_BUILD": "1"},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench", type=Path, default=BENCH)
    commands = parser.add_subparsers(required=True)
    fetch_command = commands.add_parser("fetch")
    fetch_command.add_argument(
        "--app", nargs=2, action="append", required=True, metavar=("URL", "REF")
    )
    fetch_command.add_argument("--apps-file", type=Path)
    fetch_command.add_argument("--resolve-dependencies", action="store_true")
    fetch_command.set_defaults(run=fetch)
    commands.add_parser("assemble").set_defaults(run=assemble)
    args = parser.parse_args()
    args.bench = args.bench.resolve()
    try:
        args.run(args)
    except (ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
