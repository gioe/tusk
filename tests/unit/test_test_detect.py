"""Unit tests for tusk-test-detect.py."""

import importlib.util
import json
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "tusk_test_detect", os.path.join(BIN, "tusk-test-detect.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestNestedPackageJsonDetection:
    def test_detects_vitest_test_script_in_apps_workspace(self, tmp_path):
        mod = _load_module()
        app = tmp_path / "apps" / "web"
        app.mkdir(parents=True)
        (app / "package.json").write_text(json.dumps({
            "scripts": {"test": "vitest run"},
            "devDependencies": {},
        }))

        result = mod.detect(str(tmp_path))

        assert result == {
            "command": "cd apps/web && npm test",
            "confidence": "high",
        }

    def test_root_package_json_still_takes_precedence(self, tmp_path):
        mod = _load_module()
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "jest"},
            "devDependencies": {"jest": "^29.0.0"},
        }))
        app = tmp_path / "apps" / "web"
        app.mkdir(parents=True)
        (app / "package.json").write_text(json.dumps({
            "scripts": {"test": "vitest run"},
        }))

        result = mod.detect(str(tmp_path))

        assert result == {"command": "npx jest", "confidence": "medium"}
