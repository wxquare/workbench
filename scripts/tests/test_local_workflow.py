import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]


class LocalWorkflowScriptsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in (
            "scripts", "bin", "apps/agent/.venv/bin", "apps/mcp-server/.venv/bin",
            "apps/web", "config/local", "var",
        ):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.agent_config = self.root / "config/local/agent.yaml"
        self.agent_config.write_text("storage:\n  database_url_ref: env://WORKBENCH_DATABASE_URL\n")
        (self.root / "config/local/agent.example.yaml").write_text(self.agent_config.read_text())
        (self.root / "config/local/mcp-server.example.yaml").write_text("example mcp\n")
        (self.root / "config/local/mcp-server.yaml").write_text("custom mcp\n")
        self.calls = self.root / "calls.log"
        self.pids = self.root / "pids.log"
        self.storage_env_log = self.root / "storage-env.log"
        self.db = self.root / "var/agents.sqlite3"
        self.env = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
            "CALL_LOG": str(self.calls),
            "PID_LOG": str(self.pids),
            "STORAGE_ENV_LOG": str(self.storage_env_log),
            "WORKBENCH_PERSISTENT_ROOT": str(self.root / "var"),
            "WORKBENCH_DATABASE_URL": f"sqlite+aiosqlite:///{self.db}",
        }
        self._executable(
            "apps/agent/.venv/bin/python",
            f'#!/bin/sh\nexec {REPO_ROOT / "apps/agent/.venv/bin/python"} "$@"\n',
        )
        self._executable("apps/agent/.venv/bin/workbench", """#!/usr/bin/env python3
import json
import os
import signal
import sys
import time

with open(os.environ["CALL_LOG"], "a") as log:
    log.write("workbench:" + " ".join(sys.argv[1:]) + "\\n")
with open(os.environ["STORAGE_ENV_LOG"], "a") as log:
    log.write(json.dumps({
        "command": sys.argv[1],
        "root": os.environ.get("WORKBENCH_PERSISTENT_ROOT"),
        "url": os.environ.get("WORKBENCH_DATABASE_URL"),
    }) + "\\n")
if sys.argv[1] == "doctor" and os.environ.get("DOCTOR_FAIL") == "1":
    sys.exit(1)
if sys.argv[1] in ("api", "worker", "scheduler"):
    with open(os.environ["PID_LOG"], "a") as pids:
        pids.write(str(os.getpid()) + "\\n")
    if os.environ.get("IGNORE_TERM") == sys.argv[1]:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(0.1)
""")
        self._executable("apps/mcp-server/.venv/bin/workbench-mcp-server", """#!/usr/bin/env python3
import os
import signal
import sys
import time

with open(os.environ["CALL_LOG"], "a") as log:
    log.write("mcp:" + " ".join(sys.argv[1:]) + "\\n")
if sys.argv[1] == "serve":
    with open(os.environ["PID_LOG"], "a") as pids:
        pids.write(str(os.getpid()) + "\\n")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(0.1)
""")
        self._executable("bin/uv", '#!/bin/sh\nprintf "uv:%s:%s\\n" "$(basename "$PWD")" "$*" >> "$CALL_LOG"\n')
        self._executable("bin/npm", '#!/bin/sh\nprintf "npm:%s:%s\\n" "$(basename "$PWD")" "$*" >> "$CALL_LOG"\n')

    def _executable(self, name, content):
        path = self.root / name
        path.write_text(content)
        path.chmod(0o755)

    def _script(self, name):
        source = REPO_ROOT / "scripts" / name
        self.assertTrue(source.is_file(), f"missing {name}")
        target = self.root / "scripts" / name
        shutil.copy2(source, target)
        helper = REPO_ROOT / "scripts/lib/local-paths.sh"
        if helper.is_file():
            (self.root / "scripts/lib").mkdir(exist_ok=True)
            shutil.copy2(helper, self.root / "scripts/lib/local-paths.sh")
        return target

    def _run(self, name, *arguments, env=None):
        return subprocess.run(
            ["bash", str(self._script(name)), *arguments], cwd=self.root,
            env=env or self.env, capture_output=True, text=True, timeout=10,
        )

    def _storage_env(self):
        return [json.loads(line) for line in self.storage_env_log.read_text().splitlines()]

    def _without_storage_env(self):
        return {key: value for key, value in self.env.items()
                if key not in {"WORKBENCH_PERSISTENT_ROOT", "WORKBENCH_DATABASE_URL"}}

    def test_install_preserves_local_config_and_builds_components(self):
        (self.root / "config/local/mcp-server.yaml").unlink()
        result = self._run("install.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.agent_config.read_text(), "storage:\n  database_url_ref: env://WORKBENCH_DATABASE_URL\n")
        self.assertEqual((self.root / "config/local/mcp-server.yaml").read_text(), "example mcp\n")
        self.assertEqual(self.calls.read_text().splitlines(), [
            "uv:agent:sync --frozen --group dev", "npm:web:ci",
            "npm:web:run build", "uv:mcp-server:sync --frozen --group dev",
        ])

    def test_install_uses_default_and_preserves_custom_data_dir(self):
        choice = self.root / "config/local/data-dir"

        result = self._run("install.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(choice.is_file(), "install must record the data directory")
        self.assertEqual(choice.read_text().strip(), str(self.root / "home/.workbench"))

        custom = self.root / "chosen dir"
        result = self._run("install.sh", "--data-dir", str(custom))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(choice.read_text().strip(), str(custom))

        result = self._run("install.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(choice.read_text().strip(), str(custom))
        self.assertFalse(custom.exists(), "install must not create persistent data")

    def test_install_rejects_invalid_data_dir_without_running_tools(self):
        for path in ("relative/path", ""):
            with self.subTest(path=path):
                result = self._run("install.sh", "--data-dir", path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("absolute path", result.stderr)
                self.assertFalse((self.root / "config/local/data-dir").exists())
        self.assertFalse(self.calls.exists())

    def test_install_rejects_symlinked_data_dir_file(self):
        target = self.root / "var/choice"
        target.write_text("original\n")
        (self.root / "config/local/data-dir").symlink_to(target)

        result = self._run("install.sh", "--data-dir", str(self.root / "new data"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(target.read_text(), "original\n")
        self.assertFalse(self.calls.exists())

    def test_init_db_refuses_existing_file_without_migration(self):
        self.db.write_text("existing data")
        result = self._run("init-db.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.db.read_text(), "existing data")
        self.assertFalse(self.calls.exists())

    def test_init_db_checks_effective_config_database(self):
        existing = self.root / "var/existing.sqlite3"
        existing.write_text("existing data")
        self.agent_config.write_text("storage:\n  database_url_ref: env://OTHER_DATABASE_URL\n")
        env = {**self.env, "OTHER_DATABASE_URL": f"sqlite+aiosqlite:///{existing}"}

        result = self._run("init-db.sh", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(existing.read_text(), "existing data")
        self.assertFalse(self.calls.exists())

    def test_init_db_rejects_database_outside_persistent_root(self):
        outside = self.root / "outside.sqlite3"
        env = {**self.env, "WORKBENCH_DATABASE_URL": f"sqlite+aiosqlite:///{outside}"}

        result = self._run("init-db.sh", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(outside.exists())
        self.assertFalse(self.calls.exists())

    def test_init_db_runs_explicit_sequence(self):
        result = self._run("init-db.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([line.split(" --config")[0] for line in self.calls.read_text().splitlines()], [
            "workbench:db migrate", "workbench:bootstrap", "workbench:doctor",
        ])

    def test_init_db_uses_saved_data_dir_without_storage_env(self):
        custom = self.root / "chosen dir"
        self.assertEqual(self._run("install.sh", "--data-dir", str(custom)).returncode, 0)
        self.calls.unlink()

        result = self._run("init-db.sh", env=self._without_storage_env())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([entry["command"] for entry in self._storage_env()], ["db", "bootstrap", "doctor"])
        for entry in self._storage_env():
            self.assertEqual(entry["root"], str(custom))
            self.assertEqual(entry["url"], f"sqlite+aiosqlite:///{custom}/agents.sqlite3")

    def test_init_db_derives_url_from_explicit_root(self):
        custom = self.root / "env data"
        env = {**self._without_storage_env(), "WORKBENCH_PERSISTENT_ROOT": str(custom)}

        result = self._run("init-db.sh", env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._storage_env()[0]["url"], f"sqlite+aiosqlite:///{custom}/agents.sqlite3")

    def test_init_db_rejects_invalid_saved_data_dir(self):
        (self.root / "config/local/data-dir").write_text("relative/path\n")

        result = self._run("init-db.sh", env=self._without_storage_env())

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("absolute path", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_init_db_refuses_existing_default_database(self):
        data_dir = self.root / "home/.workbench"
        data_dir.mkdir(parents=True)
        database = data_dir / "agents.sqlite3"
        database.write_text("existing")

        result = self._run("init-db.sh", env=self._without_storage_env())

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(database.read_text(), "existing")
        self.assertFalse(self.calls.exists())

    def test_init_db_rejects_dangling_database_symlink(self):
        data_dir = self.root / "home/.workbench"
        data_dir.mkdir(parents=True)
        (data_dir / "agents.sqlite3").symlink_to(data_dir / "missing.sqlite3")

        result = self._run("init-db.sh", env=self._without_storage_env())

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        self.assertFalse((data_dir / "missing.sqlite3").exists())
        self.assertFalse(self.calls.exists())

    def test_start_requires_passing_doctor(self):
        result = self._run("start.sh", env={**self.env, "DOCTOR_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertFalse(self.pids.exists())

    def test_start_uses_saved_data_dir_without_storage_env(self):
        custom = self.root / "saved data"
        (self.root / "config/local/data-dir").write_text(f"{custom}\n")
        env = {**self._without_storage_env(), "DOCTOR_FAIL": "1"}

        result = self._run("start.sh", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.storage_env_log.is_file(), result.stderr)
        self.assertEqual(self._storage_env(), [{
            "command": "doctor",
            "root": str(custom),
            "url": f"sqlite+aiosqlite:///{custom}/agents.sqlite3",
        }])
        self.assertFalse(self.pids.exists())

    def test_start_explicit_storage_env_overrides_saved_choice(self):
        (self.root / "config/local/data-dir").write_text(f"{self.root / 'saved'}\n")
        env = {**self.env, "DOCTOR_FAIL": "1"}

        result = self._run("start.sh", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._storage_env(), [{
            "command": "doctor",
            "root": str(self.root / "var"),
            "url": f"sqlite+aiosqlite:///{self.db}",
        }])

    def test_start_manages_agent_and_optional_mcp_processes(self):
        process = subprocess.Popen(
            ["bash", str(self._script("start.sh")), "--mcp"], cwd=self.root,
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.pids.exists() and len(self.pids.read_text().splitlines()) == 4:
                break
            if process.poll() is not None:
                break
            time.sleep(0.05)
        self.assertIsNone(process.poll(), "start script exited before services launched")
        self.assertEqual(len(self.pids.read_text().splitlines()), 4)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143)
        for pid in map(int, self.pids.read_text().splitlines()):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        calls = self.calls.read_text().splitlines()
        self.assertTrue(any(line.startswith("mcp:check ") for line in calls))
        self.assertTrue(any(line.startswith("mcp:serve ") for line in calls))

    def test_start_force_stops_child_ignoring_term(self):
        process = subprocess.Popen(
            ["bash", str(self._script("start.sh"))], cwd=self.root,
            env={**self.env, "IGNORE_TERM": "worker"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.pids.exists() and len(self.pids.read_text().splitlines()) == 3:
                    break
                time.sleep(0.05)
            self.assertEqual(len(self.pids.read_text().splitlines()), 3)
            process.send_signal(signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.fail("start did not stop an unresponsive child within five seconds")
            self.assertEqual(process.returncode, 143)
            for pid in map(int, self.pids.read_text().splitlines()):
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
        finally:
            if self.pids.exists():
                for pid in map(int, self.pids.read_text().splitlines()):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
