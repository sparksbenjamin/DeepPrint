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
        "update": False,
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

    def test_cloud_storage_persona_renders_with_webdecoy(self) -> None:
        engine = DeepPrintEngine(build_runtime_paths(make_args()))

        with patch("DeepPrint.deepprint.can_prompt_interactively", return_value=False):
            deployment = engine.render("cloud_storage")

        services = deployment.compose_data["services"]
        self.assertIn("webdecoy", services)
        self.assertEqual("cloud-estate-web", services["webdecoy"]["container_name"])
        self.assertEqual("python:3.12-alpine", services["webdecoy"]["image"])
        self.assertEqual(
            "Sparks Manor Cloud",
            services["webdecoy"]["environment"]["DECOY_SITE_NAME"],
        )
        injected_services = {injection.service for injection in deployment.injections}
        self.assertIn("webdecoy", injected_services)
        webdecoy_injection = next(
            injection
            for injection in deployment.injections
            if injection.service == "webdecoy"
        )
        self.assertEqual("/srv/decoy/webapp.py", webdecoy_injection.destination)
        self.assertIsNotNone(webdecoy_injection.rendered_text)

    def _stash_repo_pointer(self) -> None:
        """Isolate the repo-root state pointer from real deployments.

        ``write_state`` records the pointer inside the package directory, so
        tests that exercise it must not see (or leak) a pointer belonging to
        an actual deployment on this machine.
        """
        pointer_file = PROJECT_ROOT / "DeepPrint" / ".deepprint_state_pointer.json"
        original = (
            pointer_file.read_text(encoding="utf-8")
            if pointer_file.exists()
            else None
        )

        def restore() -> None:
            if original is None:
                pointer_file.unlink(missing_ok=True)
            else:
                pointer_file.write_text(original, encoding="utf-8")

        self.addCleanup(restore)
        pointer_file.unlink(missing_ok=True)

    def _deploy_cloud_storage_fake_docker(
        self, tpot_root: Path, answers: dict[str, str] | None = None
    ):
        shutil.copy2(
            PROJECT_ROOT / "DeepPrint" / "templates" / "tpot.yml",
            tpot_root / "docker-compose.yml",
        )
        shutil.copy2(
            PROJECT_ROOT / "DeepPrint" / "templates" / ".env",
            tpot_root / ".env",
        )
        engine = DeepPrintEngine(build_runtime_paths(make_args(tpot_root=tpot_root)))
        engine._run_docker = lambda compose_args: None
        engine._run_command = lambda command, error_context: None
        with patch("DeepPrint.deepprint.can_prompt_interactively", return_value=False):
            deployment = engine.render("cloud_storage", answers=answers)
        with patch("DeepPrint.deepprint.time.sleep"), patch(
            "DeepPrint.deepprint.random.uniform", return_value=1.5
        ):
            engine.deploy(deployment)
        engine.write_state(deployment)
        return engine, deployment

    def test_deploy_records_state_and_update_replays_answers(self) -> None:
        self._stash_repo_pointer()
        with tempfile.TemporaryDirectory(prefix="deepprint-state-") as temp_dir:
            tpot_root = Path(temp_dir)
            answers = {
                "site_name": "Replay Cloud",
                "deploy_domain": "replay.example.test",
            }
            engine, deployment = self._deploy_cloud_storage_fake_docker(
                tpot_root, answers=answers
            )

            state_file = tpot_root / ".deepprint_state.json"
            self.assertTrue(state_file.exists())
            self.assertTrue(deployment.prompt_values["site_name"] == "Replay Cloud")

            located_path, state = engine.find_state()
            self.assertEqual(state_file, located_path)
            self.assertEqual("cloud_storage", state["persona"])
            self.assertEqual("Replay Cloud", state["prompt_values"]["site_name"])
            self.assertEqual(
                "replay.example.test", state["prompt_values"]["deploy_domain"]
            )
            self.assertEqual(str(tpot_root), state["flags"]["tpot_root"])
            self.assertIsNone(state["flags"]["base_compose"])

            # A fresh engine (as update.sh would build one) must locate the
            # deployment through the repo-root pointer written by write_state.
            pointer_file = PROJECT_ROOT / "DeepPrint" / ".deepprint_state_pointer.json"
            self.assertTrue(pointer_file.exists())
            repo_engine = DeepPrintEngine(build_runtime_paths(make_args()))
            pointed_path, pointed_state = repo_engine.find_state()
            self.assertEqual(state_file, pointed_path)
            self.assertEqual("Replay Cloud", pointed_state["prompt_values"]["site_name"])

            # Replay must reproduce the deployed answers verbatim, not the
            # persona defaults.
            with patch(
                "DeepPrint.deepprint.can_prompt_interactively", return_value=False
            ):
                replayed = repo_engine.render(
                    "cloud_storage", answers=pointed_state["prompt_values"]
                )
            webdecoy_env = replayed.compose_data["services"]["webdecoy"]["environment"]
            self.assertEqual("Replay Cloud", webdecoy_env["DECOY_SITE_NAME"])
            self.assertEqual(
                "replay.example.test", webdecoy_env["DECOY_DOMAIN"]
            )

    def test_update_replay_never_prompts(self) -> None:
        self._stash_repo_pointer()
        with tempfile.TemporaryDirectory(prefix="deepprint-noprompt-") as temp_dir:
            tpot_root = Path(temp_dir)
            _, deployment = self._deploy_cloud_storage_fake_docker(tpot_root)
            saved = dict(deployment.prompt_values)
            saved["site_name"] = "Silent Replay"

            engine = DeepPrintEngine(build_runtime_paths(make_args()))
            # Even on an interactive TTY, a replay must never prompt.
            with patch(
                "DeepPrint.deepprint.can_prompt_interactively", return_value=True
            ), patch("builtins.input", side_effect=AssertionError("prompted")):
                replayed = engine.render("cloud_storage", answers=saved)
            self.assertEqual(
                "Silent Replay",
                replayed.compose_data["services"]["webdecoy"]["environment"][
                    "DECOY_SITE_NAME"
                ],
            )

    def test_update_without_state_reports_missing_deployment(self) -> None:
        self._stash_repo_pointer()
        with tempfile.TemporaryDirectory(prefix="deepprint-nostate-") as temp_dir:
            # No deployment here and no repo-root pointer: --update must
            # fail with actionable guidance instead of a traceback.
            engine = DeepPrintEngine(
                build_runtime_paths(make_args(tpot_root=Path(temp_dir)))
            )
            with self.assertRaises(DeepPrintError) as raised:
                engine.find_state()
            self.assertIn("No previous DeepPrint deployment", str(raised.exception))

    def test_update_rejects_corrupt_state_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deepprint-badstate-") as temp_dir:
            tpot_root = Path(temp_dir)
            (tpot_root / ".deepprint_state.json").write_text(
                "{not json", encoding="utf-8"
            )
            engine = DeepPrintEngine(
                build_runtime_paths(make_args(tpot_root=tpot_root))
            )
            with self.assertRaises(DeepPrintError) as raised:
                engine.find_state()
            self.assertIn("could not be read", str(raised.exception))

            (tpot_root / ".deepprint_state.json").write_text(
                '{"version": 99, "persona": "cloud_storage"}', encoding="utf-8"
            )
            with self.assertRaises(DeepPrintError) as raised:
                engine.find_state()
            self.assertIn("unknown or missing version", str(raised.exception))

    def test_full_update_flow_reapplies_saved_deployment(self) -> None:
        self._stash_repo_pointer()
        with tempfile.TemporaryDirectory(prefix="deepprint-updateflow-") as temp_dir:
            tpot_root = Path(temp_dir)
            engine, deployment = self._deploy_cloud_storage_fake_docker(tpot_root)
            self.assertTrue((tpot_root / "docker-compose.deepprint.yml").exists())

            # Simulate the engine side of `update.sh`: fresh engine, no
            # --tpot-root, state located via the pointer, deploy faked.
            _, state = DeepPrintEngine(build_runtime_paths(make_args())).find_state()

            replay_args = make_args(
                deploy=str(state["persona"]), tpot_root=Path(state["flags"]["tpot_root"])
            )
            if state["flags"].get("base_compose"):
                replay_args.base_compose = Path(state["flags"]["base_compose"])
            replay_engine = DeepPrintEngine(build_runtime_paths(replay_args))

            up_calls: list[list[str]] = []
            replay_engine._run_docker = lambda compose_args: up_calls.append(compose_args)
            replay_engine._run_command = lambda command, error_context: None

            with patch("DeepPrint.deepprint.can_prompt_interactively", return_value=False):
                replayed = replay_engine.render(
                    str(state["persona"]), answers=state["prompt_values"]
                )
            with patch("DeepPrint.deepprint.time.sleep"), patch(
                "DeepPrint.deepprint.random.uniform", return_value=1.5
            ):
                replay_engine.deploy(replayed)
            replay_engine.write_state(replayed)

            # The webdecoy service is part of the redeployed stack.
            self.assertTrue(
                any("webdecoy" in call for call in up_calls),
                up_calls,
            )
            # State survives the update and still records the persona.
            state_file = Path(state["flags"]["tpot_root"]) / ".deepprint_state.json"
            self.assertTrue(state_file.exists())

    def test_explicit_base_compose_overrides_persona_base_template(self) -> None:
        engine = DeepPrintEngine(
            build_runtime_paths(
                make_args(
                    base_compose=PROJECT_ROOT / "DeepPrint" / "templates" / "tpot.yml",
                )
            )
        )

        with patch("DeepPrint.deepprint.can_prompt_interactively", return_value=False):
            with self.assertRaisesRegex(DeepPrintError, "webdecoy"):
                engine.render("cloud_storage")

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
