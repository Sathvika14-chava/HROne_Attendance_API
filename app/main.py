"""HROne Employee Attendance & Analytics API, contract v2.0."""

import os

import re

import calendar

from datetime import datetime, date, time, timedelta, timezone

from decimal import Decimal, ROUND_HALF_UP

from typing import Optional, Literal, Any



from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query, Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument

from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError



load_dotenv(override=False)

IST = timezone(timedelta(hours=5, minutes=30))

UTC = timezone.utc

PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")

ALL_STATUSES = PRESENT_STATUSES + ("ABSENT", "LEAVE")



client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=1500)

db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.get("/")
def root():
    return {"message": "HROne Attendance API is running"}





def now_utc() -> datetime:

    return datetime.now(UTC).replace(microsecond=0)





def truncate_dt(value: datetime) -> datetime:

    if value.tzinfo is None:

        value = value.replace(tzinfo=UTC)

    return value.astimezone(UTC).replace(microsecond=0)





def to_ms(value: Optional[datetime]) -> Optional[int]:

    if value is None:

        return None

    return int(truncate_dt(value).timestamp() * 1000)





def from_ms(value: Optional[int]) -> Optional[datetime]:

    if value is None:

        return None

    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)





def api_doc(doc: dict) -> dict:

    out = dict(doc)

    out.pop("_id", None)

    for key in ("created_at", "punch_in", "punch_out"):

        if key in out:

            out[key] = to_ms(out[key]) if out[key] is not None else None

    out.setdefault("history", [])

    out.setdefault("half_day", False)

    for item in out.get("history", []):

        if item.get("at") is not None:

            item["at"] = to_ms(item["at"])

        for change in item.get("changes", {}).values():

            for side in ("from", "to"):

                if isinstance(change.get(side), datetime):

                    change[side] = to_ms(change[side])

    return out





def require_date(value: str, field: str = "date") -> date:

    try:

        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):

            raise ValueError

        return date.fromisoformat(value)

    except (ValueError, TypeError):

        raise HTTPException(422, f"{field} must be YYYY-MM-DD")





def require_month(value: str) -> tuple[date, date]:

    if not re.fullmatch(r"\d{4}-\d{2}", value or ""):

        raise HTTPException(422, "month must be YYYY-MM")

    try:

        y, m = map(int, value.split("-"))

        first = date(y, m, 1)

        last = date(y, m, calendar.monthrange(y, m)[1])

        return first, last

    except ValueError:

        raise HTTPException(422, "month must be YYYY-MM")





def validate_shift(value: str) -> str:

    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):

        raise ValueError("must be a valid HH:MM time")

    return value





def validate_timestamp(value: Optional[int]) -> Optional[int]:

    if value is None:

        return None

    if isinstance(value, bool) or not isinstance(value, int) or value < 100000000000 or value > 4102444800000:

        raise HTTPException(422, "timestamp must be an epoch-millisecond integer in range")

    return value





def shift_start_instant(punch_in: datetime, shift_start: str) -> datetime:

    local = punch_in.astimezone(IST)

    hh, mm = map(int, shift_start.split(":"))

    return local.replace(hour=hh, minute=mm, second=0, microsecond=0)





def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: Optional[str] = None) -> int:

    local = truncate_dt(punch_in).astimezone(IST)

    start = shift_start_instant(local, shift_start)

    if shift_end is not None:

        start_hm = tuple(map(int, shift_start.split(":")))

        end_hm = tuple(map(int, shift_end.split(":")))

        if end_hm <= start_hm and (local.hour, local.minute) < end_hm:

            start -= timedelta(days=1)

    delta = int((local - start).total_seconds())

    return delta // 60 if delta > 600 else 0





def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:

    local = truncate_dt(punch_in).astimezone(IST)

    start_hm = tuple(map(int, shift_start.split(":")))

    end_hm = tuple(map(int, shift_end.split(":")))

    if end_hm <= start_hm and (local.hour, local.minute) < end_hm:

        return local.date() - timedelta(days=1)

    return local.date()





