"""Shared SQLite safety utilities for incremental Justice Faite migrations.

Existing modules keep their own connection boundaries until they can be migrated
individually; this module provides the canonical primitives for new and touched
code without a risky mass rewrite.
"""
from __future__ import annotations
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def connect(path: str | os.PathLike, *, timeout: float = 30.0, row_factory=True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=timeout)
    if row_factory:
        conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={max(0, int(timeout * 1000))}")
    return conn


def ensure_columns(conn: sqlite3.Connection, table: str, columns: Iterable[tuple[str, str]]) -> list[str]:
    """Add missing columns safely and idempotently; return names added."""
    if not _IDENT.fullmatch(table):
        raise ValueError("unsafe table identifier")
    existing = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
    added = []
    for name, declaration in columns:
        if not _IDENT.fullmatch(name):
            raise ValueError("unsafe column identifier")
        declaration = str(declaration).strip()
        if not declaration or ";" in declaration or "--" in declaration:
            raise ValueError("unsafe column declaration")
        if name not in existing:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}')
            added.append(name)
    return added


def integrity_check(path: str | os.PathLike) -> tuple[bool, str]:
    conn = connect(path)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        messages = [str(row[0]) for row in rows]
        return messages == ["ok"], "; ".join(messages)
    finally:
        conn.close()


def backup_atomic(source_path: str | os.PathLike, destination_path: str | os.PathLike) -> str:
    """Create and validate a consistent SQLite backup, then atomically publish it."""
    source = Path(source_path)
    destination = Path(destination_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=str(destination.parent))
    os.close(fd)
    try:
        src = connect(source)
        dst = sqlite3.connect(temp_name)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
            src.close()
        ok, detail = integrity_check(temp_name)
        if not ok:
            raise sqlite3.DatabaseError(f"backup integrity check failed: {detail}")
        os.replace(temp_name, destination)
        return detail
    except Exception:
        try: os.unlink(temp_name)
        except OSError: pass
        raise


def sqlite_inventory(root: str | os.PathLike) -> dict:
    """Static inventory of sqlite3.connect call sites; diagnostic, not a rewrite."""
    import ast
    base = Path(root)
    found = []
    for path in base.rglob("*.py"):
        if any(part in {"tests", ".venv", "venv", "__pycache__"} for part in path.parts):
            continue
        try: tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError): continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "connect":
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "sqlite3":
                    found.append({"file": str(path.relative_to(base)), "line": node.lineno})
    return {"count": len(found), "sites": sorted(found, key=lambda x: (x["file"], x["line"]))}
