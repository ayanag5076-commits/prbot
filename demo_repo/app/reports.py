"""Order reporting for the demo shop."""
import sqlite3


def order_totals_by_user(conn: sqlite3.Connection) -> dict[str, int]:
    """Return {user_name: total_spent_in_cents} for every user, including users with no orders."""
    totals = {}
    users = conn.execute("SELECT id, name FROM users ORDER BY id").fetchall()
    for user_id, name in users:
        rows = conn.execute(
            "SELECT amount FROM orders WHERE user_id = ?", (user_id,)
        ).fetchall()
        totals[name] = sum(amount for (amount,) in rows)
    return totals
