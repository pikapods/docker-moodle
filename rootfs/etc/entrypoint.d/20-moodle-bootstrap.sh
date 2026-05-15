#!/bin/sh
# Moodle bootstrap — runs once before s6 starts nginx + php-fpm.
# POSIX sh. Pipelines are avoided so install.php exit status is never masked.
set -eu

APP_DIR=/var/www/html
CONFIG_DIR=/data/config
CONFIG_FILE=/data/config/config.php
DATAROOT=/data/moodledata

log() { printf '[moodle-bootstrap] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# Pre-flight: refuse the legacy /data/config-as-file layout.
# ---------------------------------------------------------------------------
if [ -e "$CONFIG_DIR" ] && [ ! -d "$CONFIG_DIR" ]; then
    echo "ERROR: /data/config exists but is not a directory (legacy single-file layout)." >&2
    echo "       This image expects /data/config/ to be a directory containing config.php." >&2
    echo "       Move the existing file aside and re-run." >&2
    exit 1
fi

# Preflight writability. Without this, an unchowned bind-mount surfaces as a
# bare `mkdir: Permission denied` deep in the boot sequence and the container
# crash-loops with no actionable hint.
if ! ( : > /data/.write-test ) 2>/dev/null; then
    cat >&2 <<EOF
ERROR: /data is not writable by the container (container UID:GID $(id -u):$(id -g)).
       Ownership of the bind-mount target must match how the container sees it.
       The right fix depends on your runtime — see README "User & permissions":
         - rootful docker/podman bind mount: chown the host dir to $(id -u):$(id -g)
         - rootless podman: add --userns=keep-id:uid=$(id -u),gid=$(id -g)
         - or use a named volume
         - or rebuild with --build-arg WWW_DATA_UID=...
EOF
    exit 1
fi
rm -f /data/.write-test

# ---------------------------------------------------------------------------
# 1. Validate required env, map DB_TYPE, default DB_PORT.
# ---------------------------------------------------------------------------
: "${MOODLE_URL:?MOODLE_URL is required}"
: "${DB_HOST:?DB_HOST is required}"
: "${DB_NAME:?DB_NAME is required}"
: "${DB_USER:?DB_USER is required}"
# DB_PASS may be empty (passwordless local dev DBs).
DB_PASS=${DB_PASS:-}

DB_TYPE_RAW=${DB_TYPE:-mariadb}
case "$DB_TYPE_RAW" in
    mariadb)
        # Use Moodle's MariaDB-tuned driver class; the moodle docs explicitly
        # recommend dbtype=mariadb (not mysqli) when the server is MariaDB.
        MOODLE_DBTYPE=mariadb
        DB_PROBE=mysql
        DB_PORT_DEFAULT=3306
        ;;
    mysql)
        MOODLE_DBTYPE=mysqli
        DB_PROBE=mysql
        DB_PORT_DEFAULT=3306
        ;;
    pgsql|postgres|postgresql)
        MOODLE_DBTYPE=pgsql
        DB_PROBE=pgsql
        DB_PORT_DEFAULT=5432
        ;;
    *)
        die "unsupported DB_TYPE='$DB_TYPE_RAW' (expected mariadb|mysql|pgsql)"
        ;;
esac
DB_PORT=${DB_PORT:-$DB_PORT_DEFAULT}
DB_PREFIX=${DB_PREFIX:-mdl_}
MOODLE_LANG=${MOODLE_LANG:-en}
DATAROOT_CHMOD=${MOODLE_DATAROOT_CHMOD:-2777}

SITE_FULLNAME=${SITE_FULLNAME:-Moodle}
SITE_SHORTNAME=${SITE_SHORTNAME:-moodle}
ADMIN_USER=${ADMIN_USER:-admin}

# ---------------------------------------------------------------------------
# 2. Ensure /data tree exists.
# ---------------------------------------------------------------------------
mkdir -p "$CONFIG_DIR" "$DATAROOT"
# Moodle's default install.php --chmod is 2777; this matches the tree it would
# create itself, but pre-creating lets us own the parent permission bits.
chmod "$DATAROOT_CHMOD" "$DATAROOT" 2>/dev/null || \
    log "WARN: chmod $DATAROOT_CHMOD $DATAROOT failed (continuing)"

