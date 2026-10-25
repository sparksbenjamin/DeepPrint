from __future__ import annotations

import contextlib
import errno
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bootstrap


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_installs_local_copy_and_launches_cli(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deepprint-bootstrap-") as temp_dir:
            install_root = Path(temp_dir) / ".deepprint"
            captured: dict[str, object] = {}

            def fake_execv(executable: str, command: list[str]) -> None:
                captured["executable"] = executable
                captured["command"] = command
                raise SystemExit(0)

            with patch.object(bootstrap, "ensure_pyyaml", return_value=None), patch.object(
                bootstrap, "INSTALL_ROOT", install_root
            ), patch.dict(
                os.environ,
                {"DEEPPRINT_BOOTSTRAP_SOURCE": str(PROJECT_ROOT)},
                clear=False,
            ), patch.object(
                sys, "argv", ["bootstrap.py", "--list-personas"]
            ), patch.object(
                bootstrap.os, "execv", side_effect=fake_execv
            ):
                with self.assertRaises(SystemExit) as exc:
                    bootstrap.main()

            self.assertEqual(0, exc.exception.code)
            self.assertTrue((install_root / "DeepPrint" / "deepprint.py").exists())
            self.assertEqual(sys.executable, captured["command"][0])
            self.assertIn("--list-personas", captured["command"])


class BootstrapUpdateTests(unittest.TestCase):
    """Self-update flow for bootstrap (zip) installs."""

    def _fixture_repo(self, base: Path, name: str = "upstream") -> Path:
        """Copy the working tree as a stand-in for the upstream release."""
        repo = base / name
        shutil.copytree(
            PROJECT_ROOT,
            repo,
            ignore=shutil.ignore_patterns(
                ".git",
                "__pycache__",
                ".pytest_cache",
                ".deepprint_rendered",
                ".deepprint_state.json",
                ".deepprint_state_pointer.json",
                "tpot_deepprint.yml",
                ".env.deepprint",
                "*.pyc",
            ),
        )
        return repo

    def _seed_runtime_state(self, install_root: Path) -> None:
        """Write the runtime artifacts a previous deploy leaves behind."""
        package = install_root / "DeepPrint"
        (package / ".deepprint_state.json").write_text('{"version": 1, "saved": true}')
        (package / ".deepprint_state_pointer.json").write_text('{"version": 1}')
        (package / "tpot_deepprint.yml").write_text("saved compose")
        (package / ".env.deepprint").write_text("saved env")
        rendered = package / ".deepprint_rendered"
        rendered.mkdir()
        (rendered / "snippet.txt").write_text("saved snippet")
        custom = package / "footprints" / "custom_persona_dir"
        custom.mkdir(parents=True, exist_ok=True)
        (custom / "persona.yaml").write_text("name: custom\n")

    def _assert_runtime_state_preserved(self, install_root: Path) -> None:
        package = install_root / "DeepPrint"
        self.assertEqual(
            '{"version": 1, "saved": true}',
            (package / ".deepprint_state.json").read_text(),
        )
        self.assertEqual(
            '{"version": 1}', (package / ".deepprint_state_pointer.json").read_text()
        )
        self.assertEqual("saved compose", (package / "tpot_deepprint.yml").read_text())
        self.assertEqual("saved env", (package / ".env.deepprint").read_text())
        self.assertEqual(
            "saved snippet",
            (package / ".deepprint_rendered" / "snippet.txt").read_text(),
        )
        self.assertTrue(
            (package / "footprints" / "custom_persona_dir" / "persona.yaml").exists()
        )

    def test_install_stages_update_sh(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deepprint-upd-") as temp_dir:
            base = Path(temp_dir)
            repo = self._fixture_repo(base)
            install_root = base / ".deepprint"
            with patch.object(bootstrap, "INSTALL_ROOT", install_root):
                bootstrap.install_repo(repo)
            self.assertTrue((install_root / "update.sh").exists())
            self.assertTrue((install_root / "bootstrap.py").exists())
            self.assertTrue((install_root / "DeepPrint" / "deepprint.py").exists())

    def test_reinstall_preserves_runtime_state_and_custom_personas(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deepprint-upd-") as temp_dir:
            base = Path(temp_dir)
            repo = self._fixture_repo(base)
            install_root = base / ".deepprint"
            with patch.object(bootstrap, "INSTALL_ROOT", install_root):
                bootstrap.install_repo(repo)
                self._seed_runtime_state(install_root)
                # A plain re-install must not orphan the saved deployment.
                bootstrap.install_repo(repo)
            self._assert_runtime_state_preserved(install_root)

    def test_update_install_noop_when_upstream_identical(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deepprint-upd-") as temp_dir:
            base = Path(temp_dir)
            repo = self._fixture_repo(base)
            install_root = base / ".deepprint"
            with patch.object(
                bootstrap, "INSTALL_ROOT", install_root
            ), patch.dict(os.environ, {"DEEPPRINT_BOOTSTRAP_SOURCE": str(repo)}):
                bootstrap.update_install()
                install_root, changed = bootstrap.update_install()
            self.assertFalse(changed)
            self.assertTrue((install_root / "DeepPrint" / "deepprint.py").exists())

    def test_update_install_noop_with_runtime_state_and_custom_personas(self) -> None:
        """A deployed install with state + custom personas is still a no-op."""
        with tempfile.TemporaryDirectory(prefix="deepprint-upd-") as temp_dir:
            base = Path(temp_dir)
            repo = self._fixture_repo(base)
            install_root = base / ".deepprint"
            with patch.object(bootstrap, "INSTALL_ROOT", install_root):
                bootstrap.install_repo(repo)
                self._seed_runtime_state(install_root)
                with patch.dict(
                    os.environ, {"DEEPPRINT_BOOTSTRAP_SOURCE": str(repo)}
                ):
                    _, changed = bootstrap.update_install()
            self.assertFalse(changed)
            self._assert_runtime_state_preserved(install_root)

    def test_update_install_swaps_and_preserves_when_upstream_changed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deepprint-upd-") as temp_dir:
            base = Path(temp_dir)
            repo = self._fixture_repo(base)
            newer = self._fixture_repo(base, name="newer")
            newer.joinpath("DeepPrint", "deepprint.py").write_text(
                newer.joinpath("DeepPrint", "deepprint.py").read_text()
                + "\n# upstream rev 2\n"
            )
            install_root = base / ".deepprint"
            with patch.object(bootstrap, "INSTALL_ROOT", install_root):
                bootstrap.install_repo(repo)
                self._seed_runtime_state(install_root)
                with patch.dict(
                    os.environ, {"DEEPPRINT_BOOTSTRAP_SOURCE": str(newer)}
                ):
                    result, changed = bootstrap.update_install()
            self.assertTrue(changed)
            self.assertEqual(install_root, result)
            self.assertIn(
                "# upstream rev 2",
                (install_root / "DeepPrint" / "deepprint.py").read_text(),
            )
            self._assert_runtime_state_preserved(install_root)

    def test_update_install_force_reinstalls_identical_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deepprint-upd-") as temp_dir:
            base = Path(temp_dir)
            repo = self._fixture_repo(base)
            install_root = base / ".deepprint"
            with patch.object(
                bootstrap, "INSTALL_ROOT", install_root
            ), patch.dict(os.environ, {"DEEPPRINT_BOOTSTRAP_SOURCE": str(repo)}):
                bootstrap.install_repo(repo)
                _, changed = bootstrap.update_install(force=True)
            self.assertTrue(changed)

    def test_update_mode_execs_engine_update(self) -> None:
        captured: dict[str, object] = {}

        def fake_execv(executable: str, command: list[str]) -> None:
            captured["executable"] = executable
            captured["command"] = command
            raise SystemExit(0)

        with tempfile.TemporaryDirectory(prefix="deepprint-upd-") as temp_dir:
            base = Path(temp_dir)
            repo = self._fixture_repo(base)
            newer = self._fixture_repo(base, name="newer")
            newer.joinpath("DeepPrint", "deepprint.py").write_text(
                newer.joinpath("DeepPrint", "deepprint.py").read_text()
                + "\n# upstream rev 2\n"
            )
            install_root = base / ".deepprint"
            with patch.object(
                bootstrap, "ensure_pyyaml", return_value=None
            ), patch.object(
                bootstrap, "INSTALL_ROOT", install_root
            ), patch.dict(
                os.environ, {"DEEPPRINT_BOOTSTRAP_SOURCE": str(newer)}
            ), patch.object(
                sys, "argv", ["bootstrap.py", "--update", "--dry-run"]
            ), patch.object(
                bootstrap.os, "execv", side_effect=fake_execv
            ):
                with self.assertRaises(SystemExit) as exc:
                    bootstrap.main()

            self.assertEqual(0, exc.exception.code)
            engine_path = install_root / "DeepPrint" / "deepprint.py"
            self.assertEqual(sys.executable, captured["executable"])
            self.assertEqual(
                [sys.executable, str(engine_path), "--update", "--dry-run"],
                captured["command"],
            )
            self.assertIn(
                "# upstream rev 2", engine_path.read_text()
            )

    def test_update_mode_already_current_returns_zero_without_exec(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deepprint-upd-") as temp_dir:
            base = Path(temp_dir)
            repo = self._fixture_repo(base)
            install_root = base / ".deepprint"
            with patch.object(
                bootstrap, "INSTALL_ROOT", install_root
            ), patch.dict(
                os.environ, {"DEEPPRINT_BOOTSTRAP_SOURCE": str(repo)}
            ):
                # Establish an install that already matches upstream.
                bootstrap.install_repo(repo)
            with patch.object(
                bootstrap, "ensure_pyyaml", return_value=None
            ), patch.object(
                bootstrap, "INSTALL_ROOT", install_root
            ), patch.dict(
                os.environ, {"DEEPPRINT_BOOTSTRAP_SOURCE": str(repo)}
            ), patch.object(
                sys, "argv", ["bootstrap.py", "--update"]
            ), patch.object(
                bootstrap.os, "execv", side_effect=AssertionError("must not exec")
            ):
                self.assertEqual(0, bootstrap.main())
            self.assertTrue((install_root / "DeepPrint" / "deepprint.py").exists())


    def _seed_deploy_data(self, install_root: Path) -> Path:
        """Write the docker-created data dirs a default deploy leaves behind."""
        suricata = install_root / "DeepPrint" / "data" / "suricata" / "log"
        suricata.mkdir(parents=True)
        (suricata / "eve.json").write_text('{"events": []}\n')
        honeytrap = install_root / "DeepPrint" / "data" / "honeytrap" / "attacks"
        honeytrap.mkdir(parents=True)
        (honeytrap / "attacks.log").write_text("captured\n")
        return suricata

    def _undeletable_rmtree_patch(self, marker: str):
        """Simulate a docker-created root-owned dir rmtree cannot remove.

        The default deploy runs the Compose project from the package
        directory, so `./data/...` bind-mount sources land inside the
        install root-owned; a non-root updater can neither scandir them nor
        chmod them, and shutil.rmtree aborts with
        `[Errno 1] Operation not permitted`.
        """
        real_rmtree = shutil.rmtree

        def undeletable_rmtree(path, *args, **kwargs):
            if Path(path).exists() and any(
                marker in str(child) for child in Path(path).rglob("*")
            ):
                raise OSError(1, "Operation not permitted", str(path))
            return real_rmtree(path, *args, **kwargs)

        return patch.object(bootstrap.shutil, "rmtree", side_effect=undeletable_rmtree)

    def test_update_carries_deploy_data_across_swap(self) -> None:
        """T-Pot data dirs under DeepPrint/data/ survive an update."""
        with tempfile.TemporaryDirectory(prefix="deepprint-upd-") as temp_dir:
            base = Path(temp_dir)
            repo = self._fixture_repo(base)
            newer = self._fixture_repo(base, name="newer")
            newer.joinpath("DeepPrint", "deepprint.py").write_text(
                newer.joinpath("DeepPrint", "deepprint.py").read_text()
                + "\n# upstream rev 2\n"
            )
            install_root = base / ".deepprint"
            with patch.object(bootstrap, "INSTALL_ROOT", install_root):
                bootstrap.install_repo(repo)
                self._seed_runtime_state(install_root)
                self._seed_deploy_data(install_root)
                with patch.dict(
                    os.environ, {"DEEPPRINT_BOOTSTRAP_SOURCE": str(newer)}
                ):
                    result, changed = bootstrap.update_install()
            self.assertTrue(changed)
            self.assertIn(
                "# upstream rev 2",
                (result / "DeepPrint" / "deepprint.py").read_text(),
            )
            self.assertEqual(
                '{"events": []}\n',
                (
                    result
                    / "DeepPrint"
                    / "data"
                    / "suricata"
                    / "log"
                    / "eve.json"
                ).read_text(),
            )
            self.assertTrue(
                (
                    result / "DeepPrint" / "data" / "honeytrap" / "attacks"
                ).is_dir()
            )
            self._assert_runtime_state_preserved(result)
            # The swap left no leftover trees behind.
            self.assertEqual([], list(base.glob(".deepprint-retired-*")))

    def test_deploy_data_does_not_force_reinstall(self) -> None:
        """Docker data inside the install must not look like a source change."""
        with tempfile.TemporaryDirectory(prefix="deepprint-upd-") as temp_dir:
            base = Path(temp_dir)
            repo = self._fixture_repo(base)
            install_root = base / ".deepprint"
            with patch.object(bootstrap, "INSTALL_ROOT", install_root):
                bootstrap.install_repo(repo)
                self._seed_deploy_data(install_root)
                with patch.dict(
                    os.environ, {"DEEPPRINT_BOOTSTRAP_SOURCE": str(repo)}
                ):
                    _, changed = bootstrap.update_install()
            self.assertFalse(changed)
            self.assertTrue(
                (
                    install_root
                    / "DeepPrint"
                    / "data"
                    / "suricata"
                    / "log"
                    / "eve.json"
                ).exists()
            )

    def test_update_survives_undeletable_suricata_data(self) -> None:
        """The reported crash: rmtree of data/suricata/log hits EPERM.

        A docker-created root-owned bind-mount source under
        DeepPrint/data/suricata/log used to abort the whole update with
        `[Errno 1] Operation not permitted`. The data dir is never
        deleted, overwritten, or moved by the swap.
        """
        with tempfile.TemporaryDirectory(prefix="deepprint-upd-") as temp_dir:
            base = Path(temp_dir)
            repo = self._fixture_repo(base)
            newer = self._fixture_repo(base, name="newer")
            newer.joinpath("DeepPrint", "deepprint.py").write_text(
                newer.joinpath("DeepPrint", "deepprint.py").read_text()
                + "\n# upstream rev 2\n"
            )
            install_root = base / ".deepprint"
            with patch.object(bootstrap, "INSTALL_ROOT", install_root):
                bootstrap.install_repo(repo)
                self._seed_runtime_state(install_root)
                self._seed_deploy_data(install_root)
                with patch.dict(
                    os.environ, {"DEEPPRINT_BOOTSTRAP_SOURCE": str(newer)}
                ), self._undeletable_rmtree_patch("suricata"):
                    result, changed = bootstrap.update_install()
            self.assertTrue(changed)
            self.assertIn(
                "# upstream rev 2",
                (result / "DeepPrint" / "deepprint.py").read_text(),
            )
            self.assertEqual(
                '{"events": []}\n',
                (
                    result
                    / "DeepPrint"
                    / "data"
                    / "suricata"
                    / "log"
                    / "eve.json"
                ).read_text(),
            )
            self._assert_runtime_state_preserved(result)
            self.assertEqual([], list(base.glob(".deepprint-retired-*")))

    def test_update_leaves_unmovable_deploy_data_in_place(self) -> None:
        """Docker data is unmovable, not just undeletable: EACCES on rename.

        Moving a directory to a different parent needs write permission on
        the directory itself (the kernel updates `..`), so a root-owned
        mode-0700 data dir raises EACCES on os.rename. A design that
        carries the data dir across the swap can never work; the update
        must leave it exactly where it is and swap everything else around
        it. Guard: any rename touching deploy data fails this test.
        """
        with tempfile.TemporaryDirectory(prefix="deepprint-upd-") as temp_dir:
            base = Path(temp_dir)
            repo = self._fixture_repo(base)
            newer = self._fixture_repo(base, name="newer")
            newer.joinpath("DeepPrint", "deepprint.py").write_text(
                newer.joinpath("DeepPrint", "deepprint.py").read_text()
                + "\n# upstream rev 2\n"
            )
            install_root = base / ".deepprint"
            real_rename = os.rename

            def deny_data_renames(source, target):
                if Path(source).name in bootstrap.DEPLOY_DATA_DIRS:
                    raise PermissionError(
                        errno.EACCES, "Permission denied (unmovable dir)"
                    )
                return real_rename(source, target)

            with patch.object(bootstrap, "INSTALL_ROOT", install_root):
                bootstrap.install_repo(repo)
                self._seed_runtime_state(install_root)
                self._seed_deploy_data(install_root)
                with patch.dict(
                    os.environ, {"DEEPPRINT_BOOTSTRAP_SOURCE": str(newer)}
                ), patch.object(bootstrap.os, "rename", deny_data_renames):
                    result, changed = bootstrap.update_install()
            self.assertTrue(changed)
            self.assertIn(
                "# upstream rev 2",
                (result / "DeepPrint" / "deepprint.py").read_text(),
            )
            self.assertEqual(
                '{"events": []}\n',
                (
                    result
                    / "DeepPrint"
                    / "data"
                    / "suricata"
                    / "log"
                    / "eve.json"
                ).read_text(),
            )
            self._assert_runtime_state_preserved(result)
            self.assertEqual([], list(base.glob(".deepprint-retired-*")))

    def test_update_survives_undeletable_stale_entry(self) -> None:
        """An undeletable stale entry warns and stays put; the update lands."""
        with tempfile.TemporaryDirectory(prefix="deepprint-upd-") as temp_dir:
            base = Path(temp_dir)
            repo = self._fixture_repo(base)
            newer = self._fixture_repo(base, name="newer")
            newer.joinpath("DeepPrint", "deepprint.py").write_text(
                newer.joinpath("DeepPrint", "deepprint.py").read_text()
                + "\n# upstream rev 2\n"
            )
            install_root = base / ".deepprint"
            with patch.object(bootstrap, "INSTALL_ROOT", install_root):
                bootstrap.install_repo(repo)
                self._seed_runtime_state(install_root)
                evil = install_root / "DeepPrint" / "evil_root_dir"
                evil.mkdir()
                (evil / "stuck.log").write_text("stuck\n")
                stderr = io.StringIO()
                with patch.dict(
                    os.environ, {"DEEPPRINT_BOOTSTRAP_SOURCE": str(newer)}
                ), self._undeletable_rmtree_patch("evil_root_dir"), (
                    contextlib.redirect_stderr(stderr)
                ):
                    result, changed = bootstrap.update_install()
            self.assertTrue(changed)
            self.assertIn(
                "# upstream rev 2",
                (result / "DeepPrint" / "deepprint.py").read_text(),
            )
            # The new install landed; the stuck entry is warned about and
            # left in place rather than aborting the swap.
            self.assertIn("could not remove", stderr.getvalue())
            self.assertIn("evil_root_dir", stderr.getvalue())
            self.assertTrue(evil.exists())
            self._assert_runtime_state_preserved(result)


if __name__ == "__main__":
    unittest.main()
