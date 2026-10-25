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
deployment state, the state pointer, and the default rendered outputs),
T-Pot runtime data directories under DeepPrint/data/ (root-owned
Compose bind-mount sources on default deploys), and any custom personas
under DeepPrint/footprints/ survive the refresh, so updating never
orphans, deletes, or breaks an existing deployment. Runtime data is left
exactly where it is: docker owns it, the updating user cannot move or
remove it, and bind mounts keep following the same inode.
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

# Deploy-time data directories the T-Pot Compose project creates inside the
# package tree. Default deploys run the Compose project from the package
# directory, so `./data/...` bind-mount sources land under DeepPrint/data/.
# Docker creates them root-owned (often while containers hold them mounted),
# so updates leave them exactly where they are instead of trying to delete,
# overwrite, or move them.
DEPLOY_DATA_DIRS = ("data",)


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


def _move_or_copy(source: Path, target: Path) -> None:
    """Relocate a preserved artifact into the staged tree.

    A rename keeps ownership, permissions, and any live bind mounts exactly
    as deployed (and works for docker-owned entries a non-root updater could
    not read back); the copy fallback covers staging roots that somehow
    ended up on another filesystem.
    """
    try:
        os.rename(source, target)
        return
    except OSError:
        pass
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


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
        if target.exists():
            # Staged trees never ship these names; if one does, the staged
            # version wins. Clear the way before moving the deployed file in.
            if target.is_dir():
                safe_rmtree(target)
            else:
                target.unlink()
        _move_or_copy(source, target)


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
        _move_or_copy(source, target)


def _warn_problem(problem: str | None) -> None:
    if problem:
        print(f"WARNING: {problem}", file=sys.stderr)


def _remove_entry(entry: Path) -> str | None:
    """Delete an installed entry, best-effort; returns a problem or None."""
    try:
        if entry.is_dir() and not entry.is_symlink():
            safe_rmtree(entry)
        else:
            entry.unlink()
    except OSError as exc:
        return (
            f"could not remove {entry} ({exc}); it was left in place -- "
            "remove it manually once the update is verified"
        )
    return None


def _replace_entry(source: Path, target: Path) -> str | None:
    """Move one staged entry over its installed counterpart, best-effort.

    The swap happens one entry at a time (rather than replacing the whole
    tree) precisely so that individual docker-owned leftovers degrade into
    a warning instead of aborting the update.
    """
    if target.exists() or target.is_symlink():
        problem = _remove_entry(target)
        if problem:
            return f"{problem} (the installed entry was kept as-is)"
    try:
        os.rename(source, target)
    except OSError as exc:
        return f"could not install {target} ({exc})"
    return None


def _swap_install(staging_root: Path) -> Path:
    """Replace the install at INSTALL_ROOT with the staged tree, in place.

    T-Pot runtime data under DeepPrint/data/ is docker-owned (root, often
    mode 0700 while containers hold it mounted): the updating user cannot
    delete it, read it, or even rename it across directories (moving a
    directory requires write permission on the directory itself). So the
    data dirs are never touched -- staged entries are moved over their
    installed counterparts one by one, around the data dirs. Everything
    stays on the same filesystem and on the same path, so live bind mounts
    and the recorded tpot-root path remain valid. Any other entry that
    cannot be replaced produces a warning instead of aborting.
    """
    install_root = INSTALL_ROOT.resolve()
    if install_root.exists() and not install_root.is_dir():
        install_root.unlink()
    install_root.mkdir(parents=True, exist_ok=True)
    current_package = install_root / "DeepPrint"
    staged_package = staging_root / "DeepPrint"
    if current_package.is_dir() and staged_package.is_dir():
        _preserve_runtime_files(current_package, staged_package)
        _preserve_custom_footprints(current_package, staged_package)
    current_package.mkdir(parents=True, exist_ok=True)

    staged_top = {entry.name for entry in staging_root.iterdir()}
    for entry in sorted(staging_root.iterdir()):
        if entry.name == "DeepPrint":
            continue
        _warn_problem(_replace_entry(entry, install_root / entry.name))

    if staged_package.is_dir():
        staged_package_names = {entry.name for entry in staged_package.iterdir()}
        for entry in sorted(staged_package.iterdir()):
            if entry.name in DEPLOY_DATA_DIRS:
                # Docker-owned runtime data: never deleted, never
                # overwritten, never moved. A bind mount follows the
                # inode, so the running stack keeps its data and the
                # recorded tpot-root path stays valid.
                continue
            _warn_problem(_replace_entry(entry, current_package / entry.name))
    else:
        staged_package_names = set()

    # Prune installed entries that no longer ship upstream. Runtime data
    # dirs are exempt, and preserved artifacts were already moved into the
    # staged tree, so they appear in the staged listings above.
    keep_top = staged_top | {"DeepPrint"}
    for entry in sorted(install_root.iterdir()):
        if entry.name in keep_top:
            continue
        _warn_problem(_remove_entry(entry))
    keep_package = staged_package_names | set(DEPLOY_DATA_DIRS)
    for entry in sorted(current_package.iterdir()):
        if entry.name in keep_package:
            continue
        _warn_problem(_remove_entry(entry))

    _warn_problem(_remove_entry(staging_root))
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
    # T-Pot runtime data (Compose bind-mount sources on default deploys)
    # sits under DeepPrint/data/, is docker-owned, and stays exactly where
    # it is across updates; it never counts as a source change.
    if len(parts) >= 2 and parts[0] == "DeepPrint" and parts[1] in DEPLOY_DATA_DIRS:
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
        try:
            content = path.read_bytes()
        except OSError:
            # Unreadable runtime artifacts (docker-owned files) cannot be
            # compared; never let one abort the update check.
            continue
        digests[relative] = hashlib.sha256(content).hexdigest()
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
