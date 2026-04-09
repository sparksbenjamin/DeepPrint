from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from io import StringIO
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from DeepPrint.deepprint import (
    DeepPrintEngine,
    build_runtime_paths,
    select_persona_interactive,
)


def make_args(**overrides: object) -> Namespace:
    values = {
        "deploy": None,
        "list_personas": False,
        "restore": False,
        "interactive": False,
        "dry_run": False,
        "tpot_root": None,
        "base_compose": None,
        "base_env": None,
        "output_compose": None,
        "output_env": None,
    }
    values.update(overrides)
    return Namespace(**values)


class DeepPrintEngineTests(unittest.TestCase):
    def test_all_personas_render_successfully(self) -> None:
        engine = DeepPrintEngine(build_runtime_paths(make_args()))

        personas = engine.list_personas()
        self.assertGreaterEqual(len(personas), 1)

        with patch("DeepPrint.deepprint.can_prompt_interactively", return_value=False):
            for persona in personas:
                deployment = engine.render(persona)
                self.assertIn("services", deployment.compose_data)
                self.assertIn("cowrie", deployment.compose_data["services"])
                self.assertTrue(
                    deployment.compose_data["services"]["cowrie"]["hostname"],
                    msg=f"Persona {persona} did not produce a cowrie hostname.",
                )

    def test_deploy_to_tpot_root_backs_up_active_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deepprint-deploy-") as temp_dir:
            tpot_root = Path(temp_dir)
            shutil.copy2(
                PROJECT_ROOT / "DeepPrint" / "templates" / "tpot.yml",
                tpot_root / "docker-compose.yml",
            )
            shutil.copy2(
                PROJECT_ROOT / "DeepPrint" / "templates" / ".env",
                tpot_root / ".env",
            )

            engine = DeepPrintEngine(build_runtime_paths(make_args(tpot_root=tpot_root)))
            with patch("DeepPrint.deepprint.can_prompt_interactively", return_value=False):
                deployment = engine.render("power_plant")

            compose_calls: list[list[str]] = []
            copy_calls: list[list[str]] = []

            engine._run_docker = lambda compose_args: compose_calls.append(compose_args)
            engine._run_command = (
                lambda command, error_context: copy_calls.append(command)
            )

            engine.deploy(deployment)

            self.assertTrue((tpot_root / "docker-compose.yml.deepprint.bak").exists())
            self.assertTrue((tpot_root / ".env.deepprint.bak").exists())
            self.assertTrue((tpot_root / "docker-compose.deepprint.yml").exists())
            self.assertTrue((tpot_root / ".env.deepprint").exists())
            self.assertIn(
                "DEEPPRINT_PERSONA=power_plant",
                (tpot_root / ".env").read_text(encoding="utf-8"),
            )
            self.assertEqual(2, len(compose_calls))
            self.assertEqual(2, len(copy_calls))

    def test_restore_reinstates_backups_and_restarts_stack(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deepprint-restore-") as temp_dir:
            tpot_root = Path(temp_dir)
            active_compose = tpot_root / "docker-compose.yml"
            active_env = tpot_root / ".env"
            backup_compose = tpot_root / "docker-compose.yml.deepprint.bak"
            backup_env = tpot_root / ".env.deepprint.bak"

            active_compose.write_text(
                "version: '3.8'\nservices:\n  cowrie:\n    image: live\n",
                encoding="utf-8",
            )
            active_env.write_text(
                "COMPOSE_PROJECT_NAME=deepprint-live\n",
                encoding="utf-8",
            )
            backup_compose.write_text(
                "version: '3.8'\nservices:\n  cowrie:\n    image: restored\n",
                encoding="utf-8",
            )
            backup_env.write_text("COMPOSE_PROJECT_NAME=tpot\n", encoding="utf-8")

            engine = DeepPrintEngine(build_runtime_paths(make_args(tpot_root=tpot_root)))

            compose_calls: list[list[str]] = []
            engine._run_docker = lambda compose_args: compose_calls.append(compose_args)

            engine.restore()

            self.assertEqual(
                backup_compose.read_text(encoding="utf-8"),
                active_compose.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                backup_env.read_text(encoding="utf-8"),
                active_env.read_text(encoding="utf-8"),
            )
            self.assertEqual(2, len(compose_calls))
            self.assertEqual("down", compose_calls[0][-1])
            self.assertEqual(["up", "-d"], compose_calls[1][-2:])
            self.assertEqual(
                "deepprint-live",
                compose_calls[0][compose_calls[0].index("-p") + 1],
            )
            self.assertEqual("tpot", compose_calls[1][compose_calls[1].index("-p") + 1])

    def test_select_persona_uses_console_input_when_stdin_is_not_tty(self) -> None:
        with patch("sys.stdin.isatty", return_value=False), patch(
            "DeepPrint.deepprint.open_console_input_stream",
            side_effect=lambda: StringIO("2\n"),
        ):
            persona = select_persona_interactive(["airport_ops", "power_plant"])

        self.assertEqual("power_plant", persona)


if __name__ == "__main__":
    unittest.main()
