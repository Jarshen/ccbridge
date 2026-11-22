"""Read the local session indexes. No transcript lookup, filesystem probes for
projects, or session binding. Results are newest first when a timestamp exists.

CloudCLI rows need both the app ID and the announced Claude ID. Codex uses one
ID for both. In older Codex indexes, 'vscode' is shared by Desktop and the IDE
extension. Without an originator these interactive rows are legacy candidates;
the index alone cannot prove which client created them.
"""

from contextlib import closing
import ntpath
from pathlib import Path
import re
import sqlite3


def default_paths() -> dict[str, Path]:
    """Index locations for the normal Windows account and installed schema."""
    home = Path.home()
    return {"cc": home / ".cloudcli" / "auth.db",
            "desktop": home / ".codex" / "state_5.sqlite"}


def _windows_cwd(value):
    if not isinstance(value, str) or "\0" in value:
        return None
    path = value.replace("/", "\\")
    if path[:8].lower() == "\\\\?\\unc\\":
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
        if not re.match(r"^[A-Za-z]:\\", path):
            return None
    if re.match(r"^[A-Za-z]:\\", path) or re.match(r"^\\\\(?![.?]\\)[^\\]+\\[^\\]+(?:\\|$)", path):
        return ntpath.normpath(path)
    return None


def _cc_rows(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(projects)")}
    filters = ["s.provider = 'claude'", "s.isArchived = 0",
               "s.provider_session_id IS NOT NULL"]
    if {"project_path", "isArchived"} <= columns:
        filters.append("""NOT EXISTS (
            SELECT 1 FROM projects p WHERE p.project_path = s.project_path
            AND p.isArchived = 1)""")
    session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
    order = "s.updated_at DESC, " if "updated_at" in session_columns else ""
    return db.execute(f"""
        SELECT s.session_id, s.provider_session_id, s.custom_name, s.project_path
        FROM sessions s WHERE {' AND '.join(filters)}
        ORDER BY {order}s.session_id
    """)


def _desktop_rows(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
    filters = ["archived = 0", "source IN ('vscode', 'desktop')"]
    if "originator" in columns:
        filters.append("""lower(coalesce(originator, '')) IN
            ('', 'codex desktop', 'codex_work_desktop')""")
    if "thread_source" in columns:
        filters.append("coalesce(nullif(thread_source, ''), 'user') = 'user'")
    for field in ("agent_role", "agent_nickname", "agent_path"):
        if field in columns:
            filters.append(f"nullif({field}, '') IS NULL")
    title = "coalesce(nullif(trim(name), ''), title)" if "name" in columns else "title"
    order = "updated_at DESC, " if "updated_at" in columns else ""
    return db.execute(f"""
        SELECT id, id, {title}, cwd FROM threads
        WHERE {' AND '.join(filters)} ORDER BY {order}id
    """)


def list_sessions(kind, db_path, query='') -> list[dict[str, str]]:
    """List compatible 'cc' or 'desktop' rows from the specified SQLite index.

    Search is a literal Unicode case-insensitive substring of any output field.
    Missing/unreadable databases and incompatible required columns raise SQLite
    errors. An empty list means the query ran and no compatible row matched.
    """
    if kind not in ("cc", "desktop"):
        raise ValueError("kind must be 'cc' or 'desktop'")
    uri = Path(db_path).resolve().as_uri()
    # On Windows, SQLite expects UNC paths in the path part of a file URI.
    if uri.startswith("file://") and not uri.startswith("file:///"):
        uri = "file:////" + uri[len("file://"):]
    needle = query.strip().casefold()
    result = []
    with closing(sqlite3.connect(uri + "?mode=ro", uri=True)) as db:
        rows = _cc_rows(db) if kind == "cc" else _desktop_rows(db)
        for app_id, native_id, title, project in rows:
            cwd = _windows_cwd(project)
            if not cwd or not all(isinstance(i, str) and i.strip() for i in (app_id, native_id)):
                continue
            row = {"id": app_id, "native_id": native_id,
                   "title": (title or "").strip() or app_id, "cwd": cwd}
            if (not needle or any(needle in value.casefold() for value in row.values())
                    or needle.replace("/", "\\") in cwd.casefold()):
                result.append(row)
    return result
