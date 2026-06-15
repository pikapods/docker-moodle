import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.runtime

IMAGE = os.environ["IMAGE"]
READY_DEADLINE_S = 360
HEALTHY_DEADLINE_S = 240

# Moodle issues a 303 redirect to $CFG->wwwroot whenever the request Host
# header does not match. The fixture sets MOODLE_URL=http://localhost:8080,
# so every test request must declare Host: localhost:8080 — the actual host
# port comes from `-P` and is random, only the TCP destination.
APP_HOST_HEADER = "localhost:8080"


def _sh(*args, check=True, capture=True):
    return subprocess.run(
        list(args),
        capture_output=capture, text=True, check=check,
    )


def _exec(container, *args, check=False):
    return subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, check=check,
    )


def _wait_mariadb_ready(container, deadline_s=60):
    end = time.time() + deadline_s
    while time.time() < end:
        # mariadb:11 ships `mariadb-admin`; `mysqladmin` is also a symlink.
        # Use `mariadb-admin ping` to be explicit about the new tool name.
        r = _exec(container, "mariadb-admin", "ping", "--silent")
        if r.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"mariadb container {container} not ready within {deadline_s}s")


def _http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"Host": APP_HOST_HEADER})
    return urllib.request.urlopen(req, timeout=timeout)


def _wait_http_200(url, deadline_s):
    end = time.time() + deadline_s
    last_err = None
    while time.time() < end:
        try:
            with _http_get(url, timeout=5) as r:
                if r.status == 200:
                    return
                last_err = f"status={r.status}"
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = repr(e)
        time.sleep(2)
    raise RuntimeError(f"{url} did not return 200 within {deadline_s}s (last={last_err})")


def _wait_managed_block(container, deadline_s=180):
    # install.php writes a valid config.php and exits; nginx+php-fpm are not
    # gated on the bootstrap oneshot, so `/login/index.php` can return 200
    # (a redirect to install.php) while first-boot install is still creating
    # the schema and the managed-block patch has not run yet. The deadline
    # matches the Dockerfile HEALTHCHECK start-period=180s — the project's own
    # stated headroom for a cold install — so a slow runner doesn't flake.
    end = time.time() + deadline_s
    while time.time() < end:
        r = _exec(
            container, "sh", "-c",
            "grep -q 'BEGIN moodle-image-managed' /data/config/config.php",
        )
        if r.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(
        f"managed block did not appear in config.php within {deadline_s}s"
    )


def _host_port(container, container_port):
    r = _sh("docker", "port", container, container_port)
    # Output like "0.0.0.0:32768\n[::]:32768\n" — take first line.
    line = r.stdout.splitlines()[0]
    return int(line.rsplit(":", 1)[1])


def _app_run_args(name, net, mdb, *, volume=None, prefix=None, extra_env=None):
    """Build the `docker run` argv for a Moodle app container against `mdb`.

    `-P` publishes EXPOSE'd ports on auto-assigned host ports. Real docker also
    accepts `-p 0:8080`, but podman rejects that form ("port numbers must be
    between 1 and 65535"), and `-P` works on both. _host_port() resolves the
    actual host port.
    """
    args = ["docker", "run", "-d", "--name", name, "--network", net]
    if volume:
        args += ["-v", f"{volume}:/data"]
    env = {
        "MOODLE_URL": "http://localhost:8080",
        "DB_TYPE": "mariadb",
        "DB_HOST": mdb,
        "DB_NAME": "moodle",
        "DB_USER": "moodle",
        "DB_PASS": "changeme",
        "ADMIN_USER": "admin",
        "ADMIN_PASS": "changeme",
        "ADMIN_EMAIL": "admin@smoke.local",
    }
    if prefix:
        env["DB_PREFIX"] = prefix
    if extra_env:
        env.update(extra_env)
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    args += ["-P", IMAGE]
    return args


