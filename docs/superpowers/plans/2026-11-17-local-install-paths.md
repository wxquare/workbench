# Local Install Paths Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make clone-then-install Workbench usable without manually exporting storage paths, while retaining explicit database initialization and separate startup.

**Architecture:** Keep the existing three entry points. A small sourced shell helper resolves the effective persistent directory and database URL for initialization and startup; installation records an optional local directory choice without touching the database. Existing Agent configuration continues to consume `env://WORKBENCH_DATABASE_URL`.

**Tech Stack:** Bash, Python `unittest`, existing Python 3.12 Agent settings, Markdown.

## Global Constraints

- Source stays wherever the private repository was cloned. No remote installer or global executable.
- Default data directory: `$HOME/.workbench`; optional `install.sh --data-dir /absolute/path`.
- Store the chosen path as plain text in Git-ignored `config/local/data-dir`; do not shell-evaluate it or store secrets.
- Explicit `WORKBENCH_PERSISTENT_ROOT` and `WORKBENCH_DATABASE_URL` override local defaults for that invocation.
- Installation never creates or migrates a database; initialization refuses existing database files and symlinks; startup runs doctor but never migrates.
- Keep Docker Compose, real secrets, existing databases and unrelated code unchanged.

---

### Task 1: Persist an install-time data directory choice

**Files:**
- Create: `scripts/lib/local-paths.sh`
- Modify: `scripts/install.sh`, `.gitignore`
- Test: `scripts/tests/test_local_workflow.py`

**Interfaces:**
- Produces `workbench_data_dir ROOT` to print an absolute data directory, checking environment override, local file, then `$HOME/.workbench`.
- Produces `workbench_export_storage ROOT` to export `WORKBENCH_PERSISTENT_ROOT` and default `WORKBENCH_DATABASE_URL` for Task 2.
- `install.sh [--data-dir ABSOLUTE_PATH]` writes `config/local/data-dir` only when absent or explicitly overridden.

- [x] **Step 1: Write failing installer and helper tests.** Set `HOME` to a temporary directory in the test environment. Make `_script()` also copy `scripts/lib/local-paths.sh` into the temporary fixture. Test default path, a path containing a space, repeat installation retaining the previous path, explicit change to another absolute path, and rejection of relative/empty paths and a symlinked path file. Check the existing config and `uv`/`npm` call sequence remain unchanged.

```python
def test_install_uses_default_and_preserves_choice(self):
    self.assertEqual(self._run("install.sh").returncode, 0)
    chosen = self.root / "config/local/data-dir"
    self.assertEqual(chosen.read_text().strip(), str(self.root / "home/.workbench"))
    self.assertEqual(self._run("install.sh", "--data-dir", str(self.root / "chosen dir")).returncode, 0)
    self.assertEqual(self._run("install.sh").returncode, 0)
    self.assertEqual(chosen.read_text().strip(), str(self.root / "chosen dir"))
```

- [x] **Step 2: Run red tests.** `python3 -m unittest scripts.tests.test_local_workflow -v`; expect installer argument/default-path assertions to fail.
- [x] **Step 3: Implement the helper and installer.** Use `[[ $path == /* ]]` for absolute validation, reject newlines, and read the path file with `IFS= read -r`. Never `source` the data file. Have `install.sh` parse only `--data-dir PATH` and `--help`; when no explicit option, keep an existing valid choice or write `$HOME/.workbench`. Refuse a symlinked choice file; write a changed choice through a temporary file plus `mv`, and warn that data is not moved. Add `config/local/data-dir` to `.gitignore`.

```bash
workbench_export_storage() {
  local data_dir
  data_dir=$(workbench_data_dir "$1") || return
  export WORKBENCH_PERSISTENT_ROOT="$data_dir"
  export WORKBENCH_DATABASE_URL="${WORKBENCH_DATABASE_URL:-sqlite+aiosqlite:///$data_dir/agents.sqlite3}"
}
```

- [x] **Step 4: Run green tests and syntax checks.** `python3 -m unittest scripts.tests.test_local_workflow -v` and `bash -n scripts/install.sh scripts/lib/local-paths.sh`.
- [x] **Step 5: Commit.** Stage only Task 1 files and commit `feat: remember local workbench data directory`.

