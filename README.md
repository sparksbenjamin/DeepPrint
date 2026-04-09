# DeepPrint

DeepPrint is a Python automation framework for applying persona-based
"re-skins" to a T-Pot honeypot deployment. It renders a persona-specific
Docker Compose manifest, updates the runtime environment file, and can inject
custom honey-files into running containers after deployment.

Personas can optionally define interactive prompts so operators can set
hostnames, banners, site names, and other values at deploy time.

For live T-Pot installs, DeepPrint can detect `~/tpotce`, guide the operator
through persona selection, generate DeepPrint-specific compose and env files,
back up the active `docker-compose.yml` and `.env`, and then activate the new
configuration.

## Layout

- `DeepPrint/deepprint.py` - main deployment engine
- `DeepPrint/footprints/` - persona footprints, assets, and injectables
- `DeepPrint/templates/` - base `tpot.yml`, `.env`, and service config placeholders

## Quick Start

```powershell
python DeepPrint/deepprint.py --list-personas
python DeepPrint/deepprint.py --deploy power_plant --dry-run
python DeepPrint/deepprint.py --deploy power_plant
```

## One-Line T-Pot Launch

On a T-Pot host, run:

```bash
curl -fsSL https://raw.githubusercontent.com/sparksbenjamin/DeepPrint/main/bootstrap.py | python3 -
```

That bootstrap command downloads DeepPrint into `~/.deepprint`, ensures
`PyYAML` is available, launches the interactive wizard, detects a live T-Pot
installation such as `~/tpotce`, and then walks the operator through selecting
a persona, previewing the generated manifest, and deploying it.

If you already cloned this repository on the host, you can launch the same
guided flow directly:

```bash
python3 DeepPrint/deepprint.py --interactive
python3 DeepPrint/deepprint.py --interactive --tpot-root ~/tpotce
```

## Prompted Values

If a persona defines a `prompts:` block, `deepprint.py` will ask for those
values in an interactive terminal and then apply the answers to service
hostnames, environment variables, and text assets such as `motd.txt` and
`web.config`. When stdin is non-interactive, DeepPrint falls back to the
prompt defaults.

## Included Footprints

The repository now ships with 17 deployable persona templates. See
`DeepPrint/footprints/README.md` for the full catalog and deployment targets.

## Requirements

- Python 3.10+
- `PyYAML`
- Docker with either `docker compose` or `docker-compose`
