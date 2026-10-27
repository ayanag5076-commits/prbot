from app.users import find_duplicate_emails, find_user_by_name


def test_find_existing_user(conn):
    assert find_user_by_name(conn, "alice") == (1, "alice", "alice@example.com")


def test_find_missing_user(conn):
    assert find_user_by_name(conn, "mallory") is None


def test_duplicates_in_first_seen_order():
    emails = ["a@x.io", "b@x.io", "b@x.io", "c@x.io", "a@x.io", "a@x.io"]
    assert find_duplicate_emails(emails) == ["a@x.io", "b@x.io"]


def test_no_duplicates():
    assert find_duplicate_emails(["a@x.io", "b@x.io"]) == []
