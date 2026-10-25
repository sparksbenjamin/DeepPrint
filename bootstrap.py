#!/usr/bin/env python3
"""DeepPrint bootstrap installer and self-updater.

Fresh install (one line on a live T-Pot host):

    curl -fsSL https://raw.githubusercontent.com/sparksbenjamin/DeepPrint/main/bootstrap.py | python3 -

Update an existing install in place, without re-running the wizard:

    python3 ~/.deepprint/bootstrap.py --update

The updater refreshes the installed sources (personas, base Compose
templates, engine, bootstrap itself, and update.sh) from the same GitHub
ref used at install time, then re-applies the saved deployment with the
engine's --update mode. Runtime files written by previous deploys (the
deployment state, the state pointer, and the default rendered outputs)
and any custom personas under DeepPrint/footprints/ are carried across
the refresh, so updating never orphans or deletes an existing
deployment.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


REPO_OWNER = "sparksbenjamin"
REPO_NAME = "DeepPrint"
REPO_REF = os.environ.get("DEEPPRINT_REF", "main")
REPO_ZIP_URL = (
    f"https://github.com/{REPO_OWNER}/{REPO_NAME}/archive/refs/heads/{REPO_REF}.zip"
)
INSTALL_ROOT = Path(
    os.environ.get("DEEPPRINT_HOME", str(Path.home() / ".deepprint"))
).expanduser()

# Files staged at the install root in addition to the engine package.
STAGED_FILES = ("README.md", "LICENSE", "bootstrap.py", "update.sh")

# Runtime artifacts the engine writes into its own package directory during
# deploys. They are carried across installs and updates so a refresh never
# orphans an existing deployment.
PRESERVED_RUNTIME_FILES = (
    ".deepprint_state.json",
    ".deepprint_state_pointer.json",
    "tpot_deepprint.yml",
    ".env.deepprint",
    ".deepprint_rendered",
)


def ensure_pyyaml() -> None:
    try:
        import yaml  # noqa: F401
        return
    except ImportError:
        pass

    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        subprocess.run(
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            check=True,
        )

    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--user", "PyYAML"],
        check=True,
    )


def _handle_remove_readonly(func, path, exc_info) -> None:
    del exc_info
    os.chmod(path, stat.S_IWRITE)
    func(path)


def safe_rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, onerror=_handle_remove_readonly)


def find_repo_root(base_dir: Path) -> Path:
    for candidate in base_dir.rglob("deepprint.py"):
        if candidate.parent.name == "DeepPrint":
            return candidate.parent.parent
    raise RuntimeError("Unable to locate DeepPrint sources after download.")


def download_or_copy_repo() -> Path:
    source_override = os.environ.get("DEEPPRINT_BOOTSTRAP_SOURCE")
    temp_root = Path(tempfile.mkdtemp(prefix="deepprint-bootstrap-"))

    if source_override:
        override_path = Path(source_override).expanduser()
        if override_path.is_dir():
            destination = temp_root / "repo"
            shutil.copytree(override_path, destination)
            return destination
        if override_path.is_file():
            with zipfile.ZipFile(override_path) as archive:
                archive.extractall(temp_root)
            return find_repo_root(temp_root)
        raise RuntimeError(
            f"DEEPPRINT_BOOTSTRAP_SOURCE does not exist: {override_path}"
        )

    archive_path = temp_root / "deepprint.zip"
    with urllib.request.urlopen(REPO_ZIP_URL) as response, archive_path.open("wb") as handle:
        shutil.copyfileobj(response, handle)

    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(temp_root)

    return find_repo_root(temp_root)


def build_staging(repo_root: Path) -> Path:
    """Materialise a fresh install tree for repo_root beside INSTALL_ROOT."""
    install_root = INSTALL_ROOT.resolve()
    install_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix="deepprint-install-", dir=str(install_root.parent))
    )
    shutil.copytree(
        repo_root / "DeepPrint",
        staging_root / "DeepPrint",
        ignore=shutil.ignore_patterns(
            "__pycache__",
            ".pytest_cache",
            ".deepprint_rendered",
            "*.pyc",
            "*.pyo",
        ),
    )
    for file_name in STAGED_FILES:
        source_file = repo_root / file_name
        if source_file.exists():
            shutil.copy2(source_file, staging_root / file_name)
    return staging_root


def _preserve_runtime_files(current_package: Path, staged_package: Path) -> None:
    """Carry deploy-time runtime artifacts from the current install forward."""
    for name in PRESERVED_RUNTIME_FILES:
        source = current_package / name
        if not source.exists():
            continue
        target = staged_package / name
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)


def _preserve_custom_footprints(current_package: Path, staged_package: Path) -> None:
    """Keep personas the user added that the upstream release does not ship."""
    current_footprints = current_package / "footprints"
    staged_footprints = staged_package / "footprints"
    if not current_footprints.is_dir():
        return
    for source in sorted(current_footprints.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(current_footprints)
        target = staged_footprints / relative
        if target.exists():
            # Upstream ships this persona path: the released version wins.
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _swap_install(staging_root: Path) -> Path:
    """Replace the install at INSTALL_ROOT with the staged tree."""
    install_root = INSTALL_ROOT.resolve()
    current_package = install_root / "DeepPrint"
    if current_package.is_dir():
        _preserve_runtime_files(current_package, staging_root / "DeepPrint")
        _preserve_custom_footprints(current_package, staging_root / "DeepPrint")
    if install_root.exists():
        safe_rmtree(install_root)
    staging_root.replace(install_root)
    return install_root


def install_repo(repo_root: Path) -> Path:
    return _swap_install(build_staging(repo_root))


def _is_comparison_ignorable(relative_path: str) -> bool:
    parts = relative_path.split("/")
    if "__pycache__" in parts or ".pytest_cache" in parts:
        return True
    if parts[-1].endswith((".pyc", ".pyo")):
        return True
    # Runtime artifacts (state, pointer, rendered outputs) sit inside the
    # package dir and may be nested (e.g. .deepprint_rendered/snippets);
    # they are carried across updates and never count as a source change.
    if len(parts) >= 2 and parts[0] == "DeepPrint" and parts[1] in (
        PRESERVED_RUNTIME_FILES
    ):
        return True
    return False


def _tree_digests(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if _is_comparison_ignorable(relative):
            continue
        digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def install_differs(staging_root: Path) -> bool:
    """Whether the staged tree differs from the current install.

    Files present only in the current install under DeepPrint/footprints/ are
    custom personas preserved across updates; they are not a difference.
    Anything staged-but-missing (upstream added files) or digests that
    disagree (upstream changed files) is a difference.
    """
    current = INSTALL_ROOT.resolve()
    if not current.is_dir():
        return True
    staged = _tree_digests(staging_root)
    installed = _tree_digests(current)
    for relative, digest in installed.items():
        if relative in staged:
            if staged[relative] != digest:
                return True
        elif not relative.startswith("DeepPrint/footprints/"):
            return True
    return bool(set(staged) - set(installed))


def update_install(force: bool = False) -> tuple[Path, bool]:
    """Refresh the installed sources in place.

    Returns (install_root, changed). changed is False when the upstream
    tree matches the current install and force was not requested; in
    that case nothing on disk was modified.
    """
    repo_root = download_or_copy_repo()
    staging_root = build_staging(repo_root)
    if not force and not install_differs(staging_root):
        safe_rmtree(staging_root)
        return INSTALL_ROOT.resolve(), False
    return _swap_install(staging_root), True


def run_update(args: list[str]) -> int:
    """Handle `bootstrap.py --update [--force] [--dry-run] [engine flags]`."""
    force = "--force" in args
    forwarded = [arg for arg in args if arg != "--force"]
    try:
        ensure_pyyaml()
        install_root, changed = update_install(force=force)
        if not changed:
            print(
                "DeepPrint is already up to date; deployment left untouched.",
                flush=True,
            )
            return 0
    except Exception as exc:  # pragma: no cover - update failure path
        print(f"DeepPrint update failed: {exc}", file=sys.stderr)
        return 1

    engine = install_root / "DeepPrint" / "deepprint.py"
    if not engine.exists():
        print(f"DeepPrint launcher not found at {engine}", file=sys.stderr)
        return 1

    # execv replaces this process without flushing Python's buffers; flush
    # explicitly so the message reaches logs and pipes.
    print("DeepPrint sources updated; re-applying the saved deployment.", flush=True)
    command = [sys.executable, str(engine), "--update"]
    command.extend(forwarded)
    os.execv(sys.executable, command)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--update":
        return run_update(argv[1:])

    try:
        ensure_pyyaml()
        repo_root = download_or_copy_repo()
        install_root = install_repo(repo_root)
        deepprint_script = install_root / "DeepPrint" / "deepprint.py"
        if not deepprint_script.exists():
            raise RuntimeError(f"DeepPrint launcher not found at {deepprint_script}")

        passthrough_args = sys.argv[1:]
        command = [sys.executable, str(deepprint_script)]
        if not passthrough_args:
            command.append("--interactive")
        command.extend(passthrough_args)
        os.execv(sys.executable, command)
        return 0
    except Exception as exc:  # pragma: no cover - bootstrap failure path
        print(f"DeepPrint bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