### Task 2: Resolve storage paths for explicit init and ordinary startup

**Files:**
- Modify: `scripts/init-db.sh`, `scripts/start.sh`, `scripts/lib/local-paths.sh`
- Test: `scripts/tests/test_local_workflow.py`

**Interfaces:**
- Consumes `workbench_export_storage ROOT` from Task 1 before loading settings or running doctor.
- Existing `WORKBENCH_DATABASE_URL` is preserved; unset URL derives `sqlite+aiosqlite:///$WORKBENCH_PERSISTENT_ROOT/agents.sqlite3`.

- [x] **Step 1: Write failing path-precedence and safety tests.** With both storage env vars removed, verify init invokes migrate/bootstrap/doctor with the saved directory and derived URL; verify start reaches doctor with the same values. Log those two env vars from the fake Agent executable. Test custom `WORKBENCH_PERSISTENT_ROOT` with no URL, both vars explicitly set, invalid local path, pre-existing default database, symlinked database, and a configured effective database outside the persistent root.

```python
env = {key: value for key, value in self.env.items()
       if key not in {"WORKBENCH_PERSISTENT_ROOT", "WORKBENCH_DATABASE_URL"}}
result = self._run("init-db.sh", env=env)
self.assertEqual(result.returncode, 0, result.stderr)
self.assertIn("agents.sqlite3", (self.root / "storage-env.log").read_text())
```

- [x] **Step 2: Run red tests.** `python3 -m unittest scripts.tests.test_local_workflow -v`; expect missing-env and safety tests to fail.
- [x] **Step 3: Integrate the helper.** Source `scripts/lib/local-paths.sh` and call `workbench_export_storage "$root"` in both scripts before Python settings loading and doctor. Preserve current effective-config path validation in `init-db.sh`; explicitly reject a symlink at the URL's original database path before `Path.resolve()` can follow it. Keep existing refusal of an existing target. Do not add migrate/bootstrap to `start.sh`.
- [x] **Step 4: Run green tests and syntax checks.** `python3 -m unittest discover -s scripts/tests -v` and `bash -n scripts/install.sh scripts/init-db.sh scripts/start.sh scripts/lib/local-paths.sh`.
- [x] **Step 5: Commit.** Stage only Task 2 files and commit `feat: use local storage defaults for init and start`.

### Task 3: Document and verify the clone-then-install workflow

**Files:**
- Modify: `README.md`, `docs/runbooks/source-layout.md`
- Test: `scripts/tests/test_local_workflow.py`

**Interfaces:**
- Documents the three commands and the `--data-dir`/environment override precedence without changing runtime interfaces.

- [x] **Step 1: Write documentation assertions.** Add a focused test reading README and the runbook and checking that each shows `install.sh`, `init-db.sh`, `start.sh`, the default `$HOME/.workbench`, and `--data-dir`.

```python
for path in (REPO_ROOT / "README.md", REPO_ROOT / "docs/runbooks/source-layout.md"):
    content = path.read_text()
    for expected in ("install.sh", "init-db.sh", "start.sh", "--data-dir", "$HOME/.workbench"):
        self.assertIn(expected, content)
```

- [x] **Step 2: Run red documentation test.** `python3 -m unittest scripts.tests.test_local_workflow -v`; expect missing default-path/flag text.
- [x] **Step 3: Update both documents.** Show `./scripts/install.sh`, optional `./scripts/install.sh --data-dir /absolute/path`, explicit `./scripts/init-db.sh` for new databases, and `./scripts/start.sh` for daily use. Explain that an existing database is never overwritten, paths are stored locally, both env vars remain overrides, and secrets must still be injected through the environment. Remove the old requirement to export both paths for ordinary local use; keep manual advanced examples labelled as overrides.
- [x] **Step 4: Run complete relevant verification.** `python3 -m unittest discover -s scripts/tests -v`, `bash -n scripts/install.sh scripts/init-db.sh scripts/start.sh scripts/lib/local-paths.sh`, and `git diff --check`. Do not run real installation, initialization or network-dependent builds against user data.
- [x] **Step 5: Commit.** Stage only documentation and Task 3 test changes and commit `docs: explain default local storage workflow`.
