import ast
import importlib.util
from pathlib import Path


def test_dependencies_point_inward_and_interfaces_do_not_call_connectors():
    root = Path(__file__).resolve().parents[2] / "src" / "workbench_mcp_server"
    allowed = {
        "domain": {"domain"},
        "application": {"application", "domain"},
        "infrastructure": {"infrastructure", "application", "domain"},
        "interfaces": {"interfaces", "application", "domain"},
    }
    # CLI is a process entry point: composition/config loading is deliberately allowed here.
    composition_files = {"bootstrap.py", "interfaces/cli.py"}
    violations = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        layer = relative.parts[0]
        if relative.as_posix() in composition_files or layer not in allowed:
            continue
        module = "workbench_mcp_server." + ".".join(relative.with_suffix("").parts)
        package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                name = ("." * node.level) + (node.module or "")
                names = [importlib.util.resolve_name(name, package) if node.level else name]
            for name in names:
                if name.startswith("workbench_mcp_server."):
                    target = name.split(".")[1]
                    if target not in allowed[layer]:
                        violations.append(f"{relative}: {name}")
                elif layer == "domain" and name.split(".")[0] not in {
                    "dataclasses",
                    "datetime",
                    "typing",
                    "enum",
                    "collections",
                    "__future__",
                }:
                    violations.append(f"{relative}: external domain dependency {name}")
    assert not violations, "\n".join(violations)
