# Moodle image — self-maintained, derived from serversideup/php.
# See README.md for design notes and usage.
#
# Build:
#   podman build \
#     --build-arg MOODLE_VERSION=v5.2.0 \
#     --build-arg PHP_VERSION=8.3 \
#     -t ghcr.io/pikapods/docker-moodle:v5.2.0 .

ARG PHP_VERSION=8.3
# BASE_IMAGE is composed by build.yml as serversideup/php:<minor>-fpm-nginx-alpine
# optionally suffixed with @sha256:... when the watcher resolved a digest. Local
# builds without a digest fall through to the floating tag.
ARG BASE_IMAGE=serversideup/php:${PHP_VERSION}-fpm-nginx-alpine
FROM ${BASE_IMAGE}

ARG MOODLE_VERSION=v5.2.0
ARG MOODLE_REPO=https://github.com/moodle/moodle

# Build identity. IMAGE_REVISION is bumped by build.yml when the same
# MOODLE_VERSION is rebuilt against a new base digest (security patch).
# BASE_DIGEST is the resolved sha256 the FROM line pinned to; upstream-watch
# reads it back off the published image to detect base-image drift.
ARG IMAGE_REVISION=r1
ARG BASE_DIGEST=
ARG GIT_SHA=
ARG BUILD_DATE=

LABEL org.opencontainers.image.title="Moodle" \
      org.opencontainers.image.description="Self-maintained Moodle container" \
      org.opencontainers.image.source="https://github.com/pikapods/docker-moodle" \
      org.opencontainers.image.licenses="GPL-3.0" \
      org.opencontainers.image.version="${MOODLE_VERSION}-${IMAGE_REVISION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.base.name="serversideup/php:${PHP_VERSION}-fpm-nginx-alpine" \
      org.opencontainers.image.base.digest="${BASE_DIGEST}"

USER root

# Runtime + build dependencies.
# Runtime: postgresql-client (pg_isready), mysql-client (mysqladmin ping), tzdata.
# No system cron — Moodle cron and adhoc workers run as s6 longrun services.
RUN apk add --no-cache \
        git \
        postgresql-client \
        mysql-client \
        tzdata \
    # Only the extensions NOT already in the base image. As of
    # serversideup/php 8.3-fpm-nginx-alpine v4.4, the base ships:
    #   pdo_mysql, pdo_pgsql, redis, sodium, zip, opcache (Zend OPcache),
    #   plus the always-on core set (curl, dom, hash, iconv, mbstring,
    #   openssl, simplexml, tokenizer, xml, xmlreader, etc.).
    # That leaves these six for install-php-extensions to compile in:
    #   - mysqli: Moodle's native MySQL/MariaDB drivers require ext/mysqli;
    #     pdo_mysql (already present) is not sufficient.
    #   - pgsql: Moodle's native postgres driver uses procedural pg_*;
    #     pdo_pgsql (already present) is not sufficient.
    #   - gd, intl, soap, exif, bcmath: required by Moodle 5.2
    #     environment.xml.
    && install-php-extensions \
        mysqli \
        pgsql \
        gd \
        intl \
        soap \
        exif \
        bcmath

# Clone Moodle at a pinned version. Source baked into the image —
# image tag = app version, no in-data drift.
RUN git clone --depth=1 --branch="${MOODLE_VERSION}" \
        "${MOODLE_REPO}" /var/www/html \
    && rm -rf /var/www/html/.git /var/www/html/.github

# Baseline of plugins present in the pristine image, so the runtime sync can
# tell user-installed plugins apart from core. Marker = version.php (every
# Moodle plugin has one, at any nesting depth). Paths are relative to public/.
# Stored OUTSIDE public/ (not scanned by Moodle) so it travels with the image
# tag: a newer image with more core plugins ships an updated baseline; a
# volume-stored baseline would wrongly classify last version's core as user
# plugins. The chown -R below covers ownership of this file.
RUN cd /var/www/html/public \
    && find . -name version.php -type f \
        | sed -e 's#^\./##' -e 's#/version\.php$##' \
        | sort > /var/www/html/.moodle-core-plugins.manifest

