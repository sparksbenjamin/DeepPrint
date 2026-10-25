# cloud_storage

A deception persona that turns a T-Pot host into a self-hosted file cloud
estate. The web surface presents a Nextcloud-style file-sharing service that
fingerprints as **both a PHP application server and a Node.js sidecar**,
which is a very common real-world deployment shape (nginx fronting PHP-FPM
for the Nextcloud application plus a Node.js collaboration service).

This is the first persona that ships its own base Compose template. Its
`persona.yaml` declares:

```yaml
base_template: templates/tpot-web.yml
```

so rendering and deploying it uses `DeepPrint/templates/tpot-web.yml`
automatically. No `--base-compose` flag is required. An explicit
`--base-compose` argument still wins over the persona declaration.

## The Story

An operator lands on this estate and sees:

- `cloud-web` - the public web target (Nextcloud-style file cloud on PHP
  with a Node.js collaboration sidecar), served on port `8081`
- `bastion-ssh` - the SSH jump host (Cowrie) on ports `2222`/`2323`
- `rack-pdu` - the data hall rack PDU web UI (Conpot) on ports `8080`/`102`
- `nsm-sensor` - the network sensor (Suricata) on the host network

## Web Decoy Surface

The `webdecoy` service runs a single-file Python application from
`assets/webapp.py` (stdlib only, no pip installs). It is injected into the
container with `docker cp` after startup; the container's command waits for
the file to appear and then executes it.

PHP surface (Nextcloud look-alike):

| Path | Behavior |
| --- | --- |
| `/` | 302 to `/index.php/login` |
| `/index.php/login` | Nextcloud-style login page; `PHPSESSID` cookie; failed POSTs are logged with the submitted credentials |
| `/index.php/lostpassword` | Password reset form (stubbed) |
| `/status.php` | Nextcloud status JSON with the configured version |
| `/remote.php/dav` | WebDAV endpoint; 401 with `WWW-Authenticate: Basic realm="Nextcloud"` and Sabre-style XML; submitted Basic credentials are logged |
| `/ocs/v1.php`, `/ocs/v2.php` | OCS API stubs |
| `/cron.php`, `/robots.txt`, `/favicon.ico` | Standard Nextcloud support files |
| anything else | Nextcloud-themed 404 |

Node.js surface (collaboration sidecar):

| Path | Behavior |
| --- | --- |
| `/api/v1/status` | Express-style status JSON leaking the Node.js version |
| `/socket.io/` | socket.io v4 polling handshake (`sid`, `upgrades`) |
| `/metrics` | prom-client style exposition with `nodejs_*` metrics |
| `/healthz` | Sidecar health check |
| other `/api/*` | Express-style `{"message":"Cannot GET ..."}` 404 |

Common response headers on the PHP surface include `Server: nginx/...` and
`X-Powered-By: PHP/...`; the Node surface answers with
`X-Powered-By: Express`. Both stacks are fingerprintable with standard
scanner tooling.

## Deploying

Deploy like any other persona:

```bash
python3 DeepPrint/deepprint.py --deploy cloud_storage --tpot-root ~/tpotce
```

or through the interactive wizard / bootstrap flow. Preview first with
`--dry-run` as usual.

The `webdecoy` service publishes host port `8081`.

### Fronting It With A WAF Or Reverse Proxy

For WAF evaluation, do not expose `8081` directly. Put your WAF or reverse
proxy in front and point its upstream for the site at
`http://<tpot-host>:8081`. Publish ports `80`/`443` on the proxy, and
issue a certificate for the hostname you deploy under. Issuing a
certificate through a public ACME CA also places the hostname in
Certificate Transparency logs, which is the fastest way for subdomain
scanners to discover it.

Firewall `8081` to the proxy address so attackers cannot bypass the WAF.

### Adding webdecoy To An Existing Full T-Pot Stack

If you want to keep your existing full T-Pot Compose stack and only add the
web decoy, copy the `webdecoy` service block from
`DeepPrint/templates/tpot-web.yml` into your own `docker-compose.yml`, then
inject the application manually:

```bash
docker cp DeepPrint/footprints/cloud_storage/assets/webapp.py cloud-estate-web:/srv/decoy/webapp.py
```

The container command waits for the file, so no restart is required.

## Logging

Every request is logged as a JSON line to stdout (`docker logs`) and to
`./data/webdecoy/log/decoy.log` on the host:

- `request` - method, path, query, user agent, status, zone (php/node)
- `login_attempt` - submitted username and password from the login form
- `basic_auth_attempt` - decoded credentials sent to `/remote.php/*`
- `password_reset_attempt` - email addresses submitted to lostpassword

Credential capture is the point of a credential-harvesting honeypot: review
the log before sharing it, since it will contain attacker-supplied secrets.

## Tuning

All values are prompted at deploy time (with the defaults shown):

| Prompt | Default |
| --- | --- |
| `global_prefix` | `smcloud` |
| `site_name` | `Sparks Manor Cloud` |
| `deploy_domain` | `cloud.sparksmanor.com` |
| `nextcloud_version` | `30.0.4` |
| `php_version` | `8.3.14` |
| `node_version` | `v20.11.1` |
| `banner_notice` | `All sessions are monitored...` |
| `operations_contact` | `SM-CLOUD-OPS` |

Version strings only change what the decoy advertises. Pick versions that
are believable for the current date; old versions attract more scanners.

## Safety Notes

- This persona is for deception and WAF evaluation in environments you
  control. Do not point it at production assets.
- The decoy intentionally advertises exploitable-looking software; expect
  sustained automated attack traffic once it is discoverable.
- Nothing in the decoy executes attacker input; it only records it.