def shift_end_instant(attendance_day: str, shift_start: str, shift_end: str) -> datetime:

    d = require_date(attendance_day)

    sh, sm = map(int, shift_start.split(":"))

    eh, em = map(int, shift_end.split(":"))

    end_day = d + timedelta(days=1) if (eh, em) <= (sh, sm) else d

    return datetime.combine(end_day, time(eh, em), IST)





def round_half_up(value: float | Decimal, places: int = 2) -> float:

    quant = Decimal("1").scaleb(-places)

    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))





def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:

    seconds = int((truncate_dt(punch_out) - truncate_dt(punch_in)).total_seconds())

    return round_half_up(Decimal(seconds) / Decimal(3600), 2)





def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:

    end = shift_end_instant(date_str, shift_start, shift_end).astimezone(UTC)

    minutes = int((truncate_dt(punch_out) - end).total_seconds()) // 60

    return minutes if minutes >= 30 else 0





def derived_values(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict, day: str) -> dict:

    if status not in PRESENT_STATUSES:

        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    late = compute_late_minutes(punch_in, emp["shift_start"], emp["shift_end"]) if punch_in else 0

    if punch_in is None or punch_out is None:

        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    hours = compute_work_hours(punch_in, punch_out)

    overtime = compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], day)

    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.50}





def create_indexes() -> None:

    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code")

    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employees_department_code")

    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employees_department_joined")

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")

    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_code")

    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="ix_attendance_status_date")

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_emp_punchin")





@app.on_event("startup")

def startup() -> None:

    create_indexes()





class EmployeeCreate(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=7, max_length=9, pattern=r"^EMP\d{4,6}$")

    name: str = Field(min_length=1, max_length=100)

    email: str = Field(min_length=3, max_length=120)

    department: str = Field(min_length=1, max_length=50)

    shift_start: str = "09:30"

    shift_end: str = "18:30"

    joined_on: str



    @field_validator("name", "department")
    @classmethod
    def trim_text(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("email")
    @classmethod
    def valid_email(cls, v):
        v = v.strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("email must be a valid email address")
        return v

    @field_validator("shift_start", "shift_end")

    @classmethod

    def valid_shift(cls, v): return validate_shift(v)



    @field_validator("joined_on")

    @classmethod

    def valid_joined(cls, v):

        require_date(v, "joined_on")

        return v



    @model_validator(mode="after")

    def different_shifts(self):

        if self.shift_start == self.shift_end:

            raise ValueError("shift_start must differ from shift_end")

        return self





class PunchInRequest(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)

    punched_at: Optional[StrictInt] = None

    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"



    @field_validator("punched_at")

    @classmethod

    def valid_ms(cls, v):

        return validate_timestamp(v)





class PunchOutRequest(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)

    punched_at: Optional[StrictInt] = None



    @field_validator("punched_at")

    @classmethod

    def valid_ms(cls, v): return validate_timestamp(v)





class RegularizeRequest(BaseModel):

    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None

    punch_in: Optional[StrictInt] = None

    punch_out: Optional[StrictInt] = None

    regularized_by: str = Field(min_length=1)

    reason: str = Field(min_length=1)



    @field_validator("punch_in", "punch_out")

    @classmethod

    def valid_ms(cls, v): return validate_timestamp(v)





@app.get("/health")

def health():

    try:

        client.admin.command("ping")

        return {"status": "ok"}

    except PyMongoError:

        raise HTTPException(503, "MongoDB unavailable")





@app.post("/employees", status_code=201)

def create_employee(body: EmployeeCreate):

    doc = body.model_dump()

    doc["created_at"] = now_utc()

    try:

        db.employees.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(409, "emp_code already exists")

    return api_doc(doc)





@app.get("/employees")

def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):

    q = {"department": department} if department is not None else {}

    total = db.employees.count_documents(q)

    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))

    return {"items": [api_doc(d) for d in items], "total": total, "page": page, "page_size": page_size}