# Top-level config.php is the canonical location Moodle's public/config.php
# loader requires. Symlink it into /data so install.php's fopen('w') writes
# through to the volume and subsequent boots reuse the same file.
#
# moodledata is NOT symlinked — install.php points $CFG->dataroot at
# /data/moodledata directly and creates the tree itself.
#
# /data itself must exist and be owned by www-data: the container runs as a
# non-root user (UID 82 on Alpine) which cannot create /data under /.
RUN rm -f /var/www/html/config.php \
    && ln -s /data/config/config.php /var/www/html/config.php \
    && mkdir -p /data \
    && chown www-data:www-data /data \
    && chown -R www-data:www-data /var/www/html

# Build-arg UID/GID override. The base image fixes www-data at 82:82; rebuild
# with --build-arg WWW_DATA_UID=$(id -u) --build-arg WWW_DATA_GID=$(id -g) for
# bind-mount UX without host-side chown. Guarded so the default-build path
# adds no extra layer work. See README "User & permissions".
ARG WWW_DATA_UID=82
ARG WWW_DATA_GID=82
RUN if [ "$WWW_DATA_UID" != "82" ] || [ "$WWW_DATA_GID" != "82" ]; then \
        docker-php-serversideup-set-id www-data "${WWW_DATA_UID}:${WWW_DATA_GID}" \
     && docker-php-serversideup-set-file-permissions --owner "${WWW_DATA_UID}:${WWW_DATA_GID}" \
     && chown "${WWW_DATA_UID}:${WWW_DATA_GID}" /data; \
    fi

VOLUME /data

# Overlay our entrypoint hook + s6 longrun services + nginx site config.
COPY rootfs/ /

# - chmod *before* docker-php-serversideup-s6-init: the init tool moves
#   /etc/entrypoint.d/*.sh into /etc/s6-overlay/scripts/ and renames them, so
#   chmod afterwards at the original path would fail.
# - chown /etc/nginx to www-data: ServerSideUp's 10-init-webserver-config
#   runs as www-data and renders /etc/nginx/nginx.conf at boot. After our
#   COPY rootfs/ the directory ends up root-owned and nginx fails to start
#   with "Permission denied" opening nginx.conf.
RUN chmod +x /etc/entrypoint.d/20-moodle-bootstrap.sh \
             /etc/s6-overlay/s6-rc.d/moodle-cron/run \
             /etc/s6-overlay/s6-rc.d/moodle-adhoc/run \
             /etc/s6-overlay/s6-rc.d/moodle-pluginsync/run \
             /usr/local/lib/moodle/plugin-sync.sh \
    && chown -R www-data:www-data /etc/nginx \
    && docker-php-serversideup-s6-init

# Image defaults.
# AUTORUN_ENABLED=false: we own the boot sequence.
# SSL_MODE=off: TLS terminates at the reverse proxy.
# ENABLE_MOODLE_CRON / ENABLE_MOODLE_ADHOC=TRUE: Moodle is broken without
# cron (queues, notifications, gradebook regrade, scheduled tasks).
# PHP_OPCACHE_ENABLE=1: serversideup/php ships opcache disabled; Moodle is
# unusably slow without it. Override to 0 only for debugging.
# PHP_MAX_INPUT_VARS=5000: Moodle 5.2's environment check requires >=5000
# (large forms — gradebook, quiz editor — blow past the PHP default of 1000).
# ENABLE_PLUGIN_SYNC=TRUE: persist web-UI-installed plugins across container
# recreation. Captured under /data/plugins, symlinked back into the codebase
# on boot. PLUGIN_SYNC_INTERVAL is the capture/prune poll period in seconds.
ENV AUTORUN_ENABLED=false \
    SSL_MODE=off \
    ENABLE_MOODLE_CRON=TRUE \
    ENABLE_MOODLE_ADHOC=TRUE \
    ENABLE_PLUGIN_SYNC=TRUE \
    PHP_OPCACHE_ENABLE=1 \
    PHP_MAX_INPUT_VARS=5000 \
    APP_BASE_DIR=/var/www/html

# Healthcheck hits /login/index.php (always 200 after install completes;
# returns a redirect to /install.php while config.php is empty — `curl -f`
# treats 2xx/3xx as success, only 4xx/5xx fails). start-period=180s gives
# install.php enough headroom on slow first boots.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD curl -fsS http://localhost:8080/login/index.php -o /dev/null || exit 1

EXPOSE 8080

USER www-data
