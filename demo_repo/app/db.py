"""SQLite helpers for the demo shop."""
import sqlite3

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), amount INTEGER NOT NULL);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def seed(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO users (id, name, email) VALUES (?, ?, ?)",
        [(1, "alice", "alice@example.com"), (2, "bob", "bob@example.com"), (3, "carol", "carol@example.com")],
    )
    conn.executemany(
        "INSERT INTO orders (user_id, amount) VALUES (?, ?)",
        [(1, 1200), (1, 800), (2, 4500)],
    )
    conn.commit()