@pytest.fixture(scope="session")
def stack():
    suffix = secrets.token_hex(4)
    net = f"moodle-net-{suffix}"
    mdb = f"mdb-{suffix}"
    moodle = f"moodle-{suffix}"

    _sh("docker", "network", "create", net)
    try:
        _sh(
            "docker", "run", "-d", "--name", mdb, "--network", net,
            "-e", "MARIADB_DATABASE=moodle",
            "-e", "MARIADB_USER=moodle",
            "-e", "MARIADB_PASSWORD=changeme",
            "-e", "MARIADB_RANDOM_ROOT_PASSWORD=yes",
            "mariadb:11",
            "--character-set-server=utf8mb4",
            "--collation-server=utf8mb4_unicode_ci",
            "--innodb-file-per-table=ON",
            "--skip-character-set-client-handshake",
        )
        _wait_mariadb_ready(mdb)

        _sh(*_app_run_args(
            moodle, net, mdb,
            # MOODLE_CFG_* passthrough — covered by managed-block tests below.
            extra_env={
                "MOODLE_CFG_DEBUG": "32767",
                "MOODLE_CFG_DEBUGDISPLAY": "true",
                "MOODLE_CFG_NOEMAILEVER": "true",
            },
        ))
        port = _host_port(moodle, "8080")
        try:
            _wait_http_200(f"http://127.0.0.1:{port}/login/index.php", READY_DEADLINE_S)
            _wait_managed_block(moodle)
        except RuntimeError:
            for c in (moodle, mdb):
                dump = _sh("docker", "logs", c, check=False)
                print(f"--- logs {c} stdout ---\n{dump.stdout}")
                print(f"--- logs {c} stderr ---\n{dump.stderr}")
            raise

        yield {"moodle": moodle, "mdb": mdb, "net": net, "port": port}
    finally:
        for name in (moodle, mdb):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


def test_login_responds_200(stack):
    with _http_get(f"http://127.0.0.1:{stack['port']}/login/index.php") as r:
        assert r.status == 200
        body = r.read().decode("utf-8", errors="replace")
    assert 'type="password"' in body or "password" in body.lower()


def test_config_file_created(stack):
    r = _exec(stack["moodle"], "test", "-s", "/data/config/config.php")
    assert r.returncode == 0, "config.php missing or empty"


@pytest.mark.parametrize("path", [
    "/data/moodledata",
    "/data/moodledata/cache",
    "/data/moodledata/sessions",
    "/data/moodledata/temp",
])
def test_dataroot_populated(stack, path):
    r = _exec(stack["moodle"], "test", "-d", path)
    assert r.returncode == 0, f"dataroot path missing: {path}"


def test_managed_block_present_with_db_keys(stack):
    r = _exec(stack["moodle"], "cat", "/data/config/config.php")
    assert r.returncode == 0, r.stderr
    cfg = r.stdout
    assert "// BEGIN moodle-image-managed" in cfg
    assert "// END moodle-image-managed" in cfg
    for needle in (
        "$CFG->wwwroot",
        "$CFG->dbtype",
        "$CFG->dbhost",
        "$CFG->dbname",
        "$CFG->dbuser",
    ):
        assert needle in cfg, f"{needle!r} not present in managed block"


def test_moodle_cfg_passthrough_int(stack):
    r = _exec(stack["moodle"], "cat", "/data/config/config.php")
    assert r.returncode == 0, r.stderr
    # Int coercion: no quotes around the value.
    assert "$CFG->debug = 32767;" in r.stdout, (
        "expected unquoted int $CFG->debug = 32767; in config.php"
    )


def test_moodle_cfg_passthrough_bool(stack):
    r = _exec(stack["moodle"], "cat", "/data/config/config.php")
    assert r.returncode == 0, r.stderr
    assert "$CFG->debugdisplay = true;" in r.stdout
    assert "$CFG->noemailever = true;" in r.stdout


