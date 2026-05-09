#!/usr/bin/env python3
"""Detect test framework from lockfiles and return JSON {command, confidence}."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-json-lib.py

_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps


def _read_package_json(path: str) -> dict:
    """Return vitest/jest booleans parsed from package.json, or {} on error."""
    try:
        with open(path) as f:
            pkg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    deps = {}
    deps.update(pkg.get("devDependencies", {}))
    deps.update(pkg.get("dependencies", {}))
    scripts = pkg.get("scripts", {})
    test_script = scripts.get("test", "")
    return {
        "has_test": bool(test_script),
        "vitest": "vitest" in deps or "vitest" in test_script,
        "jest": "jest" in deps or "jest" in test_script,
    }


def _detect_nested_node_test(root: str) -> dict:
    """Detect common monorepo package.json test scripts under workspace dirs."""
    workspace_roots = ("apps", "packages")
    candidates: list[str] = []
    for workspace_root in workspace_roots:
        base = os.path.join(root, workspace_root)
        if not os.path.isdir(base):
            continue
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            pkg_path = os.path.join(base, name, "package.json")
            if os.path.isfile(pkg_path):
                candidates.append(os.path.relpath(os.path.dirname(pkg_path), root))

    for rel_dir in candidates:
        runner = _read_package_json(os.path.join(root, rel_dir, "package.json"))
        if runner.get("vitest") or runner.get("jest"):
            return {"command": f"cd {rel_dir} && npm test", "confidence": "high"}

    for rel_dir in candidates:
        runner = _read_package_json(os.path.join(root, rel_dir, "package.json"))
        if runner.get("has_test"):
            return {"command": f"cd {rel_dir} && npm test", "confidence": "medium"}

    return {}


def detect(root: str) -> dict:
    """Inspect root dir for lockfiles and infer test runner."""
    pkg_path = os.path.join(root, "package.json")

    # bun
    if os.path.isfile(os.path.join(root, "bun.lockb")) or os.path.isfile(os.path.join(root, "bun.lock")):
        runner = _read_package_json(pkg_path)
        if runner.get("vitest"):
            cmd = "bun run vitest"
        elif runner.get("jest"):
            cmd = "bun run jest"
        else:
            cmd = "bun test"
        return {"command": cmd, "confidence": "high"}

    # pnpm
    if os.path.isfile(os.path.join(root, "pnpm-lock.yaml")):
        runner = _read_package_json(pkg_path)
        if runner.get("vitest"):
            cmd = "pnpm run vitest"
        elif runner.get("jest"):
            cmd = "pnpm run jest"
        else:
            cmd = "pnpm test"
        return {"command": cmd, "confidence": "high"}

    # yarn (checked before npm; more specific lockfile takes precedence)
    if os.path.isfile(os.path.join(root, "yarn.lock")):
        runner = _read_package_json(pkg_path)
        if runner.get("vitest"):
            cmd = "yarn vitest"
        elif runner.get("jest"):
            cmd = "yarn jest"
        else:
            cmd = "yarn test"
        return {"command": cmd, "confidence": "high"}

    # npm (package-lock.json)
    if os.path.isfile(os.path.join(root, "package-lock.json")):
        runner = _read_package_json(pkg_path)
        if runner.get("vitest"):
            cmd = "npx vitest"
        elif runner.get("jest"):
            cmd = "npx jest"
        else:
            cmd = "npm test"
        return {"command": cmd, "confidence": "high"}

    # bare package.json (no lockfile)
    if os.path.isfile(pkg_path):
        runner = _read_package_json(pkg_path)
        if runner.get("vitest"):
            return {"command": "npx vitest", "confidence": "medium"}
        if runner.get("jest"):
            return {"command": "npx jest", "confidence": "medium"}
        return {"command": "npm test", "confidence": "low"}

    nested_node = _detect_nested_node_test(root)
    if nested_node:
        return nested_node

    # Pipfile.lock (pipenv)
    if os.path.isfile(os.path.join(root, "Pipfile.lock")):
        return {"command": "pytest", "confidence": "high"}

    # pyproject.toml / setup.py
    if os.path.isfile(os.path.join(root, "pyproject.toml")) or os.path.isfile(os.path.join(root, "setup.py")):
        return {"command": "pytest", "confidence": "medium"}

    # Cargo.toml (Rust)
    if os.path.isfile(os.path.join(root, "Cargo.toml")):
        return {"command": "cargo test", "confidence": "high"}

    # go.mod (Go)
    if os.path.isfile(os.path.join(root, "go.mod")):
        return {"command": "go test ./...", "confidence": "high"}

    # Gemfile.lock (Ruby) — use low confidence since Rails defaults to minitest, not rspec
    if os.path.isfile(os.path.join(root, "Gemfile.lock")):
        return {"command": "bundle exec rspec", "confidence": "low"}

    # Makefile with test: target
    makefile_path = os.path.join(root, "Makefile")
    if os.path.isfile(makefile_path):
        try:
            with open(makefile_path) as f:
                content = f.read()
            if "\ntest:" in content or content.startswith("test:"):
                return {"command": "make test", "confidence": "low"}
        except OSError:
            pass

    return {"command": None, "confidence": "none"}


def main(argv: list) -> int:
    # argv[0] = db_path (unused), argv[1] = config_path (unused), argv[2:] = optional [root_dir]
    root = argv[2] if len(argv) > 2 else os.getcwd()
    result = detect(root)
    print(dumps(result))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk test-detect", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
