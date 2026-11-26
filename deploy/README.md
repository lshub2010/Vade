# deploy — production samples

Copy-and-edit samples for running the Vade core. They match the code on
`dev_gingshan` for `0.0.5-alpha`; the reasoning behind every value is in
[`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md) (中文：[`docs/DEPLOYMENT.zh-CN.md`](../docs/DEPLOYMENT.zh-CN.md)).
`schema/config.schema.json` remains the authority for config keys.

| File | What it is |
|---|---|
| `core.toml.example` | minimal `core.toml` for a host install (`/etc/vade/core.toml`) |
| `env.example` | environment file holding the LLM key (`/etc/vade/vade.env`) — no real secrets |
| `systemd/vade-core.service` | hardened systemd unit, `User=vade`, `UMask=0077` |
| `Caddyfile` | Caddy v2 vhost for the **data plane** only |
| `nginx.conf` | Nginx `conf.d` drop-in for the **data plane** only |
| `Dockerfile` | image built from the release tarball (no registry image is published) |
| `docker-compose.yml` | compose service: loopback publish, named volume, healthcheck |

## Host install (systemd)

```bash
# 1) install the release (docs/DEPLOYMENT.md §3) to
#    /opt/vade/releases/vade-core-0.0.5-alpha-linux-x86_64 + /opt/vade/current

sudo install -d -m 0750 -o root -g vade /etc/vade
sudo install -d -m 0700 -o vade -g vade /var/lib/vade

sudo cp deploy/core.toml.example /etc/vade/core.toml
sudo cp deploy/env.example      /etc/vade/vade.env
sudo editor /etc/vade/core.toml /etc/vade/vade.env      # set base_url/model and the key

sudo chown root:vade /etc/vade/core.toml /etc/vade/vade.env
sudo chmod 0640     /etc/vade/core.toml /etc/vade/vade.env

sudo cp deploy/systemd/vade-core.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vade-core
sudo journalctl -u vade-core -f
```

Then either finish first-run setup through the local CLI
(`vade admin init --password-stdin`, §7.1) or pair a browser with the
`pair_code=` line from the journal (§8). Put `deploy/Caddyfile` or
`deploy/nginx.conf` in front if you want HTTPS on 443.

The admin entry point is `http://127.0.0.1:8444/login` (not `/admin`, which has
no route). The release ships `admin-ui/dist`; the core mounts `/assets` and
serves the SPA, so `/login` is usable in a browser. If the bundle cannot load,
fall back to the CLI and the JSON API (docs/DEPLOYMENT.md §7.4).

## Container

```bash
cp deploy/core.toml.example deploy/core.toml
cp deploy/env.example      deploy/vade.env && chmod 600 deploy/vade.env
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f vade-core
```

`docker-compose.yml` overrides `data_dir` with
`VADE_CORE__DATA_DIR=/var/lib/vade/data`, so the sample `core.toml` works
unchanged. The data plane is published on `127.0.0.1:8443`; keep the proxy on
the host. The admin plane is not published — use `docker compose exec` (§10.3
explains why a published admin port cannot pass its Host check).

## What these samples deliberately do **not** do

* **Proxy `/admin`.** The admin plane only accepts `Host`/`Origin` equal to
  `[core].admin_host` (default `127.0.0.1:8444`); a name-based vhost gets
  `403 ORIGIN_MISMATCH`. Use an SSH tunnel, or bind it to one LAN address and
  give the core its own certificate.
* **Generate TLS certificates.** `tls.ensure_certificates()` exists but
  `python -m vade` never calls it in this build, so `[tls]` empty means
  plaintext HTTP. Terminate TLS at Caddy/Nginx, or supply PEM files.
* **Install modules.** Modules are separate processes with their own release
  wheels; module pair codes are not usable yet, so their keys go into
  `data/identity/whitelist.json` by hand (docs/DEPLOYMENT.md §9.3).
