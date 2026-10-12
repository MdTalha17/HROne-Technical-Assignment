# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` | No actual MongoDB ping; always returns 200 even if DB is down | Start app without MongoDB running — GET /health returns 200 instead of 503 | Added `db.command("ping")` wrapped in try/except; raise 503 on failure |
| 2 | `create_employee()` — check-then-insert | Race condition: `find_one` then `insert_one` allows two concurrent creates to both pass the check | Fire two POST /employees with the same emp_code simultaneously — both get 201 | Rely on unique index on `emp_code`; catch `DuplicateKeyError` for 409 |
| 3 | `create_employee()` — `datetime.now()` | Naive datetime (no timezone); BSON stores it without tz info, breaks epoch-ms conversion | `created_at` in response is wrong or inconsistent across time zones | Use `datetime.now(timezone.utc).replace(microsecond=0)` |
| 4 | `create_employee()` — response | Returns raw datetime for `created_at` instead of epoch milliseconds; `_id` leaks | Response has `{"created_at": "2026-..."}` instead of an integer; `_id` field present | Format through `_fe()` which converts to epoch ms and excludes `_id` |
| 5 | `create_employee()` | No input validation at all — emp_code pattern, email format, shift_start ≠ shift_end, name length, etc. | Send `{"emp_code":"X","shift_start":"09:30","shift_end":"09:30",...}` — accepted | Added regex/length/equality validations per openapi.yaml schemas |
| 6 | `list_employees()` — skip | `skip = page * page_size` — page 1 skips 20, showing page 2 | GET /employees?page=1 returns items starting from position 20 | Changed to `(page - 1) * page_size` |
| 7 | `list_employees()` — total | `count_documents({})` ignores the department filter | GET /employees?department=Sales — total=6 (all) instead of total=2 | Pass the same filter `q` to `count_documents(q)` |
| 8 | `list_employees()` — sort | No sort applied; spec requires `emp_code` ascending | Results come in insertion order instead of sorted by emp_code | Added `.sort("emp_code", ASCENDING)` |
| 9 | `list_employees()` — response | Returns raw datetimes for `created_at` | `created_at` is a datetime string, not epoch ms | Format each item through `_fe()` |
| 10 | `punch_in()` — no 404 | No check that the employee exists; crashes on `emp["shift_start"]` if emp is None | POST punch-in for non-existent emp_code → 500 (NoneType subscript) | Added `if not emp: raise HTTPException(404)` |
| 11 | `punch_in()` — `datetime.fromtimestamp()` | Returns naive local-time datetime, not UTC; IST date calculation is wrong | Punch at 00:15 UTC (05:45 IST) — date computed from server's local TZ, not IST | Use `datetime.fromtimestamp(ms/1000, tz=timezone.utc)` then convert to IST for date |
| 12 | `punch_in()` — date logic | Uses `ts.date()` (server-local date) instead of IST date; overnight-shift rule (R1) not implemented | Punch-in at 02:00 IST for a 22:00–06:00 shift → wrong date (should be previous day) | Implemented `_att_date()` with IST conversion and overnight-shift exception |
| 13 | `punch_in()` — no truncation | Epoch ms with sub-second precision is stored with microseconds instead of truncated (R1) | `punched_at=1783312500999` → stored with 999ms, late calculation off by a second | Truncate to whole seconds: `.replace(microsecond=0)` |
| 14 | `punch_in()` — race condition | Check-then-insert: `find_one` then `insert_one` allows concurrent duplicates | Two punch-ins for the same emp+date at the same moment → both get 201 | Rely on unique compound index `(emp_code, date)`; catch `DuplicateKeyError` |
| 15 | `punch_in()` — status validation | No validation that status is PRESENT/WFH/ON_DUTY; accepts ABSENT/LEAVE | POST punch-in with `status: "ABSENT"` succeeds — creates an invalid record | Validate status is in `{"PRESENT", "WFH", "ON_DUTY"}` |
| 16 | `punch_in()` — punched_at validation | No validation of `punched_at` (range, type); accepts strings, floats, out-of-range | `punched_at: 999` (looks like seconds) or `punched_at: "abc"` accepted | Added `_vms()` validator checking type and range |
| 17 | `punch_in()` — response `id` field | Returns spurious `"id"` field (not in contract) and raw datetimes | Response has extra `id` field; punch_in/punch_out are datetime objects | Format through `_fr()` which converts to epoch ms and excludes internal fields |
| 18 | `compute_late_minutes()` — IST | Replaces h/m on the punch_in datetime — wrong for overnight shifts where punch-in crosses midnight | EMP0005 punches in at 02:00 IST (previous day's shift) — shift_start computed on wrong calendar day | Construct shift_start datetime from the attendance date, not from the punch_in datetime |
| 19 | `compute_late_minutes()` — rounding | Uses `int()` (truncation towards zero), not `floor()`; also doesn't use "floor of minutes" as spec says | Negative deltas (early arrival) close to zero could round wrong way | Use `//` (floor division) on integer seconds |
| 20 | `compute_work_hours()` — rounding | Python's `round()` uses banker's rounding (round-half-to-even); spec requires half-up | `round(8.125, 2)` → 8.12 (banker's) vs 8.13 (half-up) | Use `Decimal.quantize(ROUND_HALF_UP)` |
| 21 | `compute_overtime()` — overnight | Puts shift_end on the attendance date even for overnight shifts (should be next day) | EMP0005 (22:00–06:00): overtime computed against 06:00 same day instead of next day → huge number | Detect overnight shift and add 1 day to shift_end date |
| 22 | `compute_overtime()` — threshold | Returns any positive overtime; spec says only count if ≥ 30 minutes (R3) | 5 minutes past shift end → overtime_minutes=5 instead of 0 | Added `if mins >= 30 else 0` check |
| 23 | `list_attendance()` — full scan | `list(db.attendance_logs.find(q))` loads entire collection into memory, sorts in Python | With 100k records, response is slow and uses excessive memory; server may OOM | Use `.sort().skip().limit()` on the cursor; let MongoDB do the work |
| 24 | `list_attendance()` — sort | Only sorts by `date` descending; spec also requires `emp_code` ascending as tiebreaker | Records on the same date appear in arbitrary order | Sort by `[("date", DESCENDING), ("emp_code", ASCENDING)]` |
| 25 | `list_attendance()` — response | Adds `"id"` field (not in contract) and returns raw datetimes | Extra `id` field; punch_in/punch_out/history.at are datetime objects | Format through `_fr()` |
| 26 | No indexes | No indexes created at startup; spec requires idempotent index creation | COLLSCAN on 100k records → slow queries; concurrent punch-ins/creates not safe | Created unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)`, plus sort/analytics indexes |
| 27 | No page_size validation | `page_size` accepted without bounds; spec says max 100, min 1, default 20 | `page_size=999999` dumps entire collection; `page_size=-1` causes errors | Use `Query(20, ge=1, le=100)` |

## Not a defect

- **`IST = timezone(timedelta(hours=5, minutes=30))`**: Correct — IST is a fixed UTC+05:30 offset, not a DST-aware zone. Using a fixed offset is intentional and correct for this assignment.
- **`load_dotenv()` at module level**: Correct — `python-dotenv` does not override existing environment variables by default, so real env vars take priority as required.
