from app.reports import order_totals_by_user


def test_totals_include_users_without_orders(conn):
    assert order_totals_by_user(conn) == {"alice": 2000, "bob": 4500, "carol": 0}


def test_totals_are_ints(conn):
    assert all(isinstance(v, int) for v in order_totals_by_user(conn).values())
