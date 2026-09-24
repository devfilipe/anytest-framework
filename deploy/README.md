# Deploying atf as a systemd service

A persistent deployment of the `atf` web dashboard + API: the engine in `/opt/anytest-framework`,
mutable data (config store, run reports, synced checkouts) under `/var/lib/atf`, secrets in an
`EnvironmentFile` **outside the repo**, running as a dedicated unprivileged `atf` user.

[`atf-web.service`](atf-web.service) is the unit; the steps below create everything it expects.
Tested on Ubuntu; adjust `python3-venv` install for your distro.

## 1. Install the engine

```bash
sudo git clone https://github.com/devfilipe/anytest-framework /opt/anytest-framework
cd /opt/anytest-framework
sudo apt-get install -y python3-venv nmap iputils-ping   # venv bootstraps pip; nmap = mgmt scans, ping = reachability
sudo python3 -m venv .venv
sudo .venv/bin/pip install -e ".[host,web]"
```

**`nmap` is a runtime dependency** of the MGMT checks (port scans, `ssl-enum-ciphers` NSE). Without
it the `local` mgmt backend fails at dispatch (`driver dispatch failed`). See *MGMT check backends*
below for the `docker` alternative. `ping` (iputils) is the ICMP probe behind `ctx.<alias>.ping()`
and the inventory reachability button; with neither `ping` nor `nmap` on the vantage a check errors
out saying so, rather than reporting a live board as down.

## 2. Dedicated user + writable data dir

```bash
sudo useradd --system --home-dir /var/lib/atf --create-home --shell /usr/sbin/nologin atf
sudo install -d -o atf -g atf -m 750 /var/lib/atf/reports /var/lib/atf/checkouts
# git refuses a repo owned by another user; let the atf user read the running revision
sudo git config --system --add safe.directory /opt/anytest-framework
```

## 3. Secrets (never committed)

```bash
sudo mkdir -p /etc/atf
sudo tee /etc/atf/atf.env >/dev/null <<EOF
APP_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
DATABASE_URL=file:/var/lib/atf/atf.db
ATF_CHECKOUTS=/var/lib/atf/checkouts
EOF
sudo chgrp atf /etc/atf/atf.env && sudo chmod 640 /etc/atf/atf.env
```

> **`APP_SECRET` must stay stable** — it decrypts stored secrets (Fernet). Back it up together with
> the DB when migrating servers; changing it makes existing stored secrets unreadable.

## 4. Install + start the service

```bash
sudo cp deploy/atf-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now atf-web.service
systemctl status atf-web
```

The dashboard listens on `:8899` (bind `0.0.0.0`; put a reverse proxy in front for public exposure).
Log in as `admin` / `admin` and **change the password immediately** (Admin › Users). The deploy is
**config-driven** — no `ATF_CHECK_SOURCES`; add check-source repos under Admin › Repositories, or
connect an agent.

## 5. MGMT check backends (port scans / TLS audits)

A test plan runs its MGMT checks (an `ip` driver with no agent) through one of two backends:

- **`local`** — runs `nmap` directly on this host. Just needs the `nmap` package (installed in step 1).
  Simplest, lowest-privilege — recommended. The container adds no network advantage (it runs with
  `--network host`); it only packages the toolchain.
- **`docker`** — runs the checks in the `atf-mgmt` toolbox image. Build it and grant the service user
  access to the docker socket:

  ```bash
  cd /opt/anytest-framework
  sudo docker build -f docker/atf-mgmt.Dockerfile -t atf-mgmt:latest .   # or: make image
  sudo usermod -aG docker atf          # ⚠ docker-group ≈ root — weigh the trade-off
  sudo systemctl restart atf-web
  ```

  The unit sets `ATF_WORK=/var/lib/atf/work` on purpose: with `PrivateTmp=true` the service's `/tmp`
  is invisible to the docker daemon, so uploaded-agent trees and run outputs (which the `docker`
  backend bind-mounts) **must** live on a host-visible path. Keep `ATF_WORK` whenever the docker
  backend is used.

Pick the backend per test plan (its *MGMT backend* field) or per ad-hoc run.

## Update flow

```bash
cd /opt/anytest-framework && sudo git pull
sudo .venv/bin/pip install -e ".[host,web]"   # only if dependencies changed
sudo docker build -f docker/atf-mgmt.Dockerfile -t atf-mgmt:latest .   # only if using the docker backend
sudo systemctl restart atf-web
```
