"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import re
from bisect import bisect_right
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUS = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
PRESENCE_LIST = list(PRESENCE)
EMP_RE = re.compile(r"^EMP\d{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ───────────────────────────── startup ───────────────────────────── #
@app.on_event("startup")
def _startup():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index("joined_on")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)]
    )


# ───────────────────────────── helpers ───────────────────────────── #
def _h(val, p):
    """Half-up round to *p* decimal places."""
    if val is None:
        return None
    return float(Decimal(str(val)).quantize(Decimal(10) ** -p, rounding=ROUND_HALF_UP))


def _vms(v, name="punched_at"):
    """Validate an epoch-ms value; return int or raise 422."""
    if v is None:
        return None
    if isinstance(v, bool) or isinstance(v, float) or isinstance(v, str):
        raise HTTPException(422, detail=f"{name} must be an integer")
    if not isinstance(v, int):
        raise HTTPException(422, detail=f"{name} must be an integer")
    if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
        raise HTTPException(422, detail=f"{name} out of valid range")
    return v


def _ms2dt(ms):
    """Epoch ms → UTC-aware datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)


def _dt2ms(dt):
    """Datetime → epoch ms (None-safe)."""
    if dt is None:
        return None
    return int(dt.replace(microsecond=0).timestamp() * 1000)


def _overnight(ss, se):
    return se <= ss


def _att_date(pi_dt, ss, se):
    """Attendance date per R1."""
    ist = pi_dt.astimezone(IST)
    d = ist.date()
    if _overnight(ss, se):
        se_h, se_m = map(int, se.split(":"))
        if ist.hour * 3600 + ist.minute * 60 + ist.second < se_h * 3600 + se_m * 60:
            d -= timedelta(days=1)
    return d.isoformat()


def _ss_dt(ad, ss):
    """Shift-start datetime on attendance date *ad*."""
    d = date.fromisoformat(ad)
    h, m = map(int, ss.split(":"))
    return datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)


def _se_dt(ad, ss, se):
    """Shift-end datetime (next day for overnight)."""
    d = date.fromisoformat(ad)
    h, m = map(int, se.split(":"))
    if _overnight(ss, se):
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)


def _late(pi, ss, ad):
    """Late minutes per R2."""
    diff = int((pi - _ss_dt(ad, ss)).total_seconds())
    return diff // 60 if diff > 600 else 0


def _wh(pi, po):
    """Work hours per R4."""
    return _h((po - pi).total_seconds() / 3600, 2)


def _ot(po, ss, se, ad):
    """Overtime minutes per R3."""
    diff = int((po - _se_dt(ad, ss, se)).total_seconds())
    mins = diff // 60
    return mins if mins >= 30 else 0


def _hd(wh):
    """Half-day per R5."""
    return wh < 4.50


def _month_bounds(m):
    y, mo = int(m[:4]), int(m[5:7])
    _, last = monthrange(y, mo)
    return f"{y:04d}-{mo:02d}-01", f"{y:04d}-{mo:02d}-{last:02d}"


def _weekdays(s, e):
    if s > e:
        return 0
    n, d = 0, s
    while d <= e:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def _vdate(s):
    try:
        date.fromisoformat(s)
    except Exception:
        raise HTTPException(422, detail=f"Invalid date: {s}")


# ───────────────────────── serialisation ─────────────────────────── #
def _fe(doc):
    """Format employee for API."""
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": _dt2ms(doc["created_at"]),
    }


def _fr(doc):
    """Format attendance record for API."""
    return {
        "emp_code": doc["emp_code"], "date": doc["date"],
        "status": doc["status"],
        "punch_in": _dt2ms(doc.get("punch_in")),
        "punch_out": _dt2ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [_fh(e) for e in doc.get("history", [])],
    }


def _fh(entry):
    """Format one history entry."""
    r = {"at": _dt2ms(entry["at"]), "by": entry["by"],
         "reason": entry["reason"], "changes": {}}
    for f, c in entry["changes"].items():
        if f in ("punch_in", "punch_out"):
            r["changes"][f] = {
                "from": _dt2ms(c["from"]) if isinstance(c["from"], datetime) else c["from"],
                "to": _dt2ms(c["to"]) if isinstance(c["to"], datetime) else c["to"],
            }
        else:
            r["changes"][f] = {"from": c["from"], "to": c["to"]}
    return r


# ───────────────────────────── models ────────────────────────────── #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInBody(BaseModel):
    emp_code: str
    punched_at: Any = None
    status: Any = "PRESENT"


class PunchOutBody(BaseModel):
    emp_code: str
    punched_at: Any = None


class RegBody(BaseModel):
    status: Optional[str] = None
    punch_in: Any = None
    punch_out: Any = None
    reason: str
    regularized_by: str


# ──────────────────────────── health ─────────────────────────────── #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")


# ─────────────────────────── employees ───────────────────────────── #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if not EMP_RE.match(body.emp_code):
        raise HTTPException(422, detail="emp_code format invalid")
    if not body.name or len(body.name) > 100:
        raise HTTPException(422, detail="name must be 1-100 chars")
    if not EMAIL_RE.match(body.email) or len(body.email) > 120:
        raise HTTPException(422, detail="invalid email")
    if not body.department or len(body.department) > 50:
        raise HTTPException(422, detail="department must be 1-50 chars")
    if not TIME_RE.match(body.shift_start):
        raise HTTPException(422, detail="invalid shift_start")
    if not TIME_RE.match(body.shift_end):
        raise HTTPException(422, detail="invalid shift_end")
    if body.shift_start == body.shift_end:
        raise HTTPException(422, detail="shift_start must differ from shift_end")
    try:
        date.fromisoformat(body.joined_on)
    except Exception:
        raise HTTPException(422, detail="invalid joined_on")
    doc = {
        "emp_code": body.emp_code, "name": body.name, "email": body.email,
        "department": body.department, "shift_start": body.shift_start,
        "shift_end": body.shift_end, "joined_on": body.joined_on,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    return _fe(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [_fe(e) for e in items], "total": total,
            "page": page, "page_size": page_size}


# ────────────────────────── attendance ───────────────────────────── #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInBody):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    st = body.status
    if not isinstance(st, str) or st not in PRESENCE:
        raise HTTPException(422, detail="status must be PRESENT, WFH or ON_DUTY")
    if body.punched_at is not None:
        _vms(body.punched_at, "punched_at")
        ts = _ms2dt(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    ad = _att_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": ad, "status": st,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": _late(ts, emp["shift_start"], ad),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")
    return _fr(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutBody):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    if body.punched_at is not None:
        _vms(body.punched_at, "punched_at")
        ts = _ms2dt(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts, "$ne": None}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, detail="No punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, detail="Already punched out")
    if ts <= rec["punch_in"]:
        raise HTTPException(422, detail="punch_out must be after punch_in")
    if (ts - rec["punch_in"]).total_seconds() > 86400:
        raise HTTPException(422, detail="punch_out more than 24h after punch_in")
    wh = _wh(rec["punch_in"], ts)
    ot = _ot(ts, emp["shift_start"], emp["shift_end"], rec["date"])
    hd = _hd(wh)
    result = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": wh,
                  "overtime_minutes": ot, "half_day": hd}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, detail="Already punched out (concurrent)")
    return _fr(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        _vdate(date_from)
    if date_to:
        _vdate(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")
    if status and status not in ALL_STATUS:
        raise HTTPException(422, detail=f"Invalid status: {status}")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [_fr(d) for d in docs], "total": total,
            "page": page, "page_size": page_size}


# ──────────────────────── regularize (PATCH) ─────────────────────── #
@app.patch("/attendance/{emp_code}/{att_date}")
def regularize(emp_code: str, att_date: str, body: RegBody):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    _vdate(att_date)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if not rec:
        raise HTTPException(404, detail="No record for this date")
    if len(body.reason) < 5 or len(body.reason) > 200:
        raise HTTPException(422, detail="reason must be 5-200 chars")
    if not body.regularized_by or len(body.regularized_by) > 50:
        raise HTTPException(422, detail="regularized_by must be 1-50 chars")

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }
    ns = body.status if body.status is not None else old["status"]
    if ns not in ALL_STATUS:
        raise HTTPException(422, detail=f"Invalid status: {ns}")
    pi_ms = _vms(body.punch_in, "punch_in") if body.punch_in is not None else None
    po_ms = _vms(body.punch_out, "punch_out") if body.punch_out is not None else None

    if ns in ("ABSENT", "LEAVE"):
        if pi_ms is not None or po_ms is not None:
            raise HTTPException(422, detail="Cannot supply punch times with ABSENT/LEAVE")
        npi, npo, nwh, nl, no_, nhd = None, None, None, 0, 0, False
    else:
        npi = _ms2dt(pi_ms) if pi_ms else old["punch_in"]
        npo = _ms2dt(po_ms) if po_ms else old["punch_out"]
        if npi is None:
            raise HTTPException(422, detail="Presence status requires punch_in")
        if pi_ms is not None:
            if _att_date(npi, emp["shift_start"], emp["shift_end"]) != att_date:
                raise HTTPException(422, detail="punch_in not on this attendance date")
        if npo is not None:
            if npo <= npi:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (npo - npi).total_seconds() > 86400:
                raise HTTPException(422, detail="punch_out more than 24h after punch_in")
        nl = _late(npi, emp["shift_start"], att_date)
        if npo is not None:
            nwh = _wh(npi, npo)
            no_ = _ot(npo, emp["shift_start"], emp["shift_end"], att_date)
            nhd = _hd(nwh)
        else:
            nwh, no_, nhd = None, 0, False

    new = {"status": ns, "punch_in": npi, "punch_out": npo,
           "work_hours": nwh, "late_minutes": nl,
           "overtime_minutes": no_, "half_day": nhd}
    changes = {}
    for f in ("status", "punch_in", "punch_out", "work_hours",
              "late_minutes", "overtime_minutes", "half_day"):
        if new[f] != old[f]:
            changes[f] = {"from": old[f], "to": new[f]}
    if not changes:
        raise HTTPException(422, detail="No changes detected")

    he = {"at": datetime.now(timezone.utc).replace(microsecond=0),
          "by": body.regularized_by, "reason": body.reason, "changes": changes}
    hl = len(rec.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "history": {"$size": hl}},
        {"$set": {k: new[k] for k in new}, "$push": {"history": he}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, detail="Concurrent modification")
    return _fr(result)


# ──────────────────────── analytics: monthly ─────────────────────── #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    fd, ld = _month_bounds(month)
    joined = date.fromisoformat(emp["joined_on"])
    wd = _weekdays(max(date.fromisoformat(fd), joined), date.fromisoformat(ld))

    pipe = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": fd, "$lte": ld}}},
        {"$addFields": {
            "_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            "_pres": {"$in": ["$status", PRESENCE_LIST]},
            "_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$_pres", {"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": ["$_late", 1, 0]}},
            "tlm": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "tom": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipe))
    r = rows[0] if rows else {"present_days": 0, "leave_days": 0,
                               "late_count": 0, "tlm": 0, "tom": 0}
    return {
        "emp_code": emp_code, "month": month, "working_days": wd,
        "present_days": r["present_days"], "leave_days": r["leave_days"],
        "late_count": r["late_count"],
        "total_late_minutes": r["tlm"], "total_overtime_minutes": r["tom"],
        "attendance_pct": _h(r["present_days"] / wd * 100, 2) if wd > 0 else None,
    }


# ──────────────────── analytics: dept summary ────────────────────── #
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    fd, ld = _month_bounds(month)
    # headcount
    hq = {"joined_on": {"$lte": ld}}
    if department:
        hq["department"] = department
    hc_pipe = [{"$match": hq}, {"$group": {"_id": "$department", "hc": {"$sum": 1}}}]
    hcmap = {r["_id"]: r["hc"] for r in db.employees.aggregate(hc_pipe)}
    if not hcmap:
        return {"month": month, "items": []}

    # attendance stats
    sp: list = [
        {"$match": {"date": {"$gte": fd, "$lte": ld}}},
        {"$lookup": {"from": "employees", "localField": "emp_code",
                     "foreignField": "emp_code", "as": "_e"}},
        {"$unwind": "$_e"},
    ]
    if department:
        sp.append({"$match": {"_e.department": department}})
    sp.extend([
        {"$addFields": {
            "_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            "_pres": {"$in": ["$status", PRESENCE_LIST]},
        }},
        {"$group": {
            "_id": "$_e.department",
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$_pres", {"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                0]}},
            "_whs": {"$sum": {"$cond": [
                {"$and": ["$_pres", {"$ne": ["$work_hours", None]}]},
                "$work_hours", 0]}},
            "_whc": {"$sum": {"$cond": [
                {"$and": ["$_pres", {"$ne": ["$work_hours", None]}]},
                1, 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                "$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ])
    smap = {r["_id"]: r for r in db.attendance_logs.aggregate(sp)}

    items = []
    for dept in sorted(hcmap):
        s = smap.get(dept, {})
        whc = s.get("_whc", 0)
        items.append({
            "department": dept, "headcount": hcmap[dept],
            "present_days": s.get("present_days", 0),
            "avg_work_hours": _h(s["_whs"] / whc, 2) if whc > 0 else None,
            "late_count": s.get("late_count", 0),
            "total_late_minutes": s.get("total_late_minutes", 0),
            "leave_count": s.get("leave_count", 0),
            "on_duty_count": s.get("on_duty_count", 0),
        })
    return {"month": month, "items": items}


# ──────────────────── analytics: leaderboard ─────────────────────── #
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    fd, ld = _month_bounds(month)
    pipe: list = [
        {"$match": {"date": {"$gte": fd, "$lte": ld}}},
        {"$group": {
            "_id": "$emp_code",
            "tlm": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "lc": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"tlm": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "_e"}},
        {"$unwind": "$_e"},
    ]
    if department:
        pipe.append({"$match": {"_e.department": department}})
    pipe.extend([
        {"$project": {"emp_code": "$_id", "name": "$_e.name",
                      "department": "$_e.department",
                      "total_late_minutes": "$tlm", "late_count": "$lc", "_id": 0}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    rows = list(db.attendance_logs.aggregate(pipe))
    return {"month": month, "items": [
        {"rank": r["rank"], "emp_code": r["emp_code"], "name": r["name"],
         "department": r["department"],
         "total_late_minutes": r["total_late_minutes"],
         "late_count": r["late_count"]} for r in rows
    ]}


# ─────────────────────── analytics: trend ────────────────────────── #
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    _vdate(from_date); _vdate(to_date)
    if to_date < from_date:
        raise HTTPException(422, detail="to must be >= from")
    fd, td = date.fromisoformat(from_date), date.fromisoformat(to_date)
    if (td - fd).days > 91:
        raise HTTPException(422, detail="Range exceeds 92 days")
    emps = list(db.employees.find({"department": department},
                                  {"emp_code": 1, "joined_on": 1, "_id": 0}))
    if not emps:
        raise HTTPException(404, detail="Department not found")
    ec = [e["emp_code"] for e in emps]
    jd = sorted(date.fromisoformat(e["joined_on"]) for e in emps)

    f_utc = datetime(fd.year, fd.month, fd.day, tzinfo=timezone.utc)
    t_utc = datetime(td.year, td.month, td.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipe = [
        {"$match": {"emp_code": {"$in": ec}, "date": {"$gte": from_date, "$lte": to_date}}},
        {"$addFields": {
            "_pv": {"$cond": [{"$in": ["$status", PRESENCE_LIST]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]},
            "_lt": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]},
        }},
        {"$group": {"_id": "$date",
                    "present_count": {"$sum": "$_pv"},
                    "late_count": {"$sum": "$_lt"}}},
        {"$addFields": {"date_obj": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "date_obj",
                      "range": {"step": 1, "unit": "day",
                                "bounds": [f_utc, t_utc]}}},
        {"$addFields": {
            "date": {"$ifNull": ["$_id",
                     {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}}]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$sort": {"date_obj": 1}},
        {"$project": {"_id": 0, "date_obj": 0}},
    ]
    rows = list(db.attendance_logs.aggregate(pipe))

    for r in rows:
        d = date.fromisoformat(r["date"])
        r["is_working_day"] = d.weekday() < 5
        r["headcount"] = bisect_right(jd, d)
        r["attendance_rate"] = (
            _h(r["present_count"] / r["headcount"], 4)
            if r["is_working_day"] and r["headcount"] > 0 else None
        )
    for i, r in enumerate(rows):
        w = [rows[j]["attendance_rate"]
             for j in range(max(0, i - 6), i + 1)
             if rows[j]["attendance_rate"] is not None]
        r["moving_avg_7d"] = _h(sum(w) / len(w), 4) if w else None

    return {"department": department, "items": [
        {"date": r["date"], "is_working_day": r["is_working_day"],
         "headcount": r["headcount"], "present_count": r["present_count"],
         "late_count": r["late_count"], "attendance_rate": r["attendance_rate"],
         "moving_avg_7d": r["moving_avg_7d"]} for r in rows
    ]}


# ────────────────────────── admin: explain ───────────────────────── #
EXPLAIN_ENDPOINTS = {
    "attendance_list", "employee_monthly", "department_summary",
    "late_leaderboard", "department_trend",
}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in EXPLAIN_ENDPOINTS:
        raise HTTPException(422, detail=f"Unknown endpoint: {endpoint}")

    def _explain_agg(coll_name, pipeline):
        return db.command("explain",
                          {"aggregate": coll_name, "pipeline": pipeline, "cursor": {}},
                          verbosity="executionStats")

    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        ex = db.command("explain", {
            "find": "attendance_logs", "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size, "limit": page_size,
        }, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": ex}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month required")
        fd, ld = _month_bounds(month)
        pipe = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": fd, "$lte": ld}}},
            {"$group": {"_id": None, "c": {"$sum": 1}}},
        ]
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _explain_agg("attendance_logs", pipe)}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month required")
        fd, ld = _month_bounds(month)
        mq: dict = {"date": {"$gte": fd, "$lte": ld}}
        pipe = [
            {"$match": mq},
            {"$lookup": {"from": "employees", "localField": "emp_code",
                         "foreignField": "emp_code", "as": "_e"}},
            {"$unwind": "$_e"},
        ]
        if department:
            pipe.append({"$match": {"_e.department": department}})
        pipe.append({"$group": {"_id": "$_e.department", "c": {"$sum": 1}}})
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _explain_agg("attendance_logs", pipe)}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month required")
        fd, ld = _month_bounds(month)
        pipe = [
            {"$match": {"date": {"$gte": fd, "$lte": ld}}},
            {"$group": {"_id": "$emp_code",
                        "tlm": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}},
        ]
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _explain_agg("attendance_logs", pipe)}

    # department_trend
    if not department or not from_date or not to_date:
        raise HTTPException(422, detail="department, from, and to required")
    ec = db.employees.distinct("emp_code", {"department": department})
    pipe = [
        {"$match": {"emp_code": {"$in": ec},
                    "date": {"$gte": from_date, "$lte": to_date}}},
        {"$group": {"_id": "$date", "c": {"$sum": 1}}},
    ]
    return {"endpoint": endpoint, "collection": "attendance_logs",
            "explain": _explain_agg("attendance_logs", pipe)}
