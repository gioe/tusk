#!/usr/bin/env python3
"""Manage task dependencies in the SQLite database."""

import argparse
import sqlite3
import subprocess
import sys

DB_PATH = subprocess.check_output(
    ["tusk", "path"], text=True
).strip()

DEPENDENCIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id INTEGER NOT NULL,
    depends_on_id INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (task_id, depends_on_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on_id) REFERENCES tasks(id) ON DELETE CASCADE,
    CHECK (task_id != depends_on_id)
);

CREATE INDEX IF NOT EXISTS idx_task_dependencies_task_id ON task_dependencies(task_id);
CREATE INDEX IF NOT EXISTS idx_task_dependencies_depends_on_id ON task_dependencies(depends_on_id);
"""


def get_connection() -> sqlite3.Connection:
    """Get database connection with foreign keys enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection):
    """Initialize the dependencies table if it doesn't exist."""
    conn.executescript(DEPENDENCIES_SCHEMA)
    conn.commit()


def task_exists(conn: sqlite3.Connection, task_id: int) -> bool:
    """Check if a task exists."""
    result = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return result is not None


def get_task_summary(conn: sqlite3.Connection, task_id: int) -> str | None:
    """Get the summary for a task."""
    result = conn.execute("SELECT summary FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return result["summary"] if result else None


def add_dependency(conn: sqlite3.Connection, task_id: int, depends_on_id: int):
    """Add a dependency: task_id depends on depends_on_id."""
    if task_id == depends_on_id:
        print(f"Error: A task cannot depend on itself", file=sys.stderr)
        sys.exit(1)

    if not task_exists(conn, task_id):
        print(f"Error: Task {task_id} does not exist", file=sys.stderr)
        sys.exit(1)

    if not task_exists(conn, depends_on_id):
        print(f"Error: Task {depends_on_id} does not exist", file=sys.stderr)
        sys.exit(1)

    # Check for circular dependency
    if would_create_cycle(conn, task_id, depends_on_id):
        print(f"Error: Adding this dependency would create a circular dependency", file=sys.stderr)
        sys.exit(1)

    try:
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_id) VALUES (?, ?)",
            (task_id, depends_on_id)
        )
        conn.commit()
        task_summary = get_task_summary(conn, task_id)
        dep_summary = get_task_summary(conn, depends_on_id)
        print(f"Added dependency: Task {task_id} ({task_summary}) now depends on Task {depends_on_id} ({dep_summary})")
    except sqlite3.IntegrityError:
        print(f"Dependency already exists: Task {task_id} -> Task {depends_on_id}")


def remove_dependency(conn: sqlite3.Connection, task_id: int, depends_on_id: int):
    """Remove a dependency."""
    cursor = conn.execute(
        "DELETE FROM task_dependencies WHERE task_id = ? AND depends_on_id = ?",
        (task_id, depends_on_id)
    )
    conn.commit()

    if cursor.rowcount > 0:
        print(f"Removed dependency: Task {task_id} no longer depends on Task {depends_on_id}")
    else:
        print(f"No dependency found: Task {task_id} -> Task {depends_on_id}")


def list_dependencies(conn: sqlite3.Connection, task_id: int):
    """List all dependencies for a task."""
    if not task_exists(conn, task_id):
        print(f"Error: Task {task_id} does not exist", file=sys.stderr)
        sys.exit(1)

    task_summary = get_task_summary(conn, task_id)
    print(f"\nDependencies for Task {task_id}: {task_summary}")
    print("=" * 60)

    deps = conn.execute("""
        SELECT t.id, t.summary, t.status, t.priority
        FROM task_dependencies d
        JOIN tasks t ON d.depends_on_id = t.id
        WHERE d.task_id = ?
        ORDER BY t.id
    """, (task_id,)).fetchall()

    if not deps:
        print("No dependencies")
        return

    print(f"{'ID':<6} {'Status':<12} {'Priority':<10} {'Summary'}")
    print("-" * 60)
    for dep in deps:
        status_marker = "[x]" if dep["status"] == "Done" else "[ ]"
        print(f"{dep['id']:<6} {status_marker} {dep['status']:<8} {dep['priority'] or 'N/A':<10} {dep['summary']}")

    # Summary
    done_count = sum(1 for d in deps if d["status"] == "Done")
    print("-" * 60)
    print(f"Progress: {done_count}/{len(deps)} dependencies completed")

    if done_count == len(deps):
        print("Status: Ready to start")
    else:
        print("Status: Blocked - waiting on dependencies")


