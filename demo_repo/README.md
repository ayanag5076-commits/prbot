# demo_repo: a tiny shop backend with seeded bugs

Target repo for prbot. `pr.diff` is the "pull request" that introduced `app/users.py` and `app/reports.py`.
It carries three deliberate bugs:

| File | Bug | Category |
|---|---|---|
| `app/users.py` `find_user_by_name` | SQL built with an f-string (SQL injection) | security |
| `app/users.py` `find_duplicate_emails` | nested loop over the same list (O(n²)) | performance |
| `app/reports.py` `order_totals_by_user` | one query per user inside a loop (N+1) | performance |

The tests in `tests/` check behaviour only, so they pass before and after a correct fix,
and catch patches that change behaviour.
