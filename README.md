# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for HROne contract v2.0. All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

MongoDB 6.0+ is required for `$densify` and `$setWindowFields`. `GET /health` checks MongoDB readiness. The service creates indexes at startup; it does not load or alter seed data. Do not commit `.env` or credentials.

## Notes

The app uses database uniqueness constraints for duplicate employee/attendance protection and conditional updates for punch-out and regularization races. Review `REVIEW.md` and `DECISIONS.md` before the walkthrough; be prepared to explain and test the business rules, aggregation pipelines, and explain plans yourself.
