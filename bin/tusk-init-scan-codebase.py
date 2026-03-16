#!/usr/bin/env python3
"""Scan a project root and return detected manifests and domain suggestions.

Returns JSON:
{
  "manifests": ["package.json", "pyproject.toml", ...],
  "detected_domains": [
    {"name": "frontend", "confidence": "high", "signals": ["src/components/", "react in deps"]},
    ...
  ]
}
"""

import json
import os
import sys


# ---------------------------------------------------------------------------
# Manifest detection helpers
# ---------------------------------------------------------------------------

MANIFEST_FILES = [
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "docker-compose.yml",
    "Dockerfile",
    "CLAUDE.md",
    "Makefile",
]


def _find_manifests(root: str) -> list:
    """Return list of manifest filenames that exist at root."""
    found = []
    for name in MANIFEST_FILES:
        if os.path.isfile(os.path.join(root, name)):
            found.append(name)
    return found


def _read_json_file(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _read_text_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Directory-signal helpers
# ---------------------------------------------------------------------------

_DIR_SIGNALS = [
    # (subpath, domain_name, confidence)
    ("components",         "frontend",        "high"),
    ("src/components",     "frontend",        "high"),
    ("app/components",     "frontend",        "high"),
    ("api",                "api",             "high"),
    ("routes",             "api",             "medium"),
    ("src/api",            "api",             "high"),
    ("src/routes",         "api",             "medium"),
    ("migrations",         "database",        "high"),
    ("prisma",             "database",        "high"),
    ("models",             "database",        "medium"),
    ("tests",              "tests",           "medium"),
    ("__tests__",          "tests",           "medium"),
    ("spec",               "tests",           "medium"),
    ("infrastructure",     "infrastructure",  "high"),
    ("terraform",          "infrastructure",  "high"),
    (".github/workflows",  "infrastructure",  "high"),
    ("docs",               "docs",            "medium"),
    ("mobile",             "mobile",          "medium"),
    ("android",            "mobile",          "high"),
    ("ios",                "mobile",          "high"),
]


def _find_dir_signals(root: str) -> list:
    """Return list of (subpath, domain, confidence) tuples that actually exist."""
    found = []
    for subpath, domain, confidence in _DIR_SIGNALS:
        full = os.path.join(root, subpath)
        if os.path.isdir(full):
            found.append((subpath + "/", domain, confidence))
    return found


def _find_monorepo_signals(root: str) -> list:
    """Check for monorepo patterns: packages/*/ or apps/*/."""
    signals = []
    for parent in ("packages", "apps"):
        parent_path = os.path.join(root, parent)
        if not os.path.isdir(parent_path):
            continue
        try:
            children = [
                e for e in os.scandir(parent_path)
                if e.is_dir() and not e.name.startswith(".")
            ]
        except OSError:
            continue
        if children:
            signals.append((parent + "/*/", "monorepo", "high"))
            break
    return signals


# ---------------------------------------------------------------------------
# Dependency-signal helpers (package.json / pyproject.toml / Cargo.toml)
# ---------------------------------------------------------------------------

_NPM_FRONTEND = {
    "react", "vue", "@angular/core", "svelte", "next", "@nuxtjs/composition-api",
    "nuxt", "preact", "solid-js", "lit", "ember-cli",
}
_NPM_API = {
    "express", "fastify", "koa", "hapi", "@hapi/hapi", "restify",
    "nestjs", "@nestjs/core", "feathers", "@feathersjs/feathers",
    "sails", "meteor",
}
_NPM_DATABASE = {
    "prisma", "@prisma/client", "sequelize", "typeorm", "mongoose",
    "knex", "bookshelf", "objection", "drizzle-orm",
}
_NPM_AUTH = {
    "passport", "next-auth", "@auth/core", "lucia", "firebase",
    "auth0", "keycloak-js",
}
_NPM_CLI = {
    "commander", "yargs", "meow", "oclif", "vorpal",
    "ink", "clack", "@clack/prompts",
}
_NPM_MOBILE = {
    "react-native", "expo", "@capacitor/core",
}
_NPM_DATA = {
    "tensorflow", "@tensorflow/tfjs", "brain.js", "ml5",
}

_PY_API = {
    "fastapi", "flask", "django", "tornado", "starlette",
    "sanic", "aiohttp", "falcon", "bottle",
}
_PY_DATABASE = {
    "sqlalchemy", "alembic", "peewee", "tortoise-orm",
    "django", "databases",
}
_PY_CLI = {
    "click", "typer", "argparse", "docopt", "fire", "rich",
}
_PY_DATA = {
    "torch", "tensorflow", "keras", "sklearn", "scikit-learn",
    "pandas", "numpy", "xgboost", "lightgbm", "transformers",
    "huggingface-hub",
}
_PY_AUTH = {
    "authlib", "python-jose", "passlib", "fastapi-users",
}

_CARGO_CLI = {"clap", "structopt", "argh", "pico-args"}
_CARGO_API = {"actix-web", "axum", "warp", "rocket", "tide", "poem"}
_CARGO_DATABASE = {"diesel", "sea-orm", "sqlx", "rusqlite"}

_GO_API = {"github.com/gin-gonic/gin", "github.com/labstack/echo",
           "github.com/gofiber/fiber", "github.com/gorilla/mux"}
_GO_CLI = {"github.com/spf13/cobra", "github.com/urfave/cli"}
_GO_DATABASE = {"gorm.io/gorm", "github.com/jmoiron/sqlx"}


def _npm_deps(pkg: dict) -> set:
    deps = set()
    deps.update(pkg.get("dependencies", {}).keys())
    deps.update(pkg.get("devDependencies", {}).keys())
    return deps


def _py_deps(text: str) -> set:
    """Extract normalised dependency names from pyproject.toml text."""
    deps = set()
    in_deps = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in ('[project.dependencies]', '[tool.poetry.dependencies]',
                        '[tool.flit.metadata]', 'dependencies = ['):
            in_deps = True
            continue
        if in_deps and stripped.startswith('[') and not stripped.startswith('[project.optional'):
            in_deps = False
        if in_deps or stripped.startswith('"') or stripped.startswith("'"):
            # Grab the first token before any version specifier
            name = stripped.strip('"').strip("'").split(";")[0].split(">=")[0]\
                           .split("<=")[0].split("==")[0].split("!=")[0]\
                           .split("~=")[0].split(",")[0].strip().lower()
            if name:
                deps.add(name)
    return deps


def _cargo_deps(text: str) -> set:
    deps = set()
    in_deps = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in ('[dependencies]', '[dev-dependencies]'):
            in_deps = True
            continue
        if in_deps and stripped.startswith('['):
            in_deps = False
        if in_deps and '=' in stripped:
            crate = stripped.split('=')[0].strip().lower()
            deps.add(crate)
    return deps


def _go_imports(text: str) -> set:
    deps = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("//") and not stripped.startswith("module") and not stripped.startswith("go "):
            parts = stripped.split()
            if parts:
                deps.add(parts[0])
    return deps


def _dep_signals_npm(root: str) -> list:
    pkg = _read_json_file(os.path.join(root, "package.json"))
    if not pkg:
        return []
    deps = _npm_deps(pkg)
    signals = []
    for dep in sorted(deps):
        if dep in _NPM_FRONTEND:
            signals.append((dep + " in deps", "frontend", "high"))
        if dep in _NPM_API:
            signals.append((dep + " in deps", "api", "high"))
        if dep in _NPM_DATABASE:
            signals.append((dep + " in deps", "database", "high"))
        if dep in _NPM_AUTH:
            signals.append((dep + " in deps", "auth", "high"))
        if dep in _NPM_CLI:
            signals.append((dep + " in deps", "cli", "high"))
        if dep in _NPM_MOBILE:
            signals.append((dep + " in deps", "mobile", "high"))
        if dep in _NPM_DATA:
            signals.append((dep + " in deps", "data", "high"))
    return signals


def _dep_signals_py(root: str) -> list:
    text = _read_text_file(os.path.join(root, "pyproject.toml"))
    if not text:
        text = _read_text_file(os.path.join(root, "setup.py"))
    if not text:
        text = _read_text_file(os.path.join(root, "setup.cfg"))
    if not text:
        return []
    deps = _py_deps(text)
    signals = []
    for dep in sorted(deps):
        if dep in _PY_API:
            signals.append((dep + " in deps", "api", "high"))
        if dep in _PY_DATABASE:
            signals.append((dep + " in deps", "database", "high"))
        if dep in _PY_CLI:
            signals.append((dep + " in deps", "cli", "medium"))
        if dep in _PY_DATA:
            signals.append((dep + " in deps", "data", "high"))
        if dep in _PY_AUTH:
            signals.append((dep + " in deps", "auth", "medium"))
    return signals


def _dep_signals_cargo(root: str) -> list:
    text = _read_text_file(os.path.join(root, "Cargo.toml"))
    if not text:
        return []
    deps = _cargo_deps(text)
    signals = []
    for dep in sorted(deps):
        if dep in _CARGO_CLI:
            signals.append((dep + " in Cargo.toml", "cli", "high"))
        if dep in _CARGO_API:
            signals.append((dep + " in Cargo.toml", "api", "high"))
        if dep in _CARGO_DATABASE:
            signals.append((dep + " in Cargo.toml", "database", "high"))
    return signals


def _dep_signals_go(root: str) -> list:
    text = _read_text_file(os.path.join(root, "go.mod"))
    if not text:
        return []
    deps = _go_imports(text)
    signals = []
    for dep in sorted(deps):
        if dep in _GO_API:
            signals.append((dep + " in go.mod", "api", "high"))
        if dep in _GO_CLI:
            signals.append((dep + " in go.mod", "cli", "high"))
        if dep in _GO_DATABASE:
            signals.append((dep + " in go.mod", "database", "high"))
    return signals


# ---------------------------------------------------------------------------
# Domain aggregation
# ---------------------------------------------------------------------------

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _aggregate_domains(all_signals: list) -> list:
    """
    all_signals: list of (signal_text, domain_name, confidence_str)

    Returns list of {name, confidence, signals} sorted by confidence desc.
    """
    from collections import defaultdict
    domain_signals: dict = defaultdict(list)
    domain_best: dict = {}

    for signal, domain, confidence in all_signals:
        domain_signals[domain].append(signal)
        current = domain_best.get(domain, "low")
        if _CONFIDENCE_RANK.get(confidence, 0) > _CONFIDENCE_RANK.get(current, 0):
            domain_best[domain] = confidence

    result = []
    for domain, best_conf in domain_best.items():
        result.append({
            "name": domain,
            "confidence": best_conf,
            "signals": domain_signals[domain],
        })

    result.sort(key=lambda d: _CONFIDENCE_RANK.get(d["confidence"], 0), reverse=True)
    return result


# ---------------------------------------------------------------------------
# Top-level scan
# ---------------------------------------------------------------------------

def scan(root: str) -> dict:
    manifests = _find_manifests(root)

    all_signals = []
    all_signals.extend(_find_dir_signals(root))
    all_signals.extend(_find_monorepo_signals(root))
    all_signals.extend(_dep_signals_npm(root))
    all_signals.extend(_dep_signals_py(root))
    all_signals.extend(_dep_signals_cargo(root))
    all_signals.extend(_dep_signals_go(root))

    detected_domains = _aggregate_domains(all_signals)

    return {
        "manifests": manifests,
        "detected_domains": detected_domains,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list) -> int:
    # argv[0] = db_path (unused), argv[1] = config_path (unused), argv[2:] = optional [root_dir]
    root = argv[2] if len(argv) > 2 else os.getcwd()
    result = scan(root)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-scan-codebase", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
