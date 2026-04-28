"""Unit tests for the dupes-block validator in bin/tusk-config-tools.py.

Exercises the cmd_validate entry point against the dupes.*_threshold keys so
a future refactor that drops one of the inline checks fails loudly.
"""

import importlib.util
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BIN, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config_tools_mod = _load("tusk_config_tools", "tusk-config-tools.py")


def _run_validate(tmp_path, dupes: dict) -> int:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "statuses": ["To Do", "Done"],
        "priorities": ["High", "Medium", "Low"],
        "closed_reasons": ["completed", "expired"],
        "dupes": dupes,
    }))
    return config_tools_mod.cmd_validate(str(p))


class TestConfigValidatorDupesThresholds:
    def test_rejects_out_of_range_check_threshold(self, tmp_path, capsys):
        rc = _run_validate(tmp_path, {"check_threshold": 8.8})
        err = capsys.readouterr().err
        assert rc == 1
        assert '"dupes.check_threshold" must be between 0 and 1' in err

    def test_rejects_out_of_range_criterion_check_threshold(self, tmp_path, capsys):
        rc = _run_validate(tmp_path, {"criterion_check_threshold": 8.8})
        err = capsys.readouterr().err
        assert rc == 1
        assert '"dupes.criterion_check_threshold" must be between 0 and 1' in err

    def test_rejects_out_of_range_similar_threshold(self, tmp_path, capsys):
        rc = _run_validate(tmp_path, {"similar_threshold": -0.5})
        err = capsys.readouterr().err
        assert rc == 1
        assert '"dupes.similar_threshold" must be between 0 and 1' in err

    def test_rejects_non_numeric_criterion_check_threshold(self, tmp_path, capsys):
        rc = _run_validate(tmp_path, {"criterion_check_threshold": "high"})
        err = capsys.readouterr().err
        assert rc == 1
        assert '"dupes.criterion_check_threshold" must be a number' in err

    def test_rejects_non_numeric_check_threshold(self, tmp_path, capsys):
        rc = _run_validate(tmp_path, {"check_threshold": "high"})
        err = capsys.readouterr().err
        assert rc == 1
        assert '"dupes.check_threshold" must be a number' in err

    def test_accepts_valid_thresholds(self, tmp_path):
        rc = _run_validate(tmp_path, {
            "check_threshold": 0.82,
            "criterion_check_threshold": 0.88,
            "similar_threshold": 0.6,
        })
        assert rc == 0

    def test_accepts_boundary_values(self, tmp_path):
        rc = _run_validate(tmp_path, {
            "check_threshold": 0,
            "criterion_check_threshold": 1,
            "similar_threshold": 0.5,
        })
        assert rc == 0

    def test_accepts_int_thresholds(self, tmp_path):
        rc = _run_validate(tmp_path, {"criterion_check_threshold": 1})
        assert rc == 0
