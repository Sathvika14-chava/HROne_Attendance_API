# REVIEW.md — starter review findings

1. **`compute_late_minutes`:** It used a naive `replace()` and did not correctly anchor times to IST or account for overnight shifts. A 05:50 punch-in for a 22:00–06:00 shift was compared with 22:00 on the same day. Fixed by using IST-aware datetimes and the previous shift-start date for overnight early-morning punches.
2. **`compute_late_minutes`:** The original logic did not explicitly truncate milliseconds before calculating the grace boundary. Inputs such as 09:40:00.900 could produce inconsistent results. Instants are now truncated to whole seconds first.
3. **`compute_work_hours`:** Python `round()` uses ties-to-even and float arithmetic. Exact half-centimal-hour cases could be rounded incorrectly. Replaced with `Decimal` and `ROUND_HALF_UP`.
4. **`compute_overtime`:** It put `shift_end` on the attendance date unconditionally, so overnight shifts ended on the wrong day. It now advances the end date when `shift_end <= shift_start` and applies the 30-minute threshold.
5. **`/health`:** It returned `200` without contacting MongoDB. It now pings MongoDB and returns `503` if the database is unavailable.
6. **`POST /employees`:** A check-then-insert duplicate test races under concurrent requests, and it did not rely on a unique database constraint. A unique `emp_code` index and `DuplicateKeyError` handling now enforce `409` under concurrency.
7. **Employee creation:** `created_at` was a naive local datetime and was not consistently serialized as epoch milliseconds. It is now stored as a UTC BSON datetime and returned in epoch milliseconds.
8. **`GET /employees`:** `skip = page * page_size` skipped the first page. It now uses `(page - 1) * page_size`, validates page limits, sorts by `emp_code`, and counts only filtered employees.
9. **`POST /attendance/punch-in`:** Unknown employee codes caused a server error when dereferencing `emp`; it now returns `404`. Timestamp seconds, floats, strings, and out-of-range values are rejected.
10. **Punch-in date/status:** The starter used the server's local timezone, accepted unsupported statuses, and ignored overnight attendance-date rules. It now validates status and derives the attendance date in IST.
11. **Punch-in concurrency:** The starter used check-then-insert, allowing duplicates under parallel requests. A unique compound index on `(emp_code, date)` makes one insert win and maps the others to `409`.
12. **`GET /attendance`:** It loaded and sorted every matching document in Python, which is too slow for 100,000 records. Filtering, sorting, skip/limit, and count are now performed by MongoDB.
13. **Attendance sorting/filtering:** The original sort did not include the `emp_code` tie-breaker and did not reject reversed date ranges. Both are handled now.
14. **Punch-out:** The starter had no endpoint to close records. Added lookup of the most recent eligible punch-in, time-range validation, derived-field calculation, and an atomic conditional update so simultaneous punch-outs cannot both succeed.
15. **Regularization/history:** The starter had no correction endpoint. Added status/time validation, recomputation of derived values, change-only audit fields, append-only history, and compare-and-swap conflict handling.
16. **Analytics/admin endpoints:** The starter omitted all five required endpoints. Implemented monthly summaries, department aggregation, ranked late leaderboard, a densified department trend with moving average, and explain output scaffolding.
17. **Indexes:** The starter created no indexes. Startup now creates idempotent unique, filter, sort, and punch-in lookup indexes.

## Examined and retained

- `emp_code` remains the business identifier; MongoDB `_id` is not exposed in responses.
- Analytics use stored derived fields (`work_hours`, `late_minutes`, `overtime_minutes`, `half_day`) rather than recalculating seeded records.
- Missing `history` and `half_day` fields are treated as `[]` and `false` respectively.
