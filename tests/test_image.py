import json
import os
import subprocess

import pytest

IMAGE = os.environ["IMAGE"]


def _inspect():
    out = subprocess.run(
        ["docker", "inspect", IMAGE],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)[0]
    # Podman's `docker inspect` places Healthcheck/User at the top level
    # rather than under Config. Lift them so tests written against the
    # docker-CLI schema work under both runtimes.
    config = data.setdefault("Config", {})
    for key in ("Healthcheck", "User"):
        if not config.get(key) and data.get(key):
            config[key] = data[key]
    return data


@pytest.fixture(scope="session")
def inspect():
    return _inspect()


def _run(*args, check=False):
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint=", IMAGE, *args],
        capture_output=True, text=True, check=check,
    )


class TestImageMetadata:
    def test_required_oci_labels(self, inspect):
        labels = inspect["Config"].get("Labels") or {}
        for key in (
            "org.opencontainers.image.source",
            "org.opencontainers.image.version",
            "org.opencontainers.image.licenses",
            "org.opencontainers.image.title",
        ):
            assert labels.get(key), f"missing OCI label: {key}"

    def test_runs_as_non_root(self, inspect):
        user = inspect["Config"].get("User", "")
        assert user in ("www-data", "82"), f"expected www-data/82, got {user!r}"

    def test_healthcheck_defined(self, inspect):
        assert inspect["Config"].get("Healthcheck"), "no Healthcheck defined"

    def test_exposes_8080(self, inspect):
        ports = inspect["Config"].get("ExposedPorts") or {}
        assert "8080/tcp" in ports, f"8080/tcp not exposed; got {list(ports)}"

    def test_image_size_under_limit(self, inspect):
        size_mb = inspect["Size"] / (1024 * 1024)
        assert size_mb < 2200, f"image size {size_mb:.0f} MB exceeds 2200 MB guardrail"

    def test_default_env_present(self, inspect):
        env = dict(e.split("=", 1) for e in inspect["Config"].get("Env") or [])
        assert env.get("AUTORUN_ENABLED") == "false"
        assert env.get("SSL_MODE") == "off"
        assert env.get("ENABLE_MOODLE_CRON") == "TRUE"
        assert env.get("ENABLE_MOODLE_ADHOC") == "TRUE"
        assert env.get("ENABLE_PLUGIN_SYNC") == "TRUE"
        assert env.get("PHP_OPCACHE_ENABLE") == "1"
        assert env.get("PHP_MAX_INPUT_VARS") == "5000"
        assert env.get("APP_BASE_DIR") == "/var/www/html"


