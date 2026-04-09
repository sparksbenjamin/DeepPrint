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
    DeepPrintError,
    build_runtime_paths,
    generate_mac_address_from_prefix,
    normalize_mac_address,
    normalize_mac_address_prefix,
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

    def test_render_with_tpot_root_uses_bundled_templates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deepprint-live-template-") as temp_dir:
            tpot_root = Path(temp_dir)
            (tpot_root / "docker-compose.yml").write_text(
                "version: '3.8'\nservices:\n  live_only:\n    image: busybox\n",
                encoding="utf-8",
            )
            (tpot_root / ".env").write_text(
                "COMPOSE_PROJECT_NAME=live-tpot\n",
                encoding="utf-8",
            )

            engine = DeepPrintEngine(build_runtime_paths(make_args(tpot_root=tpot_root)))

            with patch("DeepPrint.deepprint.can_prompt_interactively", return_value=False):
                deployment = engine.render("utility_substation")

            self.assertIn("cowrie", deployment.compose_data["services"])
            self.assertEqual(
                "live-tpot",
                engine._get_project_name(tpot_root / ".env"),
            )

    def test_load_env_file_accepts_colon_and_equals_syntax(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deepprint-env-") as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "A=one\nB: two\n# comment\nC = three\nD: four five\n",
                encoding="utf-8",
            )

            loaded = DeepPrintEngine._load_env_file(env_path)

            self.assertEqual(
                {
                    "A": "one",
                    "B": "two",
                    "C": "three",
                    "D": "four five",
                },
                loaded,
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

            with patch("DeepPrint.deepprint.time.sleep"), patch(
                "DeepPrint.deepprint.random.uniform", return_value=1.5
            ):
                engine.deploy(deployment)

            self.assertTrue((tpot_root / "docker-compose.yml.deepprint.bak").exists())
            self.assertTrue((tpot_root / ".env.deepprint.bak").exists())
            self.assertTrue((tpot_root / "docker-compose.deepprint.yml").exists())
            self.assertTrue((tpot_root / ".env.deepprint").exists())
            self.assertIn(
                "DEEPPRINT_PERSONA=power_plant",
                (tpot_root / ".env").read_text(encoding="utf-8"),
            )
            self.assertEqual(4, len(compose_calls))
            self.assertEqual("down", compose_calls[0][-1])
            self.assertEqual(["up", "-d", "cowrie"], compose_calls[1][-3:])
            self.assertEqual(["up", "-d", "conpot"], compose_calls[2][-3:])
            self.assertEqual(["up", "-d", "suricata"], compose_calls[3][-3:])
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

            with patch("DeepPrint.deepprint.time.sleep"), patch(
                "DeepPrint.deepprint.random.uniform", return_value=1.5
            ):
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
            self.assertEqual(["up", "-d", "cowrie"], compose_calls[1][-3:])
            self.assertEqual(
                "deepprint-live",
                compose_calls[0][compose_calls[0].index("-p") + 1],
            )
            self.assertEqual("tpot", compose_calls[1][compose_calls[1].index("-p") + 1])

    def test_redeploy_preserves_original_backup_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deepprint-redeploy-") as temp_dir:
            tpot_root = Path(temp_dir)
            original_compose = tpot_root / "docker-compose.yml"
            original_env = tpot_root / ".env"

            shutil.copy2(
                PROJECT_ROOT / "DeepPrint" / "templates" / "tpot.yml",
                original_compose,
            )
            shutil.copy2(
                PROJECT_ROOT / "DeepPrint" / "templates" / ".env",
                original_env,
            )

            engine = DeepPrintEngine(build_runtime_paths(make_args(tpot_root=tpot_root)))
            with patch("DeepPrint.deepprint.can_prompt_interactively", return_value=False):
                first_deployment = engine.render("utility_substation")
                second_deployment = engine.render("airport_ops")

            engine._run_docker = lambda compose_args: None
            engine._run_command = lambda command, error_context: None

            with patch("DeepPrint.deepprint.time.sleep"), patch(
                "DeepPrint.deepprint.random.uniform", return_value=1.5
            ):
                engine.deploy(first_deployment)

            backup_compose = tpot_root / "docker-compose.yml.deepprint.bak"
            backup_env = tpot_root / ".env.deepprint.bak"
            first_backup_compose = backup_compose.read_text(encoding="utf-8")
            first_backup_env = backup_env.read_text(encoding="utf-8")

            with patch("DeepPrint.deepprint.time.sleep"), patch(
                "DeepPrint.deepprint.random.uniform", return_value=1.5
            ):
                engine.deploy(second_deployment)

            self.assertEqual(first_backup_compose, backup_compose.read_text(encoding="utf-8"))
            self.assertEqual(first_backup_env, backup_env.read_text(encoding="utf-8"))

    def test_generate_mac_address_from_prefix_randomizes_suffix(self) -> None:
        with patch(
            "DeepPrint.deepprint.random.randint",
            side_effect=[0xAA, 0xBB, 0xCC],
        ):
            mac_address = generate_mac_address_from_prefix("00-11-22")

        self.assertEqual("00:11:22:aa:bb:cc", mac_address)
        self.assertEqual("00:11:22:33:44:55", normalize_mac_address("00-11-22-33-44-55"))
        self.assertEqual("00:11:22", normalize_mac_address_prefix("00-11-22"))

    def test_host_network_service_rejects_mac_override(self) -> None:
        engine = DeepPrintEngine(build_runtime_paths(make_args()))
        service_definition = {
            "network_mode": "host",
            "environment": {},
        }
        service_override = {
            "hostname": "soc-sensor",
            "container_name": "sensor-host",
            "environment_variables": {},
            "mac_address_prefix": "00:11:22",
        }

        with self.assertRaisesRegex(DeepPrintError, "network_mode: host"):
            engine._apply_service_persona(
                service_name="suricata",
                service_definition=service_definition,
                global_prefix="plant",
                service_override=service_override,
                generated_env={},
            )

    def test_bridge_service_applies_generated_mac_address(self) -> None:
        engine = DeepPrintEngine(build_runtime_paths(make_args()))
        service_definition = {
            "environment": {},
            "networks": ["tpot"],
        }
        service_override = {
            "hostname": "plc-gateway",
            "container_name": "plc-1",
            "environment_variables": {},
            "mac_address_prefix": "00:11:22",
        }
        generated_env: dict[str, str] = {}

        with patch(
            "DeepPrint.deepprint.random.randint",
            side_effect=[0xAA, 0xBB, 0xCC],
        ):
            engine._apply_service_persona(
                service_name="conpot",
                service_definition=service_definition,
                global_prefix="plant",
                service_override=service_override,
                generated_env=generated_env,
            )

        self.assertEqual("00:11:22:aa:bb:cc", service_definition["mac_address"])
        self.assertEqual(
            "00:11:22:aa:bb:cc",
            generated_env["DEEPPRINT_CONPOT_MAC_ADDRESS"],
        )

    def test_select_persona_uses_console_input_when_stdin_is_not_tty(self) -> None:
        with patch("sys.stdin.isatty", return_value=False), patch(
            "DeepPrint.deepprint.open_console_input_stream",
            side_effect=lambda: StringIO("2\n"),
        ):
            persona = select_persona_interactive(["airport_ops", "power_plant"])

        self.assertEqual("power_plant", persona)


if __name__ == "__main__":
    unittest.main()
