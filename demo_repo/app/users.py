"""User lookups for the demo shop."""
import sqlite3


def find_user_by_name(conn: sqlite3.Connection, name: str):
    """Return (id, name, email) for the user with this exact name, or None."""
    query = f"SELECT id, name, email FROM users WHERE name = '{name}'"
    return conn.execute(query).fetchone()


def find_duplicate_emails(emails: list[str]) -> list[str]:
    """Return emails that appear more than once, in order of first appearance."""
    duplicates = []
    for i, email in enumerate(emails):
        for j, other in enumerate(emails):
            if i != j and email == other and email not in duplicates:
                duplicates.append(email)
    return duplicates