def list_dependents(conn: sqlite3.Connection, task_id: int):
    """List all tasks that depend on this task."""
    if not task_exists(conn, task_id):
        print(f"Error: Task {task_id} does not exist", file=sys.stderr)
        sys.exit(1)

    task_summary = get_task_summary(conn, task_id)
    print(f"\nTasks that depend on Task {task_id}: {task_summary}")
    print("=" * 60)

    dependents = conn.execute("""
        SELECT t.id, t.summary, t.status, t.priority
        FROM task_dependencies d
        JOIN tasks t ON d.task_id = t.id
        WHERE d.depends_on_id = ?
        ORDER BY t.id
    """, (task_id,)).fetchall()

    if not dependents:
        print("No tasks depend on this task")
        return

    print(f"{'ID':<6} {'Status':<12} {'Priority':<10} {'Summary'}")
    print("-" * 60)
    for dep in dependents:
        print(f"{dep['id']:<6} {dep['status']:<12} {dep['priority'] or 'N/A':<10} {dep['summary']}")


def show_blocked(conn: sqlite3.Connection):
    """Show all tasks that are blocked by incomplete dependencies."""
    print("\nBlocked Tasks (waiting on dependencies)")
    print("=" * 70)

    blocked = conn.execute("""
        SELECT DISTINCT t.id, t.summary, t.status, t.priority,
            (SELECT COUNT(*) FROM task_dependencies d2
             JOIN tasks t2 ON d2.depends_on_id = t2.id
             WHERE d2.task_id = t.id AND t2.status != 'Done') as blocking_count,
            (SELECT COUNT(*) FROM task_dependencies d3 WHERE d3.task_id = t.id) as total_deps
        FROM tasks t
        JOIN task_dependencies d ON t.id = d.task_id
        JOIN tasks dep ON d.depends_on_id = dep.id
        WHERE t.status != 'Done' AND dep.status != 'Done'
        ORDER BY t.priority DESC, t.id
    """).fetchall()

    if not blocked:
        print("No blocked tasks")
        return

    print(f"{'ID':<6} {'Status':<12} {'Blocked By':<12} {'Summary'}")
    print("-" * 70)
    for task in blocked:
        blocked_str = f"{task['blocking_count']}/{task['total_deps']} deps"
        print(f"{task['id']:<6} {task['status']:<12} {blocked_str:<12} {task['summary']}")


def show_ready(conn: sqlite3.Connection):
    """Show all tasks that are ready to start (all dependencies done or no dependencies)."""
    print("\nReady Tasks (all dependencies complete)")
    print("=" * 70)

    ready = conn.execute("""
        SELECT t.id, t.summary, t.status, t.priority,
            (SELECT COUNT(*) FROM task_dependencies d WHERE d.task_id = t.id) as dep_count
        FROM tasks t
        WHERE t.status != 'Done'
        AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks dep ON d.depends_on_id = dep.id
            WHERE d.task_id = t.id AND dep.status != 'Done'
        )
        ORDER BY t.priority DESC, t.id
    """).fetchall()

    if not ready:
        print("No ready tasks")
        return

    print(f"{'ID':<6} {'Status':<12} {'Priority':<10} {'Deps':<6} {'Summary'}")
    print("-" * 70)
    for task in ready:
        dep_str = str(task['dep_count']) if task['dep_count'] > 0 else "-"
        print(f"{task['id']:<6} {task['status']:<12} {task['priority'] or 'N/A':<10} {dep_str:<6} {task['summary']}")


