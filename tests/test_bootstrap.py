from __future__ import annotations

import os
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


if __name__ == "__main__":
    unittest.main()
