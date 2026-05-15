# docker-moodle

[Moodle](https://moodle.org/) container image, built on
[`serversideup/php`](https://serversideup.net/open-source/docker-php/).

This image powers Moodle on [PikaPods](https://www.pikapods.com) and is
maintained by the PikaPods team. It's published here for our users' reference
and the benefit of the wider community.

Published to both `ghcr.io/pikapods/docker-moodle:<moodle-version>` and
`pikapods/docker-moodle:<moodle-version>` (Docker Hub).

Source: https://github.com/pikapods/docker-moodle

## Why this image

Moodle has no high-quality community Docker image. The dominant choice —
Bitnami's image — persists the entire Moodle codebase under
`/bitnami/moodle`, which makes image-tag upgrades unreliable (volume code
shadows new code) and produces large layers.

This image bakes the Moodle source into the image at a pinned tag and
persists only operator state under `/data`: the generated `config.php` and
the Moodle `dataroot`. Image upgrade = pull a new tag; nothing in the volume
shadows the new code.

## Quick start

The bundled `compose.yaml` brings up Moodle plus a MariaDB sidecar with zero
external dependencies — the fastest way to try the image:

```bash
git clone https://github.com/pikapods/docker-moodle.git
cd docker-moodle
docker compose up -d
# First-boot install runs admin/cli/install.php and takes 1–3 minutes.
docker compose logs -f moodle | grep -E 'bootstrap|install\.php'
# Once the healthcheck flips to "healthy", browse:
curl -I http://localhost:8080/login/index.php   # → HTTP/1.1 200 OK
```

Default credentials are `admin` / `changeme` — change them before any real
deployment.

Against an existing database:

```bash
docker run -d --name moodle \
  -v moodle-data:/data \
  -e MOODLE_URL="https://learn.example.com" \
  -e DB_TYPE=mariadb \
  -e DB_HOST=db.internal \
  -e DB_NAME=moodle \
  -e DB_USER=moodle \
  -e DB_PASS=... \
  -e ADMIN_USER=admin \
  -e ADMIN_PASS=changeme \
  -e ADMIN_EMAIL=admin@example.com \
  -e SITE_FULLNAME="My Moodle" \
  -e SITE_SHORTNAME=moodle \
  -p 8080:8080 \
  ghcr.io/pikapods/docker-moodle:latest
```

### Running on podman

The compose file and `docker run` examples work as-is under
`podman compose` / `podman run`. Three podman-specific notes:

- **Build:** `podman build --format docker …`. Podman defaults to OCI
  manifests, which silently drop the `HEALTHCHECK` instruction; docker
  format embeds it.
- **Rootless permissions:** rootless podman remaps UIDs, so the container's
  `www-data` (UID 82) isn't host UID 82. For bind mounts, add
  `--userns=keep-id:uid=82,gid=82`. Rootful podman behaves like docker.
  Full decision matrix in [User & permissions](#user--permissions).
- **Healthcheck inspection:** `podman healthcheck run <container>` runs the
  check on demand. Docker runs it automatically; inspect with
  `docker inspect --format '{{.State.Health.Status}}' <container>`.

## Environment variables

### Core

| Var               | Required | Purpose                                                              |
|-------------------|----------|----------------------------------------------------------------------|
| `MOODLE_URL`      | yes      | Public URL (no trailing slash). Patched into `$CFG->wwwroot` every boot. |
| `DB_TYPE`         | no       | `mariadb` (default), `mysql`, or `pgsql` (aliases: `postgres`, `postgresql`). |
| `DB_HOST`         | yes      | DB hostname.                                                         |
| `DB_PORT`         | no       | DB port. Defaults to 3306 (mariadb/mysql) or 5432 (pgsql).           |
| `DB_NAME`         | yes      | DB name.                                                             |
| `DB_USER`         | yes      | DB user.                                                             |
| `DB_PASS`         | no       | DB password. May be empty for passwordless local dev DBs.            |
| `DB_PREFIX`       | no       | Table prefix. Defaults to `mdl_`. Honored on first boot only.        |

### Install seed (first boot only)

| Var              | Required when         | Purpose                                          |
|------------------|-----------------------|--------------------------------------------------|
| `ADMIN_USER`     | no                    | Admin username. Defaults to `admin`.             |
| `ADMIN_PASS`     | first boot            | Admin password.                                  |
| `ADMIN_EMAIL`    | first boot            | Admin email.                                     |
| `SITE_FULLNAME`  | no                    | Site full name. Defaults to `Moodle`.            |
| `SITE_SHORTNAME` | no                    | Site short name. Defaults to `moodle`.           |
| `SUPPORT_EMAIL`  | no                    | Defaults to `ADMIN_EMAIL`.                       |
| `MOODLE_LANG`    | no                    | Install language. Defaults to `en`.              |

Once `config.php` exists in the volume, install-seed vars are ignored. Safe
to leave them set on subsequent boots.

### Cron services

| Var                    | Default | Purpose                                                |
|------------------------|---------|--------------------------------------------------------|
| `ENABLE_MOODLE_CRON`   | `TRUE`  | Disable with any of `FALSE`/`false`/`0`/`no`/`off`. Stops the per-minute `cron.php` loop. |
| `ENABLE_MOODLE_ADHOC`  | `TRUE`  | Disable with any of `FALSE`/`false`/`0`/`no`/`off`. Stops the adhoc task worker. |

Both run as s6 long-runs inside the container. Moodle is broken without cron
(notifications, queues, gradebook regrade, scheduled tasks), so the only
sensible default is `TRUE`.

The cron service invokes `admin/cli/cron.php --keep-alive=0` once per minute.
This leaves scheduling cadence to the container wrapper and avoids Moodle's
default 3-minute cron keep-alive being terminated by the wrapper timeout.

### Moodle `config.php` passthrough

Any env var named `MOODLE_CFG_<KEY>` is patched into a managed block inside
`/data/config/config.php` on every boot. The key is lowercased and assigned
to `$CFG-><key>`. Example:

```
MOODLE_CFG_SMTPHOSTS=smtp.mailgun.org:587
MOODLE_CFG_NOEMAILEVER=true
MOODLE_CFG_MAXBYTES=104857600
```

becomes

```php
$CFG->smtphosts = 'smtp.mailgun.org:587';
$CFG->noemailever = true;
$CFG->maxbytes = 104857600;
```

inside the `// BEGIN moodle-image-managed` … `// END moodle-image-managed`
block, which sits immediately *above* the `require_once('lib/setup.php')`
line so its assignments win over any install-written defaults.

**Type coercion.** Values are emitted as:

| Value pattern              | PHP literal              |
|----------------------------|--------------------------|
| `true` / `TRUE` / `True`   | `true`                   |
| `false` / `FALSE` / `False`| `false`                  |
| `NULL` / `Null`            | `null`                   |
| `^-?[0-9]+$`               | int                      |
| anything else              | single-quoted string     |

**Sentinel deletion.** Setting `MOODLE_CFG_<KEY>=unset`, `=null` (lowercase),
or empty deletes the key on the next boot. To set a PHP null literal, use
`NULL` or `Null`.

**Key validation.** Keys are lowercased and must match `^[a-z][a-z0-9_]*$`.
Invalid keys (dashes, regex metachars, names starting with a digit) are
logged and skipped.

## Mounts

| Path              | Purpose                                                            |
|-------------------|--------------------------------------------------------------------|
| `/data`           | Persistent volume. Contains `config/config.php` and `moodledata/`. |
| `/var/www/html`   | Moodle source. Baked at build time — do **not** bind-mount.        |

`/var/www/html/config.php` is a symlink into `/data/config/config.php` so the
single canonical config file lives on the volume. Moodle's `dataroot`
(uploads, sessions, cache, locks) is at `/data/moodledata/` — outside the
docroot and therefore not web-accessible.

### User & permissions

Both nginx and php-fpm run as `www-data` (**UID 82 / GID 82** — Alpine's
default, inherited from `serversideup/php:*-alpine`). How those writes
surface on the host depends on your runtime; pick the row that matches:

| Setup                                  | What to do                                                                                                   | Host-side ownership of `/data` writes |
|----------------------------------------|--------------------------------------------------------------------------------------------------------------|---------------------------------------|
| Named volume (docker or podman)        | Nothing — daemon manages ownership. Default in `compose.yaml`.                                               | Inside daemon-managed volume; not user-visible. |
| Bind mount, rootful docker/podman      | `chown -R 82:82 <host-dir>` before first boot.                                                               | `82:82`.                              |
| Bind mount, rootless podman            | Add `--userns=keep-id:uid=82,gid=82` to `podman run`.                                                        | Invoking host user's UID/GID.         |
| Custom-UID rebuild                     | `docker build --build-arg WWW_DATA_UID=$(id -u) --build-arg WWW_DATA_GID=$(id -g) -t moodle:local .`         | The UID baked at build time.          |

The bootstrap runs a preflight writability check on `/data` and refuses to
start with a readable error if ownership is wrong, rather than failing
cryptically deep in `mkdir`.

Why not a runtime `PUID`/`PGID` env var? Upstream `serversideup/php` v3
[deliberately removed root from the boot path](https://github.com/serversideup/docker-php/issues/253),
and runtime UID remap requires reintroducing it. The supported lever is the
build-time `WWW_DATA_UID`/`WWW_DATA_GID` rebuild above.

## Ports

| Port | Purpose                                                                  |
|------|--------------------------------------------------------------------------|
| 8080 | HTTP (serversideup's unprivileged default).                              |

Behind a reverse proxy this is invisible to end users.

## Behind a TLS-terminating reverse proxy

- `MOODLE_CFG_SSLPROXY=true` — treat the request as HTTPS even though the upstream hop is plain HTTP.
- `MOODLE_CFG_REVERSEPROXY=true` — trust `X-Forwarded-For` for client-IP logging.

## `config.php` ownership model

`/data/config/config.php` is **user state**, not a regenerated artifact —
generated once by `admin/cli/install.php` on first boot and preserved on
every subsequent boot.

Each boot, the image strips any prior `// BEGIN moodle-image-managed` …
`// END moodle-image-managed` block from `config.php` and re-inserts a fresh
one immediately before the `require_once(.../lib/setup.php)` line. The block
contains:

1. **Always-patched ops keys** from env, in order:
   `wwwroot`, `dbtype`, `dbhost`, `dbname`, `dbuser`, `dbpass`,
   `dboptions['dbport']`. Env wins — change any of these and restart, and
   the next boot's managed block reflects the new value.
2. **Any `MOODLE_CFG_*` env vars**, type-coerced per the table above.

Everything else in `config.php` is preserved untouched. Hand-edits via
`docker exec`, custom settings you've appended below the managed block — all
survive boots. Because the managed block sits *above* `require_once` but
*below* install.php's own assignments, its keys overwrite the install-time
values; assignments you add *below* the block (your hand-edits) still win,
since later PHP assignments overwrite earlier ones.

`DB_PREFIX` is honored on first boot only — changing the table prefix on an
existing install would orphan all data. Set it correctly the first time.

## Major-version upgrades

Across minor Moodle versions this image upgrades cleanly: pull a new tag and
restart. Moodle handles the version-mismatch dialog automatically on first
admin login.

Across major versions (`v5.x` → `v6.x`), run the upgrade CLI once manually:

```bash
docker compose pull moodle && docker compose up -d moodle
docker compose exec moodle php /var/www/html/admin/cli/upgrade.php \
    --non-interactive
```

This image does not run `upgrade.php` automatically — silent schema upgrades
across majors are the wrong default for production data.

## Deliberate breaks vs. `bitnami/moodle`

| Break                                                          | Rationale                                                  |
|----------------------------------------------------------------|------------------------------------------------------------|
| Default port `8080` (was `8080` HTTP / `8443` HTTPS)           | Same.                                                      |
| App lives at `/var/www/html` (was `/bitnami/moodle`)           | Source baked into image. `/bitnami/moodle` shadows new code on upgrade. |
| Only `config.php` + `moodledata` persisted, not the codebase   | Image tag upgrades are reliable; no in-volume drift.       |
| Env vars renamed (`MOODLE_*` → simpler `MOODLE_URL` / `DB_*`)  | Not drop-in compatible. Document migration if you need it. |
| `ENABLE_MOODLE_CRON` defaults `TRUE`                           | Moodle is broken without it.                               |
| No auto-`upgrade.php` across majors                            | Silent schema upgrades are wrong for production data.      |

## Building locally

```bash
docker build \
  --build-arg MOODLE_VERSION=v5.2.0 \
  --build-arg PHP_VERSION=8.3 \
  -t moodle:test .
```

On podman, add `--format docker` — see the podman notes in Quick start for
why.

### Pinning inputs

The published image uses floating tags (`serversideup/php:8.3-…` base,
Moodle git tag) so the daily CI rebuild picks up upstream security fixes.

For a more reproducible build:

- **Moodle version.** `MOODLE_VERSION` is passed to `git clone --branch`, so
  it must be a tag or branch name — `v5.2.0`, `MOODLE_502_STABLE`, etc.
  Arbitrary commit SHAs are **not supported** by `--branch`; if you need
  commit-level pinning, fork the Dockerfile and replace the clone step with
  a `git fetch && git checkout <sha>`.
- **Base image.** Override `serversideup/php` in your own derived
  Dockerfile with a digest pin:
  `FROM serversideup/php:8.3-fpm-nginx-alpine@sha256:…`.
- **Compose.** The bundled `compose.yaml` defaults to `:latest` for the
  quick-start path; in production, pin to a specific Moodle version tag
  (e.g. `ghcr.io/pikapods/docker-moodle:v5.2.0`).

## License

The Moodle source is GPL-3.0; this image inherits that license.