@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInRequest):

    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:

        raise HTTPException(404, "employee not found")

    ts = truncate_dt(from_ms(body.punched_at) if body.punched_at is not None else now_utc())

    day = attendance_date(ts, emp["shift_start"], emp["shift_end"]).isoformat()

    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None,

           "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),

           "overtime_minutes": 0, "half_day": False, "history": []}

    try:

        db.attendance_logs.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(409, "already punched in for this date")

    return api_doc(doc)





@app.post("/attendance/punch-out")

def punch_out(body: PunchOutRequest):

    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:

        raise HTTPException(404, "employee not found")

    out_ts = truncate_dt(from_ms(body.punched_at) if body.punched_at is not None else now_utc())

    # Most recent open or closed record that had started no later than this punch-out.

    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": out_ts}}, sort=[("punch_in", DESCENDING)])

    if not record:

        raise HTTPException(404, "no punch-in found")

    if record.get("punch_out") is not None:

        raise HTTPException(409, "record already punched out")

    in_ts = truncate_dt(record["punch_in"])

    seconds = (out_ts - in_ts).total_seconds()

    if seconds <= 0 or seconds > 24 * 3600:

        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    values = derived_values(record["status"], in_ts, out_ts, emp, record["date"])

    updated = db.attendance_logs.find_one_and_update(

        {"_id": record["_id"], "punch_out": None},

        {"$set": {"punch_out": out_ts, **values}}, return_document=ReturnDocument.AFTER)

    if updated is None:

        raise HTTPException(409, "record already punched out")

    return api_doc(updated)





@app.get("/attendance")

def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,

                    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,

                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):

    if date_from: require_date(date_from, "date_from")

    if date_to: require_date(date_to, "date_to")

    if date_from and date_to and date_from > date_to:

        raise HTTPException(422, "date_from must be <= date_to")

    q: dict[str, Any] = {}

    if emp_code is not None: q["emp_code"] = emp_code

    if status is not None: q["status"] = status

    if date_from or date_to:

        q["date"] = {}

        if date_from: q["date"]["$gte"] = date_from

        if date_to: q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)

    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))

    return {"items": [api_doc(d) for d in docs], "total": total, "page": page, "page_size": page_size}





@app.patch("/attendance/{emp_code}/{date}")

def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):

    require_date(date)

    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:

        raise HTTPException(404, "employee not found")

    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})

    if not old:

        raise HTTPException(404, "attendance record not found")

    current_status = old["status"]

    status = body.status if body.status is not None else current_status

    if status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):

        raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")

    if status in ("ABSENT", "LEAVE"):

        pin, pout = None, None

    else:

        pin = from_ms(body.punch_in) if body.punch_in is not None else old.get("punch_in")

        pout = from_ms(body.punch_out) if body.punch_out is not None else old.get("punch_out")

    if status in PRESENT_STATUSES and pin is None:

        raise HTTPException(422, "presence status requires punch_in")

    if pin is not None:

        pin = truncate_dt(pin)

        if attendance_date(pin, emp["shift_start"], emp["shift_end"]).isoformat() != date:

            raise HTTPException(422, "punch_in must belong to the record attendance date")

    if pout is not None:

        pout = truncate_dt(pout)

        if pin is None or (pout - pin).total_seconds() <= 0 or (pout - pin).total_seconds() > 86400:

            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    vals = derived_values(status, pin, pout, emp, date)

    proposed = {"status": status, "punch_in": pin, "punch_out": pout, **vals}

    changed = {}

    for k, newval in proposed.items():

        oldval = old.get(k, False if k == "half_day" else None)

        if isinstance(oldval, datetime): oldval = truncate_dt(oldval)

        if oldval != newval:

            changed[k] = {"from": oldval, "to": newval}

    if not changed:

        raise HTTPException(422, "request does not change the record")

    hist = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changed}

    # Compare-and-swap ensures concurrent edits do not overwrite each other's audit trail.

    cas_filter = {"_id": old["_id"], "status": old["status"], "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out")}

    if "history" in old:

        cas_filter["history"] = old["history"]

    else:

        cas_filter["history"] = {"$exists": False}

    result = db.attendance_logs.find_one_and_update(

        cas_filter, {"$set": proposed, "$push": {"history": hist}}, return_document=ReturnDocument.AFTER)

    if result is None:

        raise HTTPException(409, "record changed concurrently; retry")

    return api_doc(result)





