#!/usr/bin/env python3
"""Detect test framework from lockfiles and return JSON {command, confidence}."""

import json
import os
import sys


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
    return {
        "vitest": "vitest" in deps,
        "jest": "jest" in deps or "jest" in scripts.get("test", ""),
    }


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
            cmd = "pnpm vitest"
        elif runner.get("jest"):
            cmd = "pnpm jest"
        else:
            cmd = "pnpm test"
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

    # yarn
    if os.path.isfile(os.path.join(root, "yarn.lock")):
        runner = _read_package_json(pkg_path)
        if runner.get("vitest"):
            cmd = "yarn vitest"
        elif runner.get("jest"):
            cmd = "yarn jest"
        else:
            cmd = "yarn test"
        return {"command": cmd, "confidence": "high"}

    # bare package.json (no lockfile)
    if os.path.isfile(pkg_path):
        runner = _read_package_json(pkg_path)
        if runner.get("vitest"):
            return {"command": "npx vitest", "confidence": "medium"}
        if runner.get("jest"):
            return {"command": "npx jest", "confidence": "medium"}
        return {"command": "npm test", "confidence": "low"}

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

    # Gemfile.lock (Ruby)
    if os.path.isfile(os.path.join(root, "Gemfile.lock")):
        return {"command": "bundle exec rspec", "confidence": "medium"}

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
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