# ---------------------------------------------------------------------------
# 3. Wait for DB — server-up *and* credentials valid against the target DB.
#    A bare `mysqladmin ping` / `pg_isready` returns success before
#    docker-compose's MARIADB_USER provisioning finishes, racing install.php
#    into the wrong error. Probe with a real `SELECT 1` so the wait covers
#    both server-up and grants-in-place.
#    60s deadline — first-boot MariaDB initialization is slower than
#    warm-restart Postgres.
# ---------------------------------------------------------------------------
log "waiting for $DB_PROBE at $DB_HOST:$DB_PORT/$DB_NAME as $DB_USER (60s deadline)"
deadline=$(( $(date +%s) + 60 ))
while :; do
    case "$DB_PROBE" in
        pgsql)
            if PGPASSWORD="$DB_PASS" psql \
                    -h "$DB_HOST" -p "$DB_PORT" \
                    -U "$DB_USER" -d "$DB_NAME" \
                    -tAc 'SELECT 1' >/dev/null 2>&1; then
                break
            fi
            ;;
        mysql)
            if MYSQL_PWD="$DB_PASS" mysql \
                    -h "$DB_HOST" -P "$DB_PORT" \
                    -u "$DB_USER" -D "$DB_NAME" \
                    -N -B -e 'SELECT 1' >/dev/null 2>&1; then
                break
            fi
            ;;
    esac
    if [ "$(date +%s)" -ge "$deadline" ]; then
        die "DB at $DB_HOST:$DB_PORT/$DB_NAME not reachable as $DB_USER within 60s"
    fi
    sleep 1
done
log "DB is reachable and credentials valid"

# ---------------------------------------------------------------------------
# 4. First-boot install.
# ---------------------------------------------------------------------------
if [ ! -s "$CONFIG_FILE" ]; then
    : "${ADMIN_PASS:?ADMIN_PASS is required for first-boot install}"
    : "${ADMIN_EMAIL:?ADMIN_EMAIL is required for first-boot install}"
    SUPPORT_EMAIL=${SUPPORT_EMAIL:-$ADMIN_EMAIL}

    log "running admin/cli/install.php (dbtype=$MOODLE_DBTYPE, dbhost=$DB_HOST)"
    if ! php "$APP_DIR/admin/cli/install.php" \
            --non-interactive --agree-license --allow-unstable \
            --lang="$MOODLE_LANG" \
            --wwwroot="$MOODLE_URL" \
            --dataroot="$DATAROOT" \
            --dbtype="$MOODLE_DBTYPE" \
            --dbhost="$DB_HOST" --dbport="$DB_PORT" \
            --dbname="$DB_NAME" --dbuser="$DB_USER" --dbpass="$DB_PASS" \
            --prefix="$DB_PREFIX" \
            --fullname="$SITE_FULLNAME" --shortname="$SITE_SHORTNAME" \
            --adminuser="$ADMIN_USER" --adminpass="$ADMIN_PASS" \
            --adminemail="$ADMIN_EMAIL" \
            --supportemail="$SUPPORT_EMAIL" >&2; then
        die "admin/cli/install.php failed"
    fi
    log "install.php complete"
else
    log "existing $CONFIG_FILE found; skipping install"
fi

# ---------------------------------------------------------------------------
# 5. Managed-block patch — idempotent, every boot.
#    Strip any prior block, then insert a fresh one immediately before the
#    `require_once(.../lib/setup.php)` line so its assignments override any
#    install-time defaults written above.
# ---------------------------------------------------------------------------
phpquote() {
    # Escape a string for safe inclusion inside a PHP single-quoted literal.
    # Single-quoted PHP literals only need to escape ' and \.
    printf '%s' "$1" | sed -e "s/\\\\/\\\\\\\\/g" -e "s/'/\\\\'/g"
}

is_int() {
    # Pure decimal integer per `^-?[0-9]+$`. POSIX `case` patterns can't
    # express that compactly, so peel off an optional leading `-` and verify
    # the remainder is non-empty and digits-only.
    s=${1#-}
    [ -n "$s" ] || return 1
    case "$s" in
        *[!0-9]*) return 1 ;;
        *)        return 0 ;;
    esac
}