def month_match(month: str) -> tuple[str, str]:

    first, last = require_month(month)

    return first.isoformat(), last.isoformat()





def working_days(start: date, end: date) -> int:

    if start > end: return 0

    count = 0

    cur = start

    while cur <= end:

        if cur.weekday() < 5: count += 1

        cur += timedelta(days=1)

    return count





@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-\d{2}$")):

    first_s, last_s = month_match(month)

    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp: raise HTTPException(404, "employee not found")

    start = max(date.fromisoformat(first_s), date.fromisoformat(emp["joined_on"]))

    days = working_days(start, date.fromisoformat(last_s))

    pipeline = [

        {"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}},

        {"$addFields": {"_weekday": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}}},

        {"$group": {"_id": None,

            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, "$_weekday"]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},

            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},

            "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},

            "overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},

    ]

    row = next(iter(db.attendance_logs.aggregate(pipeline)), {})

    present = Decimal(str(row.get("present_days", 0)))

    pct = round_half_up(present * Decimal(100) / Decimal(days), 4) if days else None

    return {"emp_code": emp_code, "month": month, "working_days": days,

            "present_days": round_half_up(present, 2), "leave_days": row.get("leave_days", 0),

            "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("late_minutes", 0),
            "total_overtime_minutes": row.get("overtime_minutes", 0),
            "attendance_pct": pct}





@app.get("/analytics/departments/summary")

def department_summary(month: str = Query(..., pattern=r"^\d{4}-\d{2}$"), department: Optional[str] = None):

    first_s, last_s = month_match(month)

    match = {"joined_on": {"$lte": last_s}}

    if department is not None: match["department"] = department

    pipeline = [

        {"$match": match},

        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [

            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first_s]}, {"$lte": ["$date", last_s]}]}}}], "as": "logs"}},

        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},

        {"$addFields": {"_weekday": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", last_s]}}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", last_s]}}}}, 6]}]}}},

        {"$group": {"_id": "$department", "headcount_codes": {"$addToSet": "$emp_code"},

            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, "$_weekday"]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},

            "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", 0]}},

            "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, 1, 0]}},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},

            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},

            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},

            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},

        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount_codes"},

            "present_days": {"$round": ["$present_days", 2]},

            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, {"$round": [{"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2]}, None]},

            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},

        {"$sort": {"department": 1}},

    ]

    # Decimal half-up is applied to reported values below; Mongo's $round is only used to aggregate the raw mean.

    rows = list(db.employees.aggregate(pipeline, allowDiskUse=True))

    return {"month": month, "items": rows}





@app.get("/analytics/leaderboard/late")

def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-\d{2}$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):

    first_s, last_s = month_match(month)

    pipeline = [

        {"$match": {"date": {"$gte": first_s, "$lte": last_s}}},

        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},

        {"$match": {"total_late_minutes": {"$gt": 0}}},

        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},

        {"$unwind": "$employee"},

    ]

    if department is not None: pipeline.append({"$match": {"employee.department": department}})

    pipeline += [

        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},

        {"$match": {"rank": {"$lte": limit}}},

        {"$sort": {"total_late_minutes": -1, "_id": 1}},

        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1, "rank": 1}},

    ]

    return {"month": month, "limit": limit, "items": list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))}





@app.get("/analytics/departments/{department}/trend")

