from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency validation
    raise SystemExit(
        "PyYAML is required to run DeepPrint. Install it with `pip install pyyaml`."
    ) from exc


class DeepPrintError(RuntimeError):
    """Raised when DeepPrint cannot complete a requested operation."""


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    footprints_dir: Path
    templates_dir: Path
    base_compose: Path
    base_env: Path
    output_compose: Path
    output_env: Path
    rendered_assets_dir: Path
    tpot_root: Path | None = None


@dataclass(frozen=True)
class FileInjection:
    service: str
    source: Path
    destination: str
    rendered_text: str | None = None


@dataclass(frozen=True)
class RenderedDeployment:
    persona_name: str
    compose_data: dict[str, Any]
    compose_text: str
    env_values: dict[str, str]
    injections: list[FileInjection]
    project_name: str


@dataclass(frozen=True)
class NormalizedEnvironment:
    values: dict[str, str]
    passthrough: list[str]
    style: str


@dataclass(frozen=True)
class PromptSpec:
    id: str
    message: str
    default: str | None
    required: bool


PLACEHOLDER_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_]+)\s*}}")


class DeepPrintEngine:
    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self._docker_cmd: list[str] | None = None
        self._compose_env_reference = format_compose_path(
            target=self.paths.output_env,
            compose_file=self.paths.output_compose,
        )

    def render(self, persona_name: str) -> RenderedDeployment:
        compose_data = self._load_yaml(self.paths.base_compose)
        self._validate_compose_template(compose_data)

        persona_dir, persona_data = self._load_persona(persona_name)
        prompt_values = self._resolve_prompt_values(
            persona_name=persona_name,
            prompt_data=persona_data.get("prompts", []),
        )
        rendered_persona = render_templates(persona_data, prompt_values)
        self._validate_persona(persona_name, rendered_persona)
        rendered_compose = deepcopy(compose_data)

        services = rendered_compose["services"]
        global_prefix = str(rendered_persona["global_prefix"]).strip()
        persona_services = rendered_persona["services"]

        generated_env = self._load_env_file(self.paths.base_env)
        if "COMPOSE_PROJECT_NAME" not in generated_env:
            generated_env["COMPOSE_PROJECT_NAME"] = "tpot"

        generated_env["DEEPPRINT_PERSONA"] = persona_name
        generated_env["DEEPPRINT_GLOBAL_PREFIX"] = sanitize_hostname(global_prefix)
        generated_env["DEEPPRINT_OUTPUT_COMPOSE"] = self.paths.output_compose.name

        for service_name in persona_services:
            if service_name not in services:
                raise DeepPrintError(
                    f"Persona references unknown service '{service_name}' not found in "
                    f"{self.paths.base_compose.name}."
                )

        for service_name, service_definition in services.items():
            override = persona_services.get(service_name, {})
            self._apply_service_persona(
                service_name=service_name,
                service_definition=service_definition,
                global_prefix=global_prefix,
                service_override=override,
                generated_env=generated_env,
            )

        template_context = dict(prompt_values)
        template_context["global_prefix"] = global_prefix
        for service_name, service_definition in services.items():
            template_context[f"{service_name}_hostname"] = str(
                service_definition.get("hostname", "")
            )
            template_context[f"{service_name}_container_name"] = str(
                service_definition.get("container_name", "")
            )

        injections = self._build_injection_plan(
            persona_dir=persona_dir,
            persona_name=persona_name,
            files_to_inject=rendered_persona["files_to_inject"],
            compose_services=services,
            template_context=template_context,
        )

        compose_text = yaml.safe_dump(
            rendered_compose,
            sort_keys=False,
            default_flow_style=False,
        )

        return RenderedDeployment(
            persona_name=persona_name,
            compose_data=rendered_compose,
            compose_text=compose_text,
            env_values=generated_env,
            injections=injections,
            project_name=generated_env["COMPOSE_PROJECT_NAME"],
        )

    def write_artifacts(self, deployment: RenderedDeployment) -> None:
        self.paths.output_compose.parent.mkdir(parents=True, exist_ok=True)
        self.paths.output_env.parent.mkdir(parents=True, exist_ok=True)

        self.paths.output_compose.write_text(deployment.compose_text, encoding="utf-8")

        env_lines = [
            "# Generated by DeepPrint. Changes will be overwritten on the next deploy."
        ]
        for key, value in deployment.env_values.items():
            env_lines.append(f"{key}={value}")
        self.paths.output_env.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    def deploy(self, deployment: RenderedDeployment) -> None:
        if self.paths.tpot_root is not None:
            self._deploy_to_tpot_root(deployment)
        else:
            self.write_artifacts(deployment)
            self._run_compose_stack(
                compose_path=self.paths.output_compose,
                env_path=self.paths.output_env,
                project_name=deployment.project_name,
                action="down",
            )
            self._run_compose_stack(
                compose_path=self.paths.output_compose,
                env_path=self.paths.output_env,
                project_name=deployment.project_name,
                action="up",
                extra_args=["-d"],
            )

        service_map = deployment.compose_data["services"]
        for injection in deployment.injections:
            container_name = service_map[injection.service].get("container_name")
            if not container_name:
                raise DeepPrintError(
                    f"Cannot inject '{injection.source.name}' because service "
                    f"'{injection.service}' does not have an explicit container_name."
                )
            source_path = self._materialize_injection_source(
                persona_name=deployment.persona_name,
                injection=injection,
            )
            self._run_command(
                [
                    self._get_docker_binary(),
                    "cp",
                    str(source_path),
                    f"{container_name}:{injection.destination}",
                ],
                error_context=(
                    f"Failed to copy '{source_path}' into "
                    f"'{container_name}:{injection.destination}'."
                ),
            )

    def _deploy_to_tpot_root(self, deployment: RenderedDeployment) -> None:
        self._run_compose_stack(
            compose_path=self.paths.base_compose,
            env_path=self.paths.base_env,
            project_name=deployment.project_name,
            action="down",
        )

        self.write_artifacts(deployment)
        self._backup_active_tpot_files()
        shutil.copy2(self.paths.output_compose, self.paths.base_compose)
        shutil.copy2(self.paths.output_env, self.paths.base_env)

        self._run_compose_stack(
            compose_path=self.paths.base_compose,
            env_path=self.paths.base_env,
            project_name=deployment.project_name,
            action="up",
            extra_args=["-d"],
        )

    def _backup_active_tpot_files(self) -> None:
        if self.paths.base_compose.exists():
            shutil.copy2(
                self.paths.base_compose,
                self.paths.base_compose.with_name(
                    f"{self.paths.base_compose.name}.deepprint.bak"
                ),
            )
        if self.paths.base_env.exists():
            shutil.copy2(
                self.paths.base_env,
                self.paths.base_env.with_name(".env.deepprint.bak"),
            )

    def _run_compose_stack(
        self,
        compose_path: Path,
        env_path: Path,
        project_name: str,
        action: str,
        extra_args: list[str] | None = None,
    ) -> None:
        compose_args = [
            "-p",
            project_name,
            "--env-file",
            str(env_path),
            "-f",
            str(compose_path),
            action,
        ]
        if extra_args:
            compose_args.extend(extra_args)
        self._run_docker(compose_args)

    def _materialize_injection_source(
        self,
        persona_name: str,
        injection: FileInjection,
    ) -> Path:
        if injection.rendered_text is None:
            return injection.source

        output_dir = self.paths.rendered_assets_dir / persona_name / injection.service
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / injection.source.name
        output_path.write_text(injection.rendered_text, encoding="utf-8")
        return output_path

    def _apply_service_persona(
        self,
        service_name: str,
        service_definition: dict[str, Any],
        global_prefix: str,
        service_override: dict[str, Any],
        generated_env: dict[str, str],
    ) -> None:
        existing_container_name = service_definition.get("container_name") or service_name
        override_container_name = service_override.get("container_name", existing_container_name)
        final_container_name = sanitize_container_name(str(override_container_name))
        service_definition["container_name"] = final_container_name

        hostname_seed = (
            service_override.get("hostname")
            or service_definition.get("hostname")
            or final_container_name
            or service_name
        )
        service_definition["hostname"] = build_hostname(global_prefix, str(hostname_seed))

        existing_env = service_definition.get("environment")
        env_overrides = service_override.get("environment_variables", {})
        service_definition["environment"] = merge_environment(existing_env, env_overrides)
        service_definition["env_file"] = merge_env_file(
            service_definition.get("env_file"),
            self._compose_env_reference,
        )

        env_prefix = f"DEEPPRINT_{service_name.upper()}"
        generated_env[f"{env_prefix}_HOSTNAME"] = service_definition["hostname"]
        generated_env[f"{env_prefix}_CONTAINER_NAME"] = final_container_name
        for key, value in env_overrides.items():
            generated_env[f"{env_prefix}_{str(key).upper()}"] = str(value)

    def _build_injection_plan(
        self,
        persona_dir: Path,
        persona_name: str,
        files_to_inject: list[dict[str, Any]],
        compose_services: dict[str, Any],
        template_context: dict[str, str],
    ) -> list[FileInjection]:
        plan: list[FileInjection] = []
        for index, item in enumerate(files_to_inject, start=1):
            if not isinstance(item, dict):
                raise DeepPrintError(
                    f"files_to_inject entry #{index} in persona '{persona_name}' must be a mapping."
                )

            for key in ("service", "source"):
                if key not in item:
                    raise DeepPrintError(
                        f"files_to_inject entry #{index} in persona '{persona_name}' is missing "
                        f"required key '{key}'."
                    )

            destination = item.get("destination", item.get("container_path"))
            if not destination:
                raise DeepPrintError(
                    f"files_to_inject entry #{index} in persona '{persona_name}' must include "
                    f"'destination' or 'container_path'."
                )

            service_name = str(item["service"])
            if service_name not in compose_services:
                raise DeepPrintError(
                    f"files_to_inject entry #{index} references unknown service '{service_name}'."
                )

            source = (persona_dir / str(item["source"])).resolve()
            if not source.exists():
                raise DeepPrintError(
                    f"Injection source file does not exist: {source}"
                )

            rendered_text = render_text_file_if_possible(
                path=source,
                context=template_context,
            )
            plan.append(
                FileInjection(
                    service=service_name,
                    source=source,
                    destination=str(destination),
                    rendered_text=rendered_text,
                )
            )
        return plan

    def _load_persona(self, persona_name: str) -> tuple[Path, dict[str, Any]]:
        persona_dir = self.paths.footprints_dir / persona_name
        persona_file = persona_dir / "persona.yaml"

        if not persona_file.exists():
            raise DeepPrintError(
                f"Persona '{persona_name}' not found at {persona_file}."
            )

        persona_data = self._load_yaml(persona_file)
        self._validate_persona(persona_name, persona_data)
        return persona_dir, persona_data

    def _resolve_prompt_values(
        self,
        persona_name: str,
        prompt_data: Any,
    ) -> dict[str, str]:
        prompt_specs = self._validate_prompt_specs(persona_name, prompt_data)
        values: dict[str, str] = {"persona_name": persona_name}

        for prompt_spec in prompt_specs:
            current_context = dict(values)
            rendered_message = render_template_string(
                prompt_spec.message,
                current_context,
            )
            rendered_default = (
                render_template_string(prompt_spec.default, current_context)
                if prompt_spec.default is not None
                else None
            )

            answer = rendered_default or ""
            if sys.stdin.isatty():
                answer = self._prompt_user(
                    message=rendered_message,
                    default=rendered_default,
                )
            elif prompt_spec.required and not answer:
                raise DeepPrintError(
                    f"Prompt '{prompt_spec.id}' in persona '{persona_name}' requires "
                    "interactive input because it has no default value."
                )

            if prompt_spec.required and not answer.strip():
                raise DeepPrintError(
                    f"Prompt '{prompt_spec.id}' in persona '{persona_name}' cannot be empty."
                )

            values[prompt_spec.id] = answer

        return values

    @staticmethod
    def _prompt_user(message: str, default: str | None) -> str:
        prompt = message
        if default not in (None, ""):
            prompt += f" [{default}]"
        prompt += ": "
        response = input(prompt).strip()
        if response:
            return response
        return default or ""

    def list_personas(self) -> list[str]:
        if not self.paths.footprints_dir.exists():
            return []

        personas: list[str] = []
        for entry in sorted(self.paths.footprints_dir.iterdir()):
            if entry.is_dir() and (entry / "persona.yaml").exists():
                personas.append(entry.name)
        return personas

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise DeepPrintError(f"YAML file not found: {path}")

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise DeepPrintError(f"Failed to parse YAML file {path}: {exc}") from exc

        if raw is None:
            raise DeepPrintError(f"YAML file is empty: {path}")
        if not isinstance(raw, dict):
            raise DeepPrintError(f"Expected a top-level mapping in {path}.")
        return raw

    @staticmethod
    def _validate_compose_template(compose_data: dict[str, Any]) -> None:
        services = compose_data.get("services")
        if not isinstance(services, dict) or not services:
            raise DeepPrintError(
                "The base tpot.yml template must contain a non-empty 'services' mapping."
            )

    @staticmethod
    def _validate_persona(persona_name: str, persona_data: dict[str, Any]) -> None:
        required_top_level = ("global_prefix", "services", "files_to_inject")
        for key in required_top_level:
            if key not in persona_data:
                raise DeepPrintError(
                    f"Persona '{persona_name}' is missing required key '{key}'."
                )

        global_prefix = persona_data["global_prefix"]
        if not isinstance(global_prefix, str) or not global_prefix.strip():
            raise DeepPrintError(
                f"Persona '{persona_name}' must define a non-empty string global_prefix."
            )

        services = persona_data["services"]
        if not isinstance(services, dict) or not services:
            raise DeepPrintError(
                f"Persona '{persona_name}' must define a non-empty services mapping."
            )

        for service_name, service_config in services.items():
            if not isinstance(service_config, dict):
                raise DeepPrintError(
                    f"Service '{service_name}' in persona '{persona_name}' must be a mapping."
                )

            for key in ("hostname", "container_name", "environment_variables"):
                if key not in service_config:
                    raise DeepPrintError(
                        f"Service '{service_name}' in persona '{persona_name}' is missing "
                        f"required key '{key}'."
                    )

            if not isinstance(service_config["hostname"], str) or not service_config["hostname"].strip():
                raise DeepPrintError(
                    f"Service '{service_name}' in persona '{persona_name}' must define a "
                    "non-empty hostname."
                )
            if not isinstance(service_config["container_name"], str) or not service_config["container_name"].strip():
                raise DeepPrintError(
                    f"Service '{service_name}' in persona '{persona_name}' must define a "
                    "non-empty container_name."
                )
            if not isinstance(service_config["environment_variables"], dict):
                raise DeepPrintError(
                    f"Service '{service_name}' in persona '{persona_name}' must define "
                    "environment_variables as a mapping."
                )

        if not isinstance(persona_data["files_to_inject"], list):
            raise DeepPrintError(
                f"Persona '{persona_name}' must define files_to_inject as a list."
            )

    @staticmethod
    def _validate_prompt_specs(persona_name: str, prompt_data: Any) -> list[PromptSpec]:
        if prompt_data in (None, []):
            return []

        if not isinstance(prompt_data, list):
            raise DeepPrintError(
                f"Persona '{persona_name}' must define prompts as a list when provided."
            )

        prompt_specs: list[PromptSpec] = []
        seen_ids: set[str] = set()
        for index, prompt_item in enumerate(prompt_data, start=1):
            if not isinstance(prompt_item, dict):
                raise DeepPrintError(
                    f"Prompt entry #{index} in persona '{persona_name}' must be a mapping."
                )

            for key in ("id", "message"):
                if key not in prompt_item:
                    raise DeepPrintError(
                        f"Prompt entry #{index} in persona '{persona_name}' is missing "
                        f"required key '{key}'."
                    )

            prompt_id = str(prompt_item["id"]).strip()
            if not prompt_id:
                raise DeepPrintError(
                    f"Prompt entry #{index} in persona '{persona_name}' has an empty id."
                )
            if prompt_id in seen_ids:
                raise DeepPrintError(
                    f"Persona '{persona_name}' defines duplicate prompt id '{prompt_id}'."
                )
            seen_ids.add(prompt_id)

            message = str(prompt_item["message"]).strip()
            if not message:
                raise DeepPrintError(
                    f"Prompt '{prompt_id}' in persona '{persona_name}' must have a "
                    "non-empty message."
                )

            default_value = prompt_item.get("default")
            prompt_specs.append(
                PromptSpec(
                    id=prompt_id,
                    message=message,
                    default=None if default_value is None else str(default_value),
                    required=bool(prompt_item.get("required", False)),
                )
            )

        return prompt_specs

    @staticmethod
    def _load_env_file(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}

        env_values: dict[str, str] = {}
        for line_number, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise DeepPrintError(
                    f"Invalid env entry in {path} at line {line_number}: {raw_line}"
                )
            key, value = line.split("=", 1)
            env_values[key.strip()] = value.strip()
        return env_values

    def _run_docker(self, compose_args: list[str]) -> None:
        self._run_command(
            self._get_docker_compose_cmd() + compose_args,
            error_context="Docker Compose command failed.",
        )

    @staticmethod
    def _run_command(command: list[str], error_context: str) -> None:
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise DeepPrintError(
                f"Required executable was not found while running: {' '.join(command)}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() or exc.stdout.strip() or "No output captured."
            raise DeepPrintError(f"{error_context}\nCommand: {' '.join(command)}\n{stderr}") from exc

        if completed.stdout.strip():
            print(completed.stdout.strip())

    def _get_docker_compose_cmd(self) -> list[str]:
        if self._docker_cmd is not None:
            return self._docker_cmd

        docker_path = shutil.which("docker")
        if docker_path:
            try:
                subprocess.run(
                    [docker_path, "compose", "version"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self._docker_cmd = [docker_path, "compose"]
                return self._docker_cmd
            except subprocess.CalledProcessError:
                pass

        docker_compose_path = shutil.which("docker-compose")
        if docker_compose_path:
            self._docker_cmd = [docker_compose_path]
            return self._docker_cmd

        raise DeepPrintError(
            "Docker Compose is not available. Install Docker with `docker compose` "
            "or provide `docker-compose` on PATH."
        )

    def _get_docker_binary(self) -> str:
        docker_path = shutil.which("docker")
        if docker_path:
            return docker_path
        raise DeepPrintError(
            "The Docker CLI is required for file injection with `docker cp`, but "
            "`docker` was not found on PATH."
        )


def normalize_environment(environment: Any) -> NormalizedEnvironment:
    if environment is None:
        return NormalizedEnvironment(values={}, passthrough=[], style="dict")

    if isinstance(environment, dict):
        return NormalizedEnvironment(
            values={str(key): str(value) for key, value in environment.items()},
            passthrough=[],
            style="dict",
        )

    if isinstance(environment, list):
        values: dict[str, str] = {}
        passthrough: list[str] = []
        for entry in environment:
            if not isinstance(entry, str):
                raise DeepPrintError(
                    "Environment lists in the base template must contain string entries."
                )
            if "=" in entry:
                key, value = entry.split("=", 1)
                values[key] = value
            else:
                passthrough.append(entry)
        return NormalizedEnvironment(values=values, passthrough=passthrough, style="list")

    raise DeepPrintError(
        "Service environment blocks must be either a mapping or a list."
    )


def merge_environment(existing: Any, overrides: dict[str, Any]) -> Any:
    normalized = normalize_environment(existing)
    values = dict(normalized.values)
    passthrough = [item for item in normalized.passthrough]

    for key, value in overrides.items():
        key_text = str(key)
        values[key_text] = str(value)
        passthrough = [item for item in passthrough if item != key_text]

    if normalized.style == "list":
        rendered = [f"{key}={value}" for key, value in values.items()]
        rendered.extend(passthrough)
        return rendered

    return values


def merge_env_file(existing: Any, generated_env_name: str) -> list[str]:
    if existing is None:
        return [generated_env_name]

    if isinstance(existing, str):
        entries = [existing]
    elif isinstance(existing, list) and all(isinstance(item, str) for item in existing):
        entries = list(existing)
    else:
        raise DeepPrintError("env_file must be a string or a list of strings.")

    if generated_env_name not in entries:
        entries.insert(0, generated_env_name)
    return entries


def render_templates(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: render_templates(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_templates(item, context) for item in value]
    if isinstance(value, str):
        return render_template_string(value, context)
    return value


def render_template_string(template: str | None, context: dict[str, str]) -> str:
    if template is None:
        return ""

    rendered = str(template)
    for _ in range(10):
        if not PLACEHOLDER_PATTERN.search(rendered):
            return rendered

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in context:
                raise DeepPrintError(
                    f"Template placeholder '{key}' has no matching prompt or context value."
                )
            return str(context[key])

        updated = PLACEHOLDER_PATTERN.sub(replace, rendered)
        if updated == rendered:
            return updated
        rendered = updated

    raise DeepPrintError(
        f"Template rendering exceeded the maximum expansion depth for value '{template}'."
    )


def render_text_file_if_possible(path: Path, context: dict[str, str]) -> str | None:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None

    return render_template_string(content, context)


def build_hostname(global_prefix: str, hostname: str) -> str:
    prefix = sanitize_hostname(global_prefix)
    host = sanitize_hostname(hostname)
    combined = f"{prefix}-{host}" if host else prefix
    return sanitize_hostname(combined)


def sanitize_hostname(raw_hostname: str) -> str:
    candidate = raw_hostname.strip().lower()
    candidate = candidate.replace("_", "-").replace(" ", "-")
    candidate = re.sub(r"[^a-z0-9.-]", "-", candidate)
    candidate = re.sub(r"\.{2,}", ".", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate)

    labels: list[str] = []
    for label in candidate.split("."):
        cleaned = re.sub(r"^-+|-+$", "", label)
        if not cleaned:
            cleaned = "node"
        labels.append(cleaned[:63])

    hostname = ".".join(labels).strip(".-")
    hostname = re.sub(r"\.{2,}", ".", hostname)

    if not hostname:
        hostname = "node"

    if len(hostname) > 253:
        hostname = hostname[:253].rstrip(".-")

    if not hostname:
        hostname = "node"

    return hostname


def sanitize_container_name(raw_name: str) -> str:
    candidate = raw_name.strip().lower().replace(" ", "-")
    candidate = re.sub(r"[^a-z0-9_.-]", "-", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate)
    candidate = candidate.strip("-.")
    return candidate or "deepprint-service"


def format_compose_path(target: Path, compose_file: Path) -> str:
    compose_dir = compose_file.resolve().parent
    resolved_target = target.resolve()
    try:
        relative_path = os.path.relpath(resolved_target, compose_dir)
    except ValueError:
        return resolved_target.as_posix()
    return Path(relative_path).as_posix()


def humanize_persona_name(persona_name: str) -> str:
    return persona_name.replace("_", " ").replace("-", " ").title()


def prompt_yes_no(message: str, default: bool) -> bool:
    default_hint = "Y/n" if default else "y/N"
    while True:
        response = input(f"{message} [{default_hint}]: ").strip().lower()
        if not response:
            return default
        if response in {"y", "yes"}:
            return True
        if response in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def looks_like_tpot_root(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "docker-compose.yml").exists()
        and (path / ".env").exists()
    )


def detect_tpot_root_candidates() -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    env_candidate = os.environ.get("TPOT_ROOT")
    raw_candidates = [
        Path.cwd(),
        Path.home() / "tpotce",
    ]
    if env_candidate:
        raw_candidates.insert(0, Path(env_candidate).expanduser())

    for candidate in raw_candidates:
        resolved = candidate.resolve()
        if resolved not in seen and looks_like_tpot_root(resolved):
            seen.add(resolved)
            candidates.append(resolved)
    return candidates


def select_persona_interactive(personas: list[str]) -> str:
    print("Available DeepPrint personas:")
    for index, persona in enumerate(personas, start=1):
        print(f"  {index}. {humanize_persona_name(persona)} ({persona})")

    while True:
        response = input("Choose a persona by number or name: ").strip()
        if not response:
            print("Please choose a persona.")
            continue

        if response.isdigit():
            choice = int(response)
            if 1 <= choice <= len(personas):
                return personas[choice - 1]

        normalized = response.lower().replace(" ", "_")
        for persona in personas:
            if response == persona or normalized == persona:
                return persona

        print("That selection did not match a known persona.")


def choose_tpot_root_interactive() -> Path | None:
    candidates = detect_tpot_root_candidates()
    if candidates:
        default_candidate = candidates[0]
        print(f"Detected T-Pot installation: {default_candidate}")
        while True:
            response = input(
                "Press Enter to use it, type a different path, or type 'demo' "
                "to use bundled templates only: "
            ).strip()
            if not response:
                return default_candidate
            if response.lower() == "demo":
                return None
            candidate = Path(response).expanduser().resolve()
            if looks_like_tpot_root(candidate):
                return candidate
            print(
                "That path does not look like a T-Pot installation. Expected "
                "docker-compose.yml and .env in the target directory."
            )

    while True:
        response = input(
            "Enter the path to your T-Pot installation, or type 'demo' to use "
            "bundled templates only: "
        ).strip()
        if not response or response.lower() == "demo":
            return None
        candidate = Path(response).expanduser().resolve()
        if looks_like_tpot_root(candidate):
            return candidate
        print(
            "That path does not look like a T-Pot installation. Expected "
            "docker-compose.yml and .env in the target directory."
        )


def configure_interactive_args(args: argparse.Namespace) -> argparse.Namespace:
    updated_args = argparse.Namespace(**vars(args))
    engine = DeepPrintEngine(build_runtime_paths(updated_args))
    personas = engine.list_personas()
    if not personas:
        raise DeepPrintError("No persona footprints are available.")

    if not updated_args.deploy:
        updated_args.deploy = select_persona_interactive(personas)

    if updated_args.tpot_root is None:
        updated_args.tpot_root = choose_tpot_root_interactive()

    if not updated_args.dry_run:
        updated_args.dry_run = prompt_yes_no(
            "Run in dry-run mode only",
            default=False,
        )

    return updated_args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply DeepPrint personas to a T-Pot deployment."
    )
    parser.add_argument(
        "--deploy",
        metavar="PERSONA",
        help="The persona footprint to render or deploy.",
    )
    parser.add_argument(
        "--list-personas",
        action="store_true",
        help="List deployable persona footprints and exit.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Launch an interactive deployment wizard.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated tpot_deepprint.yml and exit without deploying.",
    )
    parser.add_argument(
        "--tpot-root",
        type=Path,
        help="Path to a live T-Pot installation such as ~/tpotce.",
    )
    parser.add_argument(
        "--base-compose",
        type=Path,
        help="Override the base tpot.yml template path.",
    )
    parser.add_argument(
        "--base-env",
        type=Path,
        help="Override the base .env template path.",
    )
    parser.add_argument(
        "--output-compose",
        type=Path,
        help="Override the output Compose manifest path.",
    )
    parser.add_argument(
        "--output-env",
        type=Path,
        help="Override the generated .env path.",
    )
    return parser


def build_runtime_paths(args: argparse.Namespace) -> RuntimePaths:
    root = Path(__file__).resolve().parent
    templates_dir = root / "templates"
    tpot_root = args.tpot_root.expanduser().resolve() if args.tpot_root else None
    default_base_compose = (
        tpot_root / "docker-compose.yml" if tpot_root else templates_dir / "tpot.yml"
    )
    default_base_env = tpot_root / ".env" if tpot_root else templates_dir / ".env"
    default_output_compose = (
        tpot_root / "docker-compose.deepprint.yml"
        if tpot_root
        else root / "tpot_deepprint.yml"
    )
    default_output_env = (
        tpot_root / ".env.deepprint" if tpot_root else root / ".env.deepprint"
    )
    return RuntimePaths(
        root=root,
        footprints_dir=root / "footprints",
        templates_dir=templates_dir,
        base_compose=(
            args.base_compose.expanduser().resolve()
            if args.base_compose
            else default_base_compose
        ),
        base_env=(
            args.base_env.expanduser().resolve() if args.base_env else default_base_env
        ),
        output_compose=(
            args.output_compose.expanduser().resolve()
            if args.output_compose
            else default_output_compose
        ),
        output_env=(
            args.output_env.expanduser().resolve()
            if args.output_env
            else default_output_env
        ),
        rendered_assets_dir=root / ".deepprint_rendered",
        tpot_root=tpot_root,
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    interactive_mode = not args.list_personas and (
        args.interactive or (
        not args.deploy and not args.list_personas and sys.stdin.isatty()
        )
    )

    try:
        if interactive_mode:
            args = configure_interactive_args(args)

        engine = DeepPrintEngine(build_runtime_paths(args))

        if args.list_personas:
            personas = engine.list_personas()
            if not personas:
                print("No persona footprints were found.")
                return 0
            for persona in personas:
                print(persona)
            return 0

        if not args.deploy:
            parser.error("one of the arguments --deploy or --list-personas is required")

        deployment = engine.render(args.deploy)

        if args.dry_run:
            print(deployment.compose_text)
            return 0

        if interactive_mode:
            print(f"Selected persona: {humanize_persona_name(deployment.persona_name)}")
            if engine.paths.tpot_root is not None:
                print(f"T-Pot root: {engine.paths.tpot_root}")
                print(
                    "DeepPrint will back up docker-compose.yml and .env before "
                    "activating the generated configuration."
                )
            if prompt_yes_no(
                "Preview the generated Docker Compose manifest before deployment",
                default=False,
            ):
                print(deployment.compose_text)
            if not prompt_yes_no("Proceed with deployment", default=True):
                print("Deployment cancelled.")
                return 0

        engine.deploy(deployment)
        print(
            f"DeepPrint deployed persona '{deployment.persona_name}' using "
            f"{engine.paths.output_compose}."
        )
        return 0
    except DeepPrintError as exc:
        print(f"DeepPrint error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
