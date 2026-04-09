# Creating Custom Personas

DeepPrint custom personas live under `DeepPrint/footprints/<persona_name>/`.
Each persona is a self-contained footprint that tells DeepPrint how to rename,
re-banner, optionally assign MAC identities, and inject assets into the T-Pot
services it manages.

## Directory Layout

Create a new footprint folder:

```text
DeepPrint/footprints/my_persona/
|-- persona.yaml
`-- assets/
    |-- motd.txt
    `-- index.html
```

At minimum, you should create:

- `persona.yaml`
- any files referenced in `files_to_inject`

## Fastest Way To Start

The easiest path is to copy an existing footprint and edit it:

- `DeepPrint/footprints/power_plant/`
- `DeepPrint/footprints/water_treatment/`
- `DeepPrint/footprints/airport_ops/`

Example:

```bash
cp -r DeepPrint/footprints/power_plant DeepPrint/footprints/my_persona
```

Then rename the folder and replace the hostnames, environment values, and
assets with your own persona data.

## Required `persona.yaml` Structure

Every persona must define:

- `global_prefix`
- `services`
- `files_to_inject`

Minimal example:

```yaml
global_prefix: acmeplant

services:
  cowrie:
    hostname: ssh-gateway
    container_name: acme-ssh
    environment_variables:
      COWRIE_HOSTNAME: ACME-OPS-01
      COWRIE_MOTD_PATH: /etc/motd
      DECEPTION_ROLE: operations-access

  conpot:
    hostname: plc-controller
    container_name: acme-plc
    mac_address_prefix: 00:11:22
    environment_variables:
      CONPOT_TEMPLATE: default
      CONPOT_DEVICE_NAME: Siemens S7 PLC
      PLC_SITE_NAME: Acme Processing Facility
      PLC_PROCESS_AREA: Line 1

  suricata:
    hostname: ids-sensor
    container_name: acme-ids
    environment_variables:
      SURICATA_SENSOR_NAME: ACME-IDS-01
      SENSOR_ROLE: perimeter

files_to_inject:
  - service: cowrie
    source: assets/motd.txt
    destination: /etc/motd
  - service: conpot
    source: assets/index.html
    destination: /usr/lib/python3.11/site-packages/conpot/templates/default/http/htdocs/index.html
```

## What The Keys Mean

- `global_prefix`
  Prepended to rendered hostnames. DeepPrint also sanitizes it for RFC 1123
  hostname safety.
- `services`
  Maps T-Pot service names to persona-specific values.
- `hostname`
  The per-service hostname suffix. DeepPrint combines it with `global_prefix`.
- `container_name`
  The explicit Docker container name to use.
- `environment_variables`
  Environment overrides merged into that service.
- `mac_address`
  Optional full MAC address for a bridged service.
- `mac_address_prefix`
  Optional vendor prefix or OUI. DeepPrint keeps the first three octets and
  randomizes the last three per deployment.
- `files_to_inject`
  Files copied into running containers after startup using `docker cp`.

Only use MAC settings on services that are attached to a bridge or user-defined
Docker network. Services using `network_mode: host` cannot take a DeepPrint MAC
override.

## Supported Services

Your `services` keys must match services present in the base Compose template.
The bundled template currently includes:

- `cowrie`
- `conpot`
- `suricata`

If you expand the template later, you can add matching persona service blocks
for those services as well.

## Prompted Values

If you want the operator to customize values during deployment, add a
`prompts:` block at the top of `persona.yaml`.

Example:

```yaml
prompts:
  - id: site_name
    message: Facility display name
    default: Acme Processing Facility
    required: true
  - id: banner_notice
    message: Login banner warning line
    default: Authorized use only.
    required: true
```

You can then reference those values with `{{variable_name}}` in:

- `persona.yaml`
- `motd.txt`
- `index.html`
- other injected text files

Example:

```yaml
global_prefix: "{{site_name}}"
```

```text
Welcome to {{site_name}}
{{banner_notice}}
```

## Assets And Injection Files

You can inject any local file into a running container as long as:

- the `source` path exists under the persona folder
- the `destination` path is valid inside the target container

Example:

```yaml
files_to_inject:
  - service: cowrie
    source: assets/motd.txt
    destination: /etc/motd
```

Common uses:

- SSH banners
- fake login notices
- service web headers
- configuration-looking files for deception realism

## MAC Identity Options

Sophisticated attackers sometimes compare the first three octets of a MAC
address against the claimed hardware vendor. DeepPrint can help you preserve
that realism while still avoiding a completely static clone.

Example:

```yaml
services:
  conpot:
    hostname: plc-controller
    container_name: acme-plc
    mac_address_prefix: 00:11:22
    environment_variables:
      CONPOT_DEVICE_NAME: Siemens S7 PLC
```

In that case, DeepPrint will render a MAC like `00:11:22:aa:bb:cc` with a
randomized suffix for each deployment.

If you need an exact MAC instead, use:

```yaml
mac_address: 00:11:22:33:44:55
```

Best practice:

- Use a real OUI that matches the vendor you are trying to emulate.
- Prefer `mac_address_prefix` over a fixed `mac_address` unless you have a
  reason to keep the entire value static.
- Keep MAC identity aligned with the rest of the persona so banners, hostnames,
  and device names tell the same story.

## Validation Workflow

Before deploying a new persona, validate it in dry-run mode:

```bash
python3 DeepPrint/deepprint.py --deploy my_persona --dry-run
```

That confirms:

- the persona can be loaded
- required keys are present
- placeholders resolve correctly
- the Compose output can be rendered

You should also verify the persona appears in the catalog:

```bash
python3 DeepPrint/deepprint.py --list-personas
```

## Deploying A Custom Persona

Deploy to a live T-Pot host:

```bash
python3 DeepPrint/deepprint.py --deploy my_persona --tpot-root ~/tpotce
```

Or use the guided flow:

```bash
python3 DeepPrint/deepprint.py --interactive --tpot-root ~/tpotce
```

## Recommended Best Practices

- Start from a footprint that already resembles your target environment.
- Keep hostnames short, specific, and realistic.
- Use prompt variables for values operators may want to change per deployment.
- Use prompt variables for optional MAC OUIs when different sites should present
  different vendors or hardware families.
- Match your `container_name` values to the persona theme for consistency.
- Test every new persona with `--dry-run` before live deployment.
- Validate injected file paths against the real container filesystem when you
  get access to a live T-Pot host.

## Common Mistakes

- Referencing a service name that is not in the base Compose template
- Forgetting to add an asset file referenced in `files_to_inject`
- Using unresolved `{{placeholder}}` variables
- Supplying invalid container destinations
- Making the hostname or banner theme inconsistent across services

## Suggested Workflow For Teams

1. Copy a similar persona as a starting point.
2. Rename the folder and update `persona.yaml`.
3. Add or update injected assets.
4. Run `--dry-run`.
5. Review the rendered Compose output.
6. Deploy to a test T-Pot host.
7. Tune banners, hostnames, and fake metadata until the footprint feels
   coherent.