def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(..., alias="to")):

    from_d, to_d = require_date(from_, "from"), require_date(to, "to")

    span = (to_d - from_d).days + 1

    if span <= 0 or span > 92: raise HTTPException(422, "to must be >= from and range must not exceed 92 days")

    if not db.employees.find_one({"department": department}): raise HTTPException(404, "department not found")

    start_dt = datetime.combine(from_d, time.min, UTC)

    end_dt = datetime.combine(to_d, time.min, UTC)

    pipeline = [

        {"$match": {"date": {"$gte": from_d.isoformat(), "$lte": to_d.isoformat()}}},

        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},

        {"$unwind": "$employee"}, {"$match": {"employee.department": department}},

        {"$addFields": {"dayDate": {"$dateFromString": {"dateString": "$date"}}}},

        {"$group": {"_id": "$dayDate",

            "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},

        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},

        {"$set": {"present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]},

            "date": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}},

            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$_id"}, 2]}, {"$lte": [{"$dayOfWeek": "$_id"}, 6]}]}}},

        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [

            {"$match": {"department": department}}, {"$match": {"$expr": {"$lte": ["$joined_on", "$$d"]}}}, {"$count": "n"}], "as": "hc"}},

        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]} }},

        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]} }},

        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},

        {"$sort": {"_id": 1}},

        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": {"$round": ["$present_count", 2]}, "late_count": 1,

            "attendance_rate": 1, "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}},

    ]

    rows = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))

    # $densify produces missing days when at least one day has data. Seed a calendar series in MongoDB when range is empty.

    if not rows:

        empty_pipeline = [

            {"$documents": [{"day": start_dt}]},

            {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},

            {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}}, "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]}}},

            {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [{"$match": {"department": department}}, {"$match": {"$expr": {"$lte": ["$joined_on", "$$d"]}}}, {"$count": "n"}], "as": "hc"}},

            {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "present_count": 0, "late_count": 0}},

            {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, 0.0, None]}}},

            {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},

            {"$sort": {"day": 1}}, {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}}

        ]

        rows = list(db.command("aggregate", 1, pipeline=empty_pipeline, cursor={}).get("cursor", {}).get("firstBatch", []))

    return {"department": department, "from": from_, "to": to, "items": rows}





EXPLAIN_ALLOWED = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}





@app.get("/admin/explain/{endpoint}")

def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,

                     status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),

                     month: Optional[str] = None, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None,

                     from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None):

    if endpoint not in EXPLAIN_ALLOWED: raise HTTPException(422, "unsupported endpoint")

    if endpoint == "attendance_list":

        q: dict[str, Any] = {}

        if emp_code: q["emp_code"] = emp_code

        if status: q["status"] = status

        if date_from or date_to:

            q["date"] = {}

            if date_from: q["date"]["$gte"] = date_from

            if date_to: q["date"]["$lte"] = date_to

        cmd = {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}

        return client[db.name].command({"explain": cmd, "verbosity": "executionStats"})

    if not month: raise HTTPException(422, "month is required for this endpoint")

    first_s, last_s = month_match(month)

    if endpoint == "employee_monthly":

        if not emp_code: raise HTTPException(422, "emp_code is required")

        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}}, {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}}]

        coll = "attendance_logs"

    elif endpoint == "department_summary":

        pipeline = [{"$match": {"date": {"$gte": first_s, "$lte": last_s}}}, {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}}]

        coll = "attendance_logs"

    elif endpoint == "late_leaderboard":

        pipeline = [{"$match": {"date": {"$gte": first_s, "$lte": last_s}}}, {"$group": {"_id": "$emp_code", "total": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}}]

        coll = "attendance_logs"

    else:

        if not department or not from_ or not to: raise HTTPException(422, "department, from and to are required")

        require_date(from_, "from"); require_date(to, "to")

        pipeline = [{"$match": {"date": {"$gte": from_, "$lte": to}}}, {"$group": {"_id": "$date", "count": {"$sum": 1}}}]

        coll = "attendance_logs"

    return client[db.name].command({"explain": {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}, "verbosity": "executionStats"})
