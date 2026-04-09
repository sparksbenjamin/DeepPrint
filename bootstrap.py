#!/usr/bin/env python3
from __future__ import annotations

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


def install_repo(repo_root: Path) -> Path:
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
    for file_name in ("README.md", "LICENSE", "bootstrap.py"):
        source_file = repo_root / file_name
        if source_file.exists():
            shutil.copy2(source_file, staging_root / file_name)

    if install_root.exists():
        safe_rmtree(install_root)
    staging_root.replace(install_root)
    return install_root


def main() -> int:
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
