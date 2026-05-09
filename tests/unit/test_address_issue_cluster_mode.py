"""Tests for /address-issue cluster selection and batch grooming guidance."""

import os
import re


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILL_PATH = os.path.join(REPO_ROOT, "skills", "address-issue", "SKILL.md")


def _skill_text():
    with open(SKILL_PATH) as f:
        return f.read()


class TestAddressIssueClusterMode:
    def test_step_1_documents_cluster_and_batch_invocations(self):
        text = _skill_text()

        assert "/address-issue --cluster worktree" in text
        assert "/address-issue --cluster worktree --batch" in text
        assert "cluster:worktree" in text

    def test_cluster_single_issue_uses_label_filtered_issue_list(self):
        text = _skill_text()

        assert re.search(
            r"gh issue list .*--label \"cluster:\$CLUSTER\"",
            text,
            re.DOTALL,
        ), "Cluster mode must fetch open issues with the selected cluster label"
        assert re.search(
            r"--cluster.*highest-leverage.*issue",
            text,
            re.IGNORECASE | re.DOTALL,
        ), "Cluster mode must select one issue instead of treating the cluster as one task"

    def test_batch_mode_groups_root_causes_before_task_creation(self):
        text = _skill_text()

        assert "Batch Cluster Mode" in text
        assert "one tusk task per root cause" in text
        assert "not one task per GitHub issue" in text
        assert re.search(
            r"canonical issue.*covered issues",
            text,
            re.IGNORECASE | re.DOTALL,
        ), "Batch mode must preserve canonical and covered issue references"
