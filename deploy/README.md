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
sudo apt-get install -y python3-venv          # Debian/Ubuntu; venv needs it to bootstrap pip
sudo python3 -m venv .venv
sudo .venv/bin/pip install -e ".[host,web]"
```

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

## Update flow

```bash
cd /opt/anytest-framework && sudo git pull
sudo .venv/bin/pip install -e ".[host,web]"   # only if dependencies changed
sudo systemctl restart atf-web
# also `make image` if you dispatch mgmt checks with --mgmt-backend docker
```