class TestImageFilesystem:
    def test_config_symlink(self):
        r = _run("readlink", "/var/www/html/config.php")
        assert r.returncode == 0, f"readlink failed: {r.stderr}"
        assert r.stdout.strip() == "/data/config/config.php", (
            f"config.php -> {r.stdout.strip()!r}, expected /data/config/config.php"
        )

    def test_data_dir_owned_by_www_data(self):
        r = _run("stat", "-c", "%U:%G", "/data")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "www-data:www-data"

    @pytest.mark.parametrize("binary", [
        "php", "nginx", "pg_isready", "mysqladmin", "mysql", "psql",
        "curl", "git", "awk",
    ])
    def test_runtime_binaries_present(self, binary):
        r = _run("which", binary)
        assert r.returncode == 0, f"{binary} not found on PATH"
        assert r.stdout.strip(), f"which {binary} returned empty"

    @pytest.mark.parametrize("ext", [
        "mysqli", "pgsql", "pdo_mysql", "pdo_pgsql", "gd", "intl", "soap",
        "exif", "bcmath", "redis", "sodium", "zip", "Zend OPcache",
    ])
    def test_php_extensions_loaded(self, ext):
        r = _run("php", "-m")
        assert r.returncode == 0, r.stderr
        modules = {line.strip() for line in r.stdout.splitlines() if line.strip()}
        assert ext in modules, f"PHP module {ext!r} not loaded; got {sorted(modules)}"

    def test_s6_cron_run_executable(self):
        r = _run("test", "-x", "/etc/s6-overlay/s6-rc.d/moodle-cron/run")
        assert r.returncode == 0, "moodle-cron run script missing or not executable"

    def test_s6_cron_disables_internal_keepalive(self):
        r = _run(
            "grep", "-F",
            "php admin/cli/cron.php --keep-alive=0",
            "/etc/s6-overlay/s6-rc.d/moodle-cron/run",
        )
        assert r.returncode == 0, (
            "moodle-cron should disable Moodle's internal keep-alive so the "
            "wrapper timeout does not SIGTERM normal cron runs"
        )

    def test_s6_adhoc_run_executable(self):
        r = _run("test", "-x", "/etc/s6-overlay/s6-rc.d/moodle-adhoc/run")
        assert r.returncode == 0, "moodle-adhoc run script missing or not executable"

    def test_s6_cron_depends_on_bootstrap(self):
        r = _run(
            "test", "-f",
            "/etc/s6-overlay/s6-rc.d/moodle-cron/dependencies.d/20-moodle-bootstrap",
        )
        assert r.returncode == 0, "moodle-cron dependency marker on bootstrap missing"

    def test_s6_adhoc_depends_on_bootstrap(self):
        r = _run(
            "test", "-f",
            "/etc/s6-overlay/s6-rc.d/moodle-adhoc/dependencies.d/20-moodle-bootstrap",
        )
        assert r.returncode == 0, "moodle-adhoc dependency marker on bootstrap missing"

    @pytest.mark.parametrize("service", ["moodle-cron", "moodle-adhoc", "moodle-pluginsync"])
    def test_s6_user_contents(self, service):
        r = _run("test", "-e", f"/etc/s6-overlay/s6-rc.d/user/contents.d/{service}")
        assert r.returncode == 0, (
            f"{service} not registered under user/contents.d "
            f"(stdout={r.stdout!r}, stderr={r.stderr!r})"
        )

    def test_plugin_sync_lib_executable(self):
        r = _run("test", "-x", "/usr/local/lib/moodle/plugin-sync.sh")
        assert r.returncode == 0, "plugin-sync.sh missing or not executable"

    def test_s6_pluginsync_run_executable(self):
        r = _run("test", "-x", "/etc/s6-overlay/s6-rc.d/moodle-pluginsync/run")
        assert r.returncode == 0, "moodle-pluginsync run script missing or not executable"

    def test_s6_pluginsync_is_longrun(self):
        r = _run("cat", "/etc/s6-overlay/s6-rc.d/moodle-pluginsync/type")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "longrun"

    def test_s6_pluginsync_depends_on_bootstrap(self):
        r = _run(
            "test", "-f",
            "/etc/s6-overlay/s6-rc.d/moodle-pluginsync/dependencies.d/20-moodle-bootstrap",
        )
        assert r.returncode == 0, "moodle-pluginsync dependency marker on bootstrap missing"

    def test_core_plugin_manifest_present(self):
        r = _run("test", "-s", "/var/www/html/.moodle-core-plugins.manifest")
        assert r.returncode == 0, "core-plugins manifest missing or empty"

    @pytest.mark.parametrize("entry", [
        "mod/quiz",
        "question/type/multichoice",
        # A nested subplugin — proves the version.php walk captures nesting
        # the components.json walk would miss.
        "mod/assign/submission/file",
    ])
    def test_core_plugin_manifest_contains(self, entry):
        r = _run("grep", "-Fxq", entry, "/var/www/html/.moodle-core-plugins.manifest")
        assert r.returncode == 0, f"{entry!r} not in core-plugins manifest"

    def test_s6_bootstrap_oneshot_installed(self):
        # The base image's docker-php-serversideup-s6-init moves our
        # /etc/entrypoint.d/20-moodle-bootstrap.sh into /etc/s6-overlay/scripts/
        # with a rename suffix we don't want to couple to — glob is deliberate.
        r = _run("sh", "-c", "ls /etc/s6-overlay/scripts/ | grep -E 'moodle-bootstrap'")
        assert r.returncode == 0, (
            "no moodle-bootstrap script in /etc/s6-overlay/scripts/ "
            f"(stdout={r.stdout!r}, stderr={r.stderr!r})"
        )

    def test_nginx_moodle_conf_present(self):
        r = _run("test", "-f", "/etc/nginx/server-opts.d/01-moodle.conf")
        assert r.returncode == 0, "nginx moodle conf missing"

    @pytest.mark.parametrize("path", [
        "/var/www/html/admin/cli/install.php",
        "/var/www/html/lib/setup.php",
        # Moodle 5.x moved the webroot's version.php and index.php under public/.
        "/var/www/html/public/version.php",
    ])
    def test_moodle_app_present(self, path):
        r = _run("test", "-f", path)
        assert r.returncode == 0, f"{path} missing"


# Marked runtime: a full image rebuild (~30s with buildkit cache, minutes
# cold). Lives outside TestImageFilesystem so `-m 'not runtime'` keeps the
# fast image lane fast.
@pytest.mark.runtime
class TestCustomUidRebuild:
    """Rebuild with --build-arg WWW_DATA_UID/GID and verify the new UID
    actually owns /data. Regression guard: set-file-permissions only touches
    a hardcoded path list, so /data needs an explicit chown in the Dockerfile
    or the rebuilt image's www-data can't write to its own volume.
    """

    UID = "1000"
    GID = "1000"

    @pytest.fixture(scope="class")
    def image(self):
        ctx = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tag = f"moodle-uid{self.UID}-test"
        r = subprocess.run(
            ["docker", "build",
             "--build-arg", f"WWW_DATA_UID={self.UID}",
             "--build-arg", f"WWW_DATA_GID={self.GID}",
             "-t", tag, ctx],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            pytest.fail(
                f"docker build failed (rc={r.returncode})\n"
                f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
            )
        try:
            yield tag
        finally:
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)

    def test_www_data_user_remapped(self, image):
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint=", image,
             "id", "-u", "www-data"],
            capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == self.UID

    def test_data_dir_remapped(self, image):
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint=", image,
             "stat", "-c", "%u:%g", "/data"],
            capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == f"{self.UID}:{self.GID}"
