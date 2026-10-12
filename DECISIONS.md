# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** `employees.emp_code` (unique) — guarantees uniqueness and fast lookups. `attendance_logs.(emp_code, date)` (unique compound) — enforces one record per employee per day and supports the employee monthly aggregation. `attendance_logs.(date desc, emp_code asc)` — matches the list-attendance sort order and serves date-range queries for analytics. `employees.(department, joined_on)` — speeds up headcount calculations. I considered a separate index on `attendance_logs.status` but rejected it because status has very low cardinality and the date-based index already narrows results sufficiently.

2. **Punch-in race.** Both requests pass validation. Both call `insert_one`. The unique compound index on `(emp_code, date)` means exactly one insert succeeds; the other raises `DuplicateKeyError`, which the handler catches and returns 409. No check-then-insert gap.

3. **Ties.** I use MongoDB's `$setWindowFields` with `$rank`, which gives standard competition ranking (1, 2, 2, 4). The `$match` after ranking keeps all rows with `rank <= limit`, so tied employees at the cutoff are all included, potentially returning more than `limit` rows.

4. **Headcount.** A separate aggregation on the `employees` collection groups by department and counts those with `joined_on <= month end`. This runs independently of attendance data, so employees with zero logs are counted.

5. **One thing I would change.** I would shard `attendance_logs` by `emp_code` to distribute writes and reads across nodes, and pre-aggregate monthly summaries into a materialized collection updated on each write, avoiding repeated full-month scans.
