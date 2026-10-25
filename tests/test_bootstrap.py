from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
