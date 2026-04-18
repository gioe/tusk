"""Unit tests for rule5_done_without_closed_reason in tusk-lint.py.

Covers the SELECT vs write disambiguation that decides whether
``status = 'Done'`` in a file is a read (skip) or a write (fire).
"""

import importlib.util
import os
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_lint",
    os.path.join(REPO_ROOT, "bin", "tusk-lint.py"),
)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


def populate_root(root: str, files: dict[str, str]) -> None:
    for rel, content in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)


class TestReadContextNoFire:
    def test_insert_from_select_with_where_status_done_no_violation(self):
        """Regression: INSERT INTO x SELECT ... WHERE status = 'Done' is a read.

        The flat 15-line context window saw both INSERT and SELECT and fell
        through to the closed_reason check, producing a false positive on the
        SELECT's WHERE clause. The fix anchors detection to the nearest
        preceding DML keyword — SELECT here — and skips.
        """
        with tempfile.TemporaryDirectory() as root:
            populate_root(
                root,
                {
                    "bin/migrate.py": (
                        "def migrate(conn):\n"
                        "    conn.executescript(\"\"\"\n"
                        "        INSERT INTO task_metrics (task_id, reopen_count)\n"
                        "        SELECT\n"
                        "            t.id,\n"
                        "            COUNT(*) AS reopen_count\n"
                        "        FROM tasks t\n"
                        "        JOIN task_status_history h ON h.task_id = t.id\n"
                        "        WHERE h.from_status = 'In Progress'\n"
                        "          AND t.status = 'Done'\n"
                        "        GROUP BY t.id;\n"
                        "    \"\"\")\n"
                    )
                },
            )
            assert lint.rule5_done_without_closed_reason(root) == []

    def test_plain_select_with_status_done_no_violation(self):
        """Plain SELECT ... WHERE status = 'Done' — read context, no fire."""
        with tempfile.TemporaryDirectory() as root:
            populate_root(
                root,
                {
                    "bin/report.py": (
                        "rows = conn.execute(\n"
                        "    \"SELECT id FROM tasks WHERE status = 'Done'\"\n"
                        ").fetchall()\n"
                    )
                },
            )
            assert lint.rule5_done_without_closed_reason(root) == []

    def test_create_trigger_body_no_violation(self):
        """CREATE TRIGGER bodies are DDL — exempted regardless of nearest DML."""
        with tempfile.TemporaryDirectory() as root:
            populate_root(
                root,
                {
                    "bin/triggers.py": (
                        "DDL = \"\"\"\n"
                        "CREATE TRIGGER touch_done\n"
                        "AFTER UPDATE ON tasks\n"
                        "BEGIN\n"
                        "    UPDATE tasks SET status = 'Done' WHERE id = NEW.id;\n"
                        "END;\n"
                        "\"\"\"\n"
                    )
                },
            )
            assert lint.rule5_done_without_closed_reason(root) == []


class TestWriteContextFires:
    def test_update_set_status_done_without_closed_reason_fires(self):
        """UPDATE tasks SET status = 'Done' with no closed_reason → fire."""
        with tempfile.TemporaryDirectory() as root:
            populate_root(
                root,
                {
                    "bin/writer.py": (
                        "conn.execute(\n"
                        "    \"UPDATE tasks SET status = 'Done' WHERE id = ?\",\n"
                        "    (task_id,),\n"
                        ")\n"
                    )
                },
            )
            violations = lint.rule5_done_without_closed_reason(root)
            assert len(violations) == 1
            assert "writer.py" in violations[0]

    def test_update_set_status_done_with_closed_reason_no_violation(self):
        """closed_reason present in window — write is intentional, no fire."""
        with tempfile.TemporaryDirectory() as root:
            populate_root(
                root,
                {
                    "bin/writer_ok.py": (
                        "conn.execute(\n"
                        "    \"UPDATE tasks \"\n"
                        "    \"SET status = 'Done', closed_reason = 'completed' \"\n"
                        "    \"WHERE id = ?\",\n"
                        "    (task_id,),\n"
                        ")\n"
                    )
                },
            )
            assert lint.rule5_done_without_closed_reason(root) == []