def test_managed_block_idempotent_across_restart(stack):
    moodle = stack["moodle"]
    _sh("docker", "restart", moodle)
    # `-p 0:8080` makes the host port ephemeral; Docker may reassign on
    # restart, so re-query rather than reusing the fixture's pre-restart port.
    port = _host_port(moodle, "8080")
    try:
        _wait_http_200(f"http://127.0.0.1:{port}/login/index.php", READY_DEADLINE_S)
        # After restart, bootstrap re-runs and re-applies the splice. Block
        # on a brief settle window so the grep -c below sees the final state
        # rather than racing a half-spliced file.
        time.sleep(2)
    except RuntimeError:
        dump = _sh("docker", "logs", moodle, check=False)
        print(dump.stdout)
        print(dump.stderr)
        raise
    r = _exec(
        moodle, "sh", "-c",
        "grep -c 'BEGIN moodle-image-managed' /data/config/config.php",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "1", (
        f"expected exactly one managed-block BEGIN marker, got {r.stdout.strip()!r} "
        "(awk splice doubled the block?)"
    )


def test_logs_clean(stack):
    logs = _sh("docker", "logs", stack["moodle"], check=False)
    combined = logs.stdout + logs.stderr
    bad = re.findall(r"RuntimeException|PHP Fatal|admin/cli/install\.php failed", combined)
    assert not bad, f"bad patterns in container logs: {bad[:5]}"


def test_cron_longrun_alive(stack):
    # /proc cmdline rationale mirrors freescout's test_scheduler_longrun_alive:
    # busybox `ps` on Alpine truncates shebang-launched scripts' argv, so
    # we read /proc/<pid>/cmdline directly.
    r = _exec(
        stack["moodle"], "sh", "-c",
        "cat /proc/[0-9]*/cmdline 2>/dev/null | tr '\\0' '\\n' "
        "| grep -qF moodle-cron/run",
    )
    assert r.returncode == 0, (
        "moodle-cron longrun process not present in /proc cmdlines "
        f"(stdout={r.stdout!r}, stderr={r.stderr!r})"
    )


def test_adhoc_longrun_alive(stack):
    r = _exec(
        stack["moodle"], "sh", "-c",
        "cat /proc/[0-9]*/cmdline 2>/dev/null | tr '\\0' '\\n' "
        "| grep -qF moodle-adhoc/run",
    )
    assert r.returncode == 0, (
        "moodle-adhoc longrun process not present in /proc cmdlines "
        f"(stdout={r.stdout!r}, stderr={r.stderr!r})"
    )


def test_healthcheck_reports_healthy(stack):
    end = time.time() + HEALTHY_DEADLINE_S
    last = None
    while time.time() < end:
        r = _sh("docker", "inspect", "--format", "{{json .State.Health}}", stack["moodle"])
        health = json.loads(r.stdout)
        if not health:
            pytest.skip("image has no HEALTHCHECK or daemon does not surface health")
        last = health.get("Status")
        if last == "healthy":
            return
        if last == "unhealthy":
            pytest.fail(f"container went unhealthy: {health.get('Log', [])[-1:]!r}")
        time.sleep(3)
    pytest.fail(f"healthcheck still {last!r} after {HEALTHY_DEADLINE_S}s")


# Path of the synthetic plugin, relative to public/. Mirrors the on-disk shape
# the web installer produces (a real dir containing version.php under a plugin
# type path) — capture keys on exactly this.
_FAKE_PLUGIN_REL = "ai/provider/fake"
_FAKE_PLUGIN_DIR = f"/var/www/html/public/{_FAKE_PLUGIN_REL}"
_FAKE_PLUGIN_STORE = f"/data/plugins/{_FAKE_PLUGIN_REL}"


def test_plugin_persists_across_recreate(stack):
    """End-to-end proof of the fix for issue #2: a web-UI-installed plugin
    survives container recreation. Uses its own /data volume + DB prefix so it
    is isolated from the session stack, but reuses the session's mariadb/net.
    """
    suffix = secrets.token_hex(4)
    app = f"moodle-plug-{suffix}"
    vol = f"moodle-plugvol-{suffix}"
    net, mdb = stack["net"], stack["mdb"]

    run_args = _app_run_args(
        app, net, mdb, volume=vol, prefix="plug_",
        # Tight poll so the background prune/capture loop is responsive; the
        # test triggers capture directly rather than waiting on it.
        extra_env={"PLUGIN_SYNC_INTERVAL": "5"},
    )

    _sh("docker", "volume", "create", vol)
    try:
        _sh(*run_args)
        port = _host_port(app, "8080")
        try:
            _wait_http_200(f"http://127.0.0.1:{port}/login/index.php", READY_DEADLINE_S)
        except RuntimeError:
            dump = _sh("docker", "logs", app, check=False)
            print(dump.stdout)
            print(dump.stderr)
            raise

        # Simulate a UI install: a real plugin dir with version.php, identical
        # on-disk shape to the web installer's extraction.
        r = _exec(
            app, "sh", "-c",
            f"mkdir -p {_FAKE_PLUGIN_DIR} && "
            f"printf '<?php\\n$plugin->version=2025010100;\\n"
            f"$plugin->component=\"aiprovider_fake\";\\n' "
            f"> {_FAKE_PLUGIN_DIR}/version.php",
            check=False,
        )
        assert r.returncode == 0, f"failed to plant fake plugin: {r.stderr}"

        # Trigger capture directly rather than waiting a poll interval.
        r = _exec(
            app, "sh", "-c",
            ". /usr/local/lib/moodle/plugin-sync.sh; capture_plugins",
        )
        assert r.returncode == 0, f"capture_plugins failed: {r.stderr}"

        # Captured to /data and the original replaced by a symlink.
        assert _exec(app, "test", "-f", f"{_FAKE_PLUGIN_STORE}/version.php").returncode == 0, \
            "plugin not captured into /data/plugins"
        assert _exec(app, "test", "-L", _FAKE_PLUGIN_DIR).returncode == 0, \
            "captured plugin not replaced by a symlink in the codebase"

        # A baked core plugin must NOT have been captured.
        assert _exec(app, "test", "!", "-e", "/data/plugins/mod/quiz").returncode == 0, \
            "core plugin mod/quiz was wrongly captured"

        # --- Recreate (NOT restart): destroy the container, start a fresh one
        # on the SAME /data volume and env. This is what a redeploy / image
        # upgrade does — the ephemeral codebase layer resets.
        _sh("docker", "rm", "-f", app)
        _sh(*run_args)
        port = _host_port(app, "8080")
        try:
            _wait_http_200(f"http://127.0.0.1:{port}/login/index.php", READY_DEADLINE_S)
        except RuntimeError:
            dump = _sh("docker", "logs", app, check=False)
            print(dump.stdout)
            print(dump.stderr)
            raise

        # Restore ran in bootstrap before php-fpm: the plugin is a symlink into
        # /data/plugins again, and the file resolves through it.
        r = _exec(app, "readlink", _FAKE_PLUGIN_DIR)
        assert r.returncode == 0 and r.stdout.strip() == _FAKE_PLUGIN_STORE, (
            f"after recreate, {_FAKE_PLUGIN_DIR} -> {r.stdout.strip()!r}, "
            f"expected {_FAKE_PLUGIN_STORE}"
        )
        assert _exec(app, "test", "-f", f"{_FAKE_PLUGIN_DIR}/version.php").returncode == 0, \
            "restored plugin symlink does not resolve to its version.php"
        assert _exec(app, "test", "!", "-e", "/data/plugins/mod/quiz").returncode == 0, \
            "core plugin mod/quiz was wrongly captured after recreate"
    finally:
        subprocess.run(["docker", "rm", "-f", app], capture_output=True)
        subprocess.run(["docker", "volume", "rm", vol], capture_output=True)
