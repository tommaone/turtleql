"""
Database adapter abstraction for TurtleQL.

Implement DbAdapter to add support for any database backend.
Two implementations are provided out of the box: SqliteAdapter and PostgresAdapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class DbAdapter(ABC):
    """Minimal interface any database backend must implement."""

    @abstractmethod
    def connect(self, **kwargs) -> Any:
        """Open and return a connection object. kwargs are driver-specific."""
        ...

    @abstractmethod
    def execute(self, connection: Any, sql: str) -> Dict[str, Any]:
        """Execute SQL on an open connection.

        Returns {"columns": [...], "rows": [...], "rowcount": int}.
        SELECT-like queries populate columns + rows.
        DML queries populate rowcount only (columns=[], rows=[]).
        """
        ...

    @abstractmethod
    def close(self, connection: Any) -> None:
        """Close the connection."""
        ...

    def name(self) -> str:
        return self.__class__.__name__


class SqliteAdapter(DbAdapter):
    """SQLite adapter — uses stdlib sqlite3, no extra dependencies."""

    def connect(self, path: str = ":memory:", **kwargs) -> Any:
        import sqlite3
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def execute(self, connection: Any, sql: str) -> Dict[str, Any]:
        cursor = connection.cursor()
        cursor.execute(sql)
        if cursor.description:
            headers = [col[0] for col in cursor.description]
            rows = [list(row) for row in cursor.fetchall()]
            cursor.close()
            return {"columns": headers, "rows": rows, "rowcount": len(rows)}
        else:
            rc = cursor.rowcount
            connection.commit()
            cursor.close()
            return {"columns": [], "rows": [], "rowcount": rc}

    def close(self, connection: Any) -> None:
        try:
            connection.close()
        except Exception:
            pass


class PostgresAdapter(DbAdapter):
    """PostgreSQL adapter — requires psycopg2 (pip install psycopg2-binary)."""

    def connect(self, host: str = "localhost", port: int = 5432,
                database: str = "", user: str = "", password: str = "", **kwargs) -> Any:
        try:
            import psycopg2
        except ImportError:
            raise RuntimeError("psycopg2 not installed — run: pip install psycopg2-binary")
        return psycopg2.connect(
            host=host, port=port, dbname=database, user=user, password=password
        )

    def execute(self, connection: Any, sql: str) -> Dict[str, Any]:
        cursor = connection.cursor()
        cursor.execute(sql)
        if cursor.description:
            headers = [col.name for col in cursor.description]
            rows = [list(row) for row in cursor.fetchall()]
            cursor.close()
            return {"columns": headers, "rows": rows, "rowcount": len(rows)}
        else:
            rc = cursor.rowcount
            connection.commit()
            cursor.close()
            return {"columns": [], "rows": [], "rowcount": rc}

    def close(self, connection: Any) -> None:
        try:
            connection.close()
        except Exception:
            pass


def get_adapter(adapter_type: str = "sqlite") -> DbAdapter:
    """Factory: return the adapter matching the config db_adapter value."""
    adapters = {
        "sqlite": SqliteAdapter,
        "postgres": PostgresAdapter,
        "postgresql": PostgresAdapter,
    }
    cls = adapters.get(adapter_type.lower())
    if not cls:
        raise ValueError(f"Unknown db_adapter '{adapter_type}'. Supported: {list(adapters)}")
    return cls()
