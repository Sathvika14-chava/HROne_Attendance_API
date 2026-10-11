# DECISIONS.md

1. **Indexes.** I use unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)` for identity and race-safe writes. The date/code index supports the attendance list sort; employee/date and status/date indexes help common filters. Department/join-date indexes support headcount queries. I considered adding many overlapping indexes, but rejected that because every extra index costs storage and slows writes.

2. **Punch-in race.** Both requests may look valid, but the unique compound index allows only one document for an employee/date. One insert succeeds with 201. MongoDB rejects the other with `DuplicateKeyError`, which the API maps to 409. A pre-check alone would not be safe.

3. **Ties.** The leaderboard uses MongoDB `$rank`, not array position. If the cutoff rank is shared, every employee with rank at or below the limit is returned; tied rows are ordered by employee code.

4. **Headcount.** Department summary starts from employees who joined by month-end, then looks up that month's logs. This keeps employees with no logs in headcount and excludes future joiners.

5. **100x scale.** I would profile explain plans on production-like data, add targeted compound indexes only when they improve real queries, and consider pre-aggregated daily summaries to reduce repeated analytics work.