emit_cfg_line() {
    # $1 = key (lowercase, already whitelisted), $2 = raw value.
    key=$1; val=$2
    case "$val" in
        true|TRUE|True)    printf '$CFG->%s = true;\n'  "$key"; return ;;
        false|FALSE|False) printf '$CFG->%s = false;\n' "$key"; return ;;
        NULL|Null)         printf '$CFG->%s = null;\n'  "$key"; return ;;
    esac
    if is_int "$val"; then
        printf '$CFG->%s = %s;\n' "$key" "$val"
    else
        printf "\$CFG->%s = '%s';\n" "$key" "$(phpquote "$val")"
    fi
}

BLOCK_TMP=$(mktemp "$CONFIG_DIR/.managed-block.XXXXXX")
{
    printf '// BEGIN moodle-image-managed — do not edit; managed by docker entrypoint\n'
    printf "\$CFG->wwwroot = '%s';\n" "$(phpquote "$MOODLE_URL")"
    printf "\$CFG->dbtype  = '%s';\n" "$(phpquote "$MOODLE_DBTYPE")"
    printf "\$CFG->dbhost  = '%s';\n" "$(phpquote "$DB_HOST")"
    printf "\$CFG->dbname  = '%s';\n" "$(phpquote "$DB_NAME")"
    printf "\$CFG->dbuser  = '%s';\n" "$(phpquote "$DB_USER")"
    printf "\$CFG->dbpass  = '%s';\n" "$(phpquote "$DB_PASS")"
    # Moodle stores the port inside the dboptions array. install.php writes
    # $CFG->dboptions = array(..., 'dbport' => '...', ...) above us, so this
    # assignment lands on an already-initialized array.
    printf "\$CFG->dboptions['dbport'] = '%s';\n" "$(phpquote "$DB_PORT")"

    # MOODLE_CFG_* passthrough. env -0 | tr is the same idiom used in
    # docker-freescout: avoids /proc/self/environ pointing at the helper.
    env -0 | tr '\0' '\n' | while IFS= read -r entry; do
        case "$entry" in
            MOODLE_CFG_*=*)
                kv=${entry#MOODLE_CFG_}
                rawkey=${kv%%=*}
                val=${kv#*=}
                # Lowercase the key (Moodle $CFG-> properties are all lowercase).
                key=$(printf '%s' "$rawkey" | tr 'A-Z' 'a-z')
                # Whitelist: must match ^[a-z][a-z0-9_]*$. Guards against
                # PHP injection via env name.
                case "$key" in
                    ''|[!a-z]*|*[!a-z0-9_]*)
                        log "skip invalid MOODLE_CFG_* key: '$rawkey'"
                        continue
                        ;;
                esac
                # Sentinels delete the key (skip emission). Lowercase `null`
                # is the delete sentinel; for a PHP null literal use NULL/Null.
                case "$val" in
                    unset|null|"") continue ;;
                esac
                emit_cfg_line "$key" "$val"
                ;;
        esac
    done

    printf '// END moodle-image-managed\n'
} > "$BLOCK_TMP"

# Splice into config.php: drop any prior managed block, insert fresh block
# immediately before the require_once(...lib/setup.php) line.
PATCHED_TMP=$(mktemp "$CONFIG_DIR/.config.php.XXXXXX")
awk -v block_file="$BLOCK_TMP" '
    BEGIN {
        in_block = 0
        inserted = 0
    }
    /\/\/ BEGIN moodle-image-managed/ { in_block = 1; next }
    /\/\/ END moodle-image-managed/   { in_block = 0; next }
    in_block { next }
    /require_once.*lib\/setup\.php/ {
        if (!inserted) {
            while ((getline line < block_file) > 0) print line
            close(block_file)
            inserted = 1
        }
        print
        next
    }
    { print }
    END {
        if (!inserted) {
            # No setup.php require found — append the block so the override
            # at least lands somewhere readable. Should not happen on a
            # config.php produced by install.php, but failing silently here
            # would be worse.
            while ((getline line < block_file) > 0) print line
            close(block_file)
        }
    }
' "$CONFIG_FILE" > "$PATCHED_TMP" || die "managed-block patch failed"

mv "$PATCHED_TMP" "$CONFIG_FILE"
rm -f "$BLOCK_TMP"

log "bootstrap complete"
