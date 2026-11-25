import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "check_python_complexity.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("complexity_checker", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ComplexityGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "sample.py"
        self.checker = load_checker()

    def source(self, text):
        self.path.write_text(text, encoding="utf-8")

    def diagnostic(self, row, value=13, name="method"):
        return {
            "filename": str(self.path),
            "location": {"row": row, "column": 5},
            "code": "C901",
            "message": f"`{name}` is too complex ({value} > 12)",
        }

    def scan(self, diagnostics):
        return self.checker.identify(self.root, diagnostics)

    def test_line_moves_and_nested_names_are_stable(self):
        self.source(
            "class Service:\n    def method(self):\n        def nested():\n            pass\n        return nested\n"
        )
        first = self.scan([self.diagnostic(2), self.diagnostic(3, name="nested")])
        self.source(
            "\n\nclass Service:\n    def method(self):\n        def nested():\n            pass\n        return nested\n"
        )
        second = self.scan([self.diagnostic(4), self.diagnostic(5, name="nested")])
        self.assertEqual(first, second)
        self.assertEqual(
            {item[1] for item in first}, {"Service.method", "Service.method.nested"}
        )

    def test_new_and_worsened_fail_but_reduction_passes(self):
        self.source(
            "class Service:\n    def method(self):\n        pass\n    def added(self):\n        pass\n"
        )
        baseline = {("sample.py", "Service.method", "C901"): 15}
        self.assertFalse(
            self.checker.compare(
                self.root, baseline, self.scan([self.diagnostic(2, 14)])
            )
        )
        self.assertTrue(
            self.checker.compare(
                self.root, baseline, self.scan([self.diagnostic(2, 16)])
            )
        )
        self.assertTrue(
            self.checker.compare(
                self.root,
                baseline,
                self.scan([self.diagnostic(2, 14), self.diagnostic(4, 13, "added")]),
            )
        )

    def test_deleted_or_renamed_function_requires_explicit_mapping(self):
        self.source("class Service:\n    def renamed(self):\n        pass\n")
        baseline = {("sample.py", "Service.method", "C901"): 15}
        current = self.scan([self.diagnostic(2, 14, "renamed")])
        self.assertTrue(self.checker.compare(self.root, baseline, current))
        mapping = {("sample.py", "Service.method"): ("sample.py", "Service.renamed")}
        self.assertFalse(self.checker.compare(self.root, baseline, current, mapping))
        self.assertTrue(
            self.checker.compare(
                self.root,
                baseline,
                self.scan([self.diagnostic(2, 16, "renamed")]),
                mapping,
            )
        )

    def test_missing_or_invalid_ruff_output_fails_closed(self):
        with patch.object(self.checker.subprocess, "run") as run:
            run.return_value.returncode = 2
            run.return_value.stdout = "[]"
            with self.assertRaises(ValueError):
                self.checker.run_ruff(self.root, "ruff")
            run.return_value.returncode = 0
            run.return_value.stdout = "invalid json"
            with self.assertRaises(ValueError):
                self.checker.run_ruff(self.root, "ruff")
            run.return_value.returncode = 1
            run.return_value.stdout = "[]"
            with self.assertRaises(ValueError):
                self.checker.run_ruff(self.root, "ruff")

    def test_invalid_baseline_paths_and_duplicate_entries_fail_closed(self):
        entry = {"path": "../outside.py", "name": "f", "rule": "C901", "value": 13}
        with self.assertRaises(ValueError):
            self.checker.read_baseline({"version": 1, "entries": [entry]})
        entry["path"] = "apps/agent/src/example.py"
        with self.assertRaises(ValueError):
            self.checker.read_baseline({"version": 1, "entries": [entry, entry]})

    def test_baseline_cannot_increase_or_add_exemptions(self):
        old = {("sample.py", "Service.method", "C901"): 15}
        self.assertFalse(
            self.checker.compare_baselines(
                old, {("sample.py", "Service.method", "C901"): 14}
            )
        )
        self.assertTrue(
            self.checker.compare_baselines(
                old, {("sample.py", "Service.method", "C901"): 16}
            )
        )
        self.assertTrue(
            self.checker.compare_baselines(
                old, {**old, ("sample.py", "Service.added", "C901"): 13}
            )
        )

    def test_real_ruff_scan_respects_identity_and_excludes_history(self):
        ruff = Path(__file__).resolve().parents[2] / "apps/agent/.venv/bin/ruff"
        if not ruff.exists():
            self.skipTest("agent Ruff environment is unavailable")
        source = self.root / "apps/agent/src/workbench_agent"
        source.mkdir(parents=True)
        body = "def complex_function(value):\n" + "".join(
            f"    if value == {i}:\n        value += 1\n" for i in range(13)
        )
        (source / "example.py").write_text(body, encoding="utf-8")
        historical = source / "persistence/migrations/versions"
        historical.mkdir(parents=True)
        (historical / "old.py").write_text(body, encoding="utf-8")
        diagnostics = self.checker.identify(
            self.root, self.checker.run_ruff(self.root, ruff)
        )
        self.assertEqual(
            diagnostics,
            {
                (
                    "apps/agent/src/workbench_agent/example.py",
                    "complex_function",
                    "C901",
                ): 14
            },
        )


if __name__ == "__main__":
    unittest.main()