def would_create_cycle(conn: sqlite3.Connection, task_id: int, depends_on_id: int) -> bool:
    """Check if adding a dependency would create a cycle."""
    # If depends_on_id already (directly or indirectly) depends on task_id,
    # then adding task_id -> depends_on_id would create a cycle
    visited = set()
    stack = [depends_on_id]

    while stack:
        current = stack.pop()
        if current == task_id:
            return True
        if current in visited:
            continue
        visited.add(current)

        # Get all tasks that current depends on
        deps = conn.execute(
            "SELECT depends_on_id FROM task_dependencies WHERE task_id = ?",
            (current,)
        ).fetchall()
        for dep in deps:
            stack.append(dep["depends_on_id"])

    return False


def show_all(conn: sqlite3.Connection):
    """Show all dependencies in the system."""
    print("\nAll Task Dependencies")
    print("=" * 80)

    all_deps = conn.execute("""
        SELECT
            d.task_id,
            t1.summary as task_summary,
            t1.status as task_status,
            d.depends_on_id,
            t2.summary as dep_summary,
            t2.status as dep_status
        FROM task_dependencies d
        JOIN tasks t1 ON d.task_id = t1.id
        JOIN tasks t2 ON d.depends_on_id = t2.id
        ORDER BY d.task_id, d.depends_on_id
    """).fetchall()

    if not all_deps:
        print("No dependencies defined")
        return

    print(f"{'Task':<30} {'Depends On':<30} {'Status'}")
    print("-" * 80)
    for dep in all_deps:
        task_str = f"{dep['task_id']}: {dep['task_summary'][:25]}"
        dep_str = f"{dep['depends_on_id']}: {dep['dep_summary'][:25]}"
        status = "Done" if dep['dep_status'] == 'Done' else "Waiting"
        print(f"{task_str:<30} {dep_str:<30} {status}")


def main():
    parser = argparse.ArgumentParser(
        description="Manage task dependencies in the SQLite database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s add 5 3          # Task 5 depends on Task 3
  %(prog)s remove 5 3       # Remove dependency
  %(prog)s list 5           # Show what Task 5 depends on
  %(prog)s dependents 3     # Show tasks that depend on Task 3
  %(prog)s blocked          # Show all blocked tasks
  %(prog)s ready            # Show tasks ready to start
  %(prog)s all              # Show all dependencies
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # add command
    add_parser = subparsers.add_parser("add", help="Add a dependency")
    add_parser.add_argument("task_id", type=int, help="Task that has the dependency")
    add_parser.add_argument("depends_on_id", type=int, help="Task that must be completed first")

    # remove command
    remove_parser = subparsers.add_parser("remove", help="Remove a dependency")
    remove_parser.add_argument("task_id", type=int, help="Task that has the dependency")
    remove_parser.add_argument("depends_on_id", type=int, help="Task to remove from dependencies")

    # list command
    list_parser = subparsers.add_parser("list", help="List dependencies for a task")
    list_parser.add_argument("task_id", type=int, help="Task to list dependencies for")

    # dependents command
    dependents_parser = subparsers.add_parser("dependents", help="List tasks that depend on a task")
    dependents_parser.add_argument("task_id", type=int, help="Task to find dependents for")

    # blocked command
    subparsers.add_parser("blocked", help="Show all blocked tasks")

    # ready command
    subparsers.add_parser("ready", help="Show tasks ready to start")

    # all command
    subparsers.add_parser("all", help="Show all dependencies")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    conn = get_connection()
    init_schema(conn)

    if args.command == "add":
        add_dependency(conn, args.task_id, args.depends_on_id)
    elif args.command == "remove":
        remove_dependency(conn, args.task_id, args.depends_on_id)
    elif args.command == "list":
        list_dependencies(conn, args.task_id)
    elif args.command == "dependents":
        list_dependents(conn, args.task_id)
    elif args.command == "blocked":
        show_blocked(conn)
    elif args.command == "ready":
        show_ready(conn)
    elif args.command == "all":
        show_all(conn)

    conn.close()


if __name__ == "__main__":
    main()
