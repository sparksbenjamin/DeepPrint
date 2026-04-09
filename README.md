# DeepPrint

DeepPrint is a Python deployment framework for re-skinning a
[T-Pot](https://github.com/telekom-security/tpotce) installation into a chosen
operational persona. It generates a persona-specific Docker Compose manifest,
sets explicit hostnames and container names, updates environment values, and
can inject banner and content files into running honeypot containers.

The goal is simple: land on a T-Pot host, run one command, choose a persona,
and deploy a more believable deception footprint without hand-editing Compose
files, environment variables, and banner assets by hand.

## Why Use DeepPrint With T-Pot

T-Pot gives you a powerful honeypot platform, but a default deployment can
still look generic. DeepPrint makes a T-Pot install more useful during
deception-oriented deployments by helping it look intentional rather than
stock.

In practice, that means:

- more believable hostnames and service identities
- consistent persona data across containers, banners, and injected files
- faster deployment for operators who do not want to hand-tune YAML on-host
- safer changes through preview mode, backups, and restore support

## What DeepPrint Does

- Applies a persona to T-Pot services such as `cowrie`, `conpot`, and `suricata`
- Forces explicit `hostname` values in Compose so Docker does not assign random IDs
- Updates environment variables to reflect the selected identity
- Supports prompted values for banners, hostnames, site names, and other persona data
- Injects local files like `motd.txt` or `web.config` into running containers
- Supports both preview mode and live deployment mode
- Can operate directly against a live `~/tpotce` installation

## Quick Start

For most operators on a live T-Pot host, this is the fastest path:

```bash
curl -fsSL https://raw.githubusercontent.com/sparksbenjamin/DeepPrint/main/bootstrap.py | python3 -
```

That bootstrap will:

1. Download DeepPrint into `~/.deepprint`
2. Ensure `PyYAML` is installed
3. Launch the interactive wizard
4. Detect a likely T-Pot installation such as `~/tpotce`
5. Walk you through persona selection, prompts, preview, and deployment

If you only want to preview a persona first:

```bash
python3 DeepPrint/deepprint.py --deploy power_plant --dry-run
```

## Repository Layout

```text
DeepPrint/
|-- DeepPrint/
|   |-- deepprint.py
|   |-- footprints/
|   `-- templates/
|-- bootstrap.py
`-- README.md
```

- `DeepPrint/deepprint.py`
  Main engine and CLI for rendering or deploying personas.
- `DeepPrint/footprints/`
  Persona definitions and injectable assets.
- `DeepPrint/templates/`
  Base Compose and environment templates used for rendering.
- `bootstrap.py`
  One-line bootstrap entrypoint for operators on a T-Pot host.

## Requirements

- Python 3.10+
- Docker with either `docker compose` or `docker-compose`
- `PyYAML`
- A T-Pot installation if you want to deploy against a live host

The bootstrap script will attempt to install `PyYAML` automatically if it is
missing.

If you are working from a clone of this repository, install runtime
dependencies with:

```bash
python -m pip install -r requirements.txt
```

## One-Line Launch On T-Pot

On a T-Pot host, run:

```bash
curl -fsSL https://raw.githubusercontent.com/sparksbenjamin/DeepPrint/main/bootstrap.py | python3 -
```

This is the recommended deployment path when you want the tool to guide the
entire process from persona selection through render and deployment.

## Interactive Usage

If the repository is already present locally:

```bash
python3 DeepPrint/deepprint.py --interactive
```

To point directly at a live T-Pot install:

```bash
python3 DeepPrint/deepprint.py --interactive --tpot-root ~/tpotce
```

The interactive flow can:

- List available personas
- Prompt for a target T-Pot root
- Ask persona-specific questions
- Offer a dry run
- Show the generated Compose manifest before deployment
- Ask for final confirmation before making changes

## Non-Interactive Usage

List available personas:

```bash
python3 DeepPrint/deepprint.py --list-personas
```

Preview a persona without deploying:

```bash
python3 DeepPrint/deepprint.py --deploy power_plant --dry-run
```

Deploy a persona against the bundled templates:

```bash
python3 DeepPrint/deepprint.py --deploy power_plant
```

Deploy directly to a live T-Pot host:

```bash
python3 DeepPrint/deepprint.py --deploy power_plant --tpot-root ~/tpotce
```

Restore the previous live T-Pot configuration from DeepPrint backup files:

```bash
python3 DeepPrint/deepprint.py --restore --tpot-root ~/tpotce
```

## Live T-Pot Behavior

When `--tpot-root ~/tpotce` is used, DeepPrint treats that directory as the
active T-Pot installation and will:

1. Stop the current stack using the active Compose and `.env`
2. Render new DeepPrint-specific files into the T-Pot root
3. Back up the active files as:
   - `docker-compose.yml.deepprint.bak`
   - `.env.deepprint.bak`
4. Replace:
   - `docker-compose.yml`
   - `.env`
5. Start the updated stack
6. Inject persona assets into the running containers with `docker cp`

It also writes:

- `docker-compose.deepprint.yml`
- `.env.deepprint`

These generated files make it easier to inspect what DeepPrint rendered before
or after deployment.

## Rollback And Restore

Every live deployment stores backups of the previously active T-Pot files:

- `docker-compose.yml.deepprint.bak`
- `.env.deepprint.bak`

To roll back to the last pre-DeepPrint state:

```bash
python3 DeepPrint/deepprint.py --restore --tpot-root ~/tpotce
```

The restore command will:

1. Stop the currently active stack
2. Copy the DeepPrint backup files back into place
3. Restart T-Pot using the restored files

## Prompted Persona Values

Personas can define a `prompts:` block. When present, DeepPrint asks the
operator for those values in an interactive terminal and uses the answers to
render service settings and injectable text files.

Typical prompted values include:

- site name
- hostname prefixes
- SSH banner hostnames
- IDS sensor names
- warning text
- operations contact names

When stdin is non-interactive, DeepPrint uses the prompt defaults.

## Persona Format

Each footprint lives under `DeepPrint/footprints/<persona_name>/` and typically
contains:

- `persona.yaml`
- `assets/motd.txt`
- `assets/web.config`

Example:

```yaml
prompts:
  - id: site_name
    message: Facility display name
    default: River Bend Generation Station
    required: true

global_prefix: riverbend

services:
  cowrie:
    hostname: eng-workstation-ssh
    container_name: siemens-eng-ssh
    environment_variables:
      COWRIE_HOSTNAME: SIEMENS-ENG-WS01
      COWRIE_MOTD_PATH: /etc/motd

files_to_inject:
  - service: cowrie
    source: assets/motd.txt
    destination: /etc/motd
```

DeepPrint validates required keys and will fail fast on missing or malformed
persona data.

## Included Footprints

The repository currently ships with 17 deployable personas:

- `airport_ops`
- `datacenter_core`
- `food_processing`
- `hospital_imaging`
- `maritime_port`
- `mining_operations`
- `oil_gas_pipeline`
- `pharma_cleanroom`
- `power_plant`
- `rail_operations`
- `retail_payment`
- `semiconductor_fab`
- `smart_warehouse`
- `telecom_edge`
- `university_research`
- `utility_substation`
- `water_treatment`

See [DeepPrint/footprints/README.md](DeepPrint/footprints/README.md) for the
catalog summary.

## CLI Reference

```text
usage: deepprint.py [-h] [--deploy PERSONA] [--list-personas] [--restore]
                    [--interactive] [--dry-run] [--tpot-root TPOT_ROOT]
                    [--base-compose BASE_COMPOSE] [--base-env BASE_ENV]
                    [--output-compose OUTPUT_COMPOSE]
                    [--output-env OUTPUT_ENV]
```

- `--deploy PERSONA`
  Render or deploy the specified persona.
- `--list-personas`
  Print all available personas and exit.
- `--restore`
  Restore `docker-compose.yml` and `.env` from DeepPrint backup files.
- `--interactive`
  Launch the guided wizard.
- `--dry-run`
  Print the generated Compose manifest instead of deploying.
- `--tpot-root`
  Target a live T-Pot install such as `~/tpotce`.
- `--base-compose`
  Override the base Compose template path.
- `--base-env`
  Override the base environment file path.
- `--output-compose`
  Override the generated Compose output path.
- `--output-env`
  Override the generated environment output path.

## Safety Notes

- DeepPrint is intended for controlled lab, deception, and research use.
- Review the generated Compose and env files before exposing a host.
- If you are deploying to a live T-Pot installation, treat this as a stack
  reconfiguration and schedule appropriately.
- Generated files and backups should be retained until the deployment is
  validated.

## Development Notes

Useful commands while working on the project:

```bash
python -m pip install -r requirements.txt
python -m py_compile DeepPrint/deepprint.py bootstrap.py
python DeepPrint/deepprint.py --list-personas
python DeepPrint/deepprint.py --deploy power_plant --dry-run
python -m unittest discover -s tests -v
```

## Testing And CI

The repository includes:

- `tests/test_deepprint.py`
  Smoke tests for persona rendering, live deploy file handling, and restore.
- `tests/test_bootstrap.py`
  A bootstrap smoke test that verifies the one-line launcher builds a local
  install and hands control to the CLI.
- `.github/workflows/ci.yml`
  GitHub Actions workflow that installs dependencies, compiles the scripts,
  lists personas, and runs the unit test suite on Python 3.10 and 3.11.
