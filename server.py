#!/usr/bin/env python3
"""Server for the SCAU YSYX Study Tracker."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import math
import mimetypes
import os
import secrets
import socket
import sys
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
DB_FILE = Path(
    os.environ.get("YSYX_DB_FILE", str(ROOT / "ysyx-study-tracker.sqlite3"))
)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123456")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
RESET_PASSWORD = "123456"
MAX_JSON_BODY_BYTES = 64 * 1024
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DATA_LOCK = threading.Lock()
USER_SESSIONS: dict[str, str] = {}
ADMIN_SESSIONS: set[str] = set()


def default_state() -> dict:
    return {"students": {}, "users": {}}


def now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat()


def init_db(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS users (phone TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, class_name TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS students (name TEXT PRIMARY KEY, phone TEXT NOT NULL, class_name TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS weeks (student_name TEXT NOT NULL, week_key TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '', running_start INTEGER, PRIMARY KEY(student_name, week_key));
      CREATE TABLE IF NOT EXISTS time_records (id TEXT PRIMARY KEY, student_name TEXT NOT NULL, week_key TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER, manual INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '');
      CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, expires_at INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS idx_records_student ON time_records(student_name, start_ms);
    """)


def write_normalized(db: sqlite3.Connection, value: dict) -> None:
    seen_users, seen_students, seen_weeks, seen_records = set(), set(), set(), set()
    for phone, user in value.get("users", {}).items():
        if isinstance(user, dict):
            name = str(user.get("name", "")).strip() or phone
            seen_users.add(phone)
            db.execute("INSERT INTO users(phone,name,class_name,password_hash,created_at) VALUES(?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET name=excluded.name,class_name=excluded.class_name,password_hash=excluded.password_hash,created_at=excluded.created_at", (phone, name, str(user.get("className", "")), str(user.get("passwordHash", "")), str(user.get("createdAt", ""))))
    for name, student in value.get("students", {}).items():
        if not isinstance(student, dict): continue
        seen_students.add(name)
        db.execute("INSERT INTO students VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET phone=excluded.phone,class_name=excluded.class_name", (name, str(student.get("phone", "")), str(student.get("className", ""))))
        for week_key, week in (student.get("weeks", {}) or {}).items():
            if not isinstance(week, dict): continue
            seen_weeks.add((name, week_key))
            db.execute("INSERT INTO weeks VALUES(?,?,?,?) ON CONFLICT(student_name,week_key) DO UPDATE SET progress=excluded.progress,running_start=excluded.running_start", (name, week_key, str(week.get("progress", "")), week.get("runningStart")))
            for record in week.get("timeRecords", []) or []:
                if isinstance(record, dict):
                    rid = str(record.get("id", secrets.token_urlsafe(12))); seen_records.add(rid)
                    record_values = (rid, name, week_key, int(record.get("start", 0)), record.get("end"), int(bool(record.get("manual"))), str(record.get("summary", "")))
                    db.execute("INSERT INTO time_records VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET student_name=excluded.student_name,week_key=excluded.week_key,start_ms=excluded.start_ms,end_ms=excluded.end_ms,manual=excluded.manual,summary=excluded.summary", record_values)
    if seen_users: db.execute("DELETE FROM users WHERE phone NOT IN (%s)" % ",".join("?" * len(seen_users)), tuple(seen_users))
    else: db.execute("DELETE FROM users")
    if seen_students: db.execute("DELETE FROM students WHERE name NOT IN (%s)" % ",".join("?" * len(seen_students)), tuple(seen_students))
    else: db.execute("DELETE FROM students")
    if seen_weeks: db.execute("DELETE FROM weeks WHERE (student_name,week_key) NOT IN (%s)" % ",".join(["(?,?)"] * len(seen_weeks)), tuple(x for pair in seen_weeks for x in pair))
    else: db.execute("DELETE FROM weeks")
    if seen_records: db.execute("DELETE FROM time_records WHERE id NOT IN (%s)" % ",".join("?" * len(seen_records)), tuple(seen_records))
    else: db.execute("DELETE FROM time_records")


def read_state() -> dict:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_FILE) as db:
        init_db(db)
        value = default_state()
        for phone, name, cls, pwd, created in db.execute("SELECT phone,name,class_name,password_hash,created_at FROM users"):
            value["users"][phone] = {"phone": phone, "name": name, "className": cls, "passwordHash": pwd, "createdAt": created}
        for name, phone, cls in db.execute("SELECT name,phone,class_name FROM students"):
            value["students"][name] = {"phone": phone, "className": cls, "weeks": {}}
        for name, week_key, progress, running in db.execute("SELECT student_name,week_key,progress,running_start FROM weeks"):
            value["students"].setdefault(name, {"weeks": {}})["weeks"][week_key] = {"progress": progress, "runningStart": running, "timeRecords": []}
        for rid, name, week_key, start, end, manual, summary in db.execute("SELECT id,student_name,week_key,start_ms,end_ms,manual,summary FROM time_records"):
            value["students"].setdefault(name, {"weeks": {}})["weeks"].setdefault(week_key, {"timeRecords": []}).setdefault("timeRecords", []).append({"id": rid, "start": start, "end": end, "manual": bool(manual), "summary": summary})
        return value


def write_state(value: dict) -> None:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_FILE) as db:
        init_db(db)
        write_normalized(db, value)
        db.commit()


def read_overview() -> list[dict]:
    """Read only roster aggregates; historical records stay in SQLite."""
    week_key = current_week_key()
    now_ms = int(time.time() * 1000)
    current_now = datetime.now(SHANGHAI_TZ)
    current_week_start = week_start_ms(current_now)
    current_week_end = current_week_start + 7 * 24 * 60 * 60 * 1000
    current_day_start = day_start_ms(current_now)
    current_day_end = current_day_start + 24 * 60 * 60 * 1000
    with sqlite3.connect(DB_FILE) as db:
        init_db(db)
        rows = []
        for name, cls in db.execute("SELECT name,class_name FROM students ORDER BY name"):
            total = db.execute("SELECT COALESCE(SUM(end_ms-start_ms),0) FROM time_records WHERE student_name=? AND end_ms IS NOT NULL", (name,)).fetchone()[0]
            week = db.execute("SELECT COALESCE(SUM(end_ms-start_ms),0) FROM time_records WHERE student_name=? AND week_key=? AND end_ms IS NOT NULL", (name, week_key)).fetchone()[0]
            day = sum(
                duration_overlap(int(start), int(end), current_day_start, current_day_end)
                for start, end in db.execute(
                    "SELECT start_ms,end_ms FROM time_records WHERE student_name=? AND end_ms IS NOT NULL",
                    (name,),
                )
                if end is not None and int(end) > int(start)
            )
            running_rows = db.execute("SELECT running_start FROM weeks WHERE student_name=? AND running_start IS NOT NULL", (name,)).fetchall()
            running_starts = [int(row[0]) for row in running_rows if row[0] and 0 < int(row[0]) <= now_ms]
            running_start = max(running_starts, default=None)
            running_ms = sum(max(0, now_ms - start) for start in running_starts)
            week_running_ms = sum(duration_overlap(start, now_ms, current_week_start, current_week_end) for start in running_starts)
            day_running_ms = sum(duration_overlap(start, now_ms, current_day_start, current_day_end) for start in running_starts)
            rows.append({"name": name, "className": cls, "totalMs": total + running_ms, "weekMs": week + week_running_ms, "dayMs": day + day_running_ms, "isStudying": bool(running_start), "runningStart": running_start, "runningDurationMs": running_ms})
        return rows


def read_user_and_student(phone: str) -> tuple[dict, dict] | None:
    """Load one account and its records without materializing the whole database."""
    with sqlite3.connect(DB_FILE) as db:
        init_db(db)
        row = db.execute("SELECT name,class_name,password_hash,created_at FROM users WHERE phone=?", (phone,)).fetchone()
        if not row: return None
        name, cls, password_hash, created_at = row
        user = {"phone": phone, "name": name, "className": cls, "passwordHash": password_hash, "createdAt": created_at}
        student = {"phone": phone, "className": cls, "weeks": {}}
        for week_key, progress, running in db.execute("SELECT week_key,progress,running_start FROM weeks WHERE student_name=?", (name,)):
            student["weeks"][week_key] = {"progress": progress, "runningStart": running, "timeRecords": []}
        for rid, week_key, start, end, manual, summary in db.execute("SELECT id,week_key,start_ms,end_ms,manual,summary FROM time_records WHERE student_name=? ORDER BY start_ms", (name,)):
            week = student["weeks"].setdefault(week_key, {"progress": "", "runningStart": None, "timeRecords": []})
            week["timeRecords"].append({"id": rid, "start": start, "end": end, "manual": bool(manual), "summary": summary})
        return user, student


def scoped_state(phone: str) -> dict:
    loaded = read_user_and_student(phone)
    if not loaded: return default_state()
    user, student = loaded
    name = user["name"]
    return {"users": {name: public_user(phone, user, include_phone=False)}, "students": {name: without_password_hash(student)}}


def save_progress(phone: str, progress: str) -> None:
    loaded = read_user_and_student(phone)
    if not loaded: return
    name = loaded[0]["name"]
    with sqlite3.connect(DB_FILE) as db:
        init_db(db)
        db.execute("INSERT INTO weeks(student_name,week_key,progress,running_start) VALUES(?,?,?,NULL) ON CONFLICT(student_name,week_key) DO UPDATE SET progress=excluded.progress", (name, current_week_key(), progress[:2000]))
        db.commit()


def toggle_timer(phone: str, summary: str) -> dict:
    loaded = read_user_and_student(phone)
    if not loaded: raise LookupError("student unauthorized")
    name = loaded[0]["name"]
    now_ms = int(time.time() * 1000)
    with sqlite3.connect(DB_FILE) as db:
        init_db(db)
        db.execute("BEGIN IMMEDIATE")
        active = db.execute("SELECT week_key,running_start FROM weeks WHERE student_name=? AND running_start IS NOT NULL ORDER BY running_start DESC LIMIT 1", (name,)).fetchone()
        if not active:
            db.execute("INSERT INTO weeks(student_name,week_key,progress,running_start) VALUES(?,?,?,?) ON CONFLICT(student_name,week_key) DO UPDATE SET running_start=excluded.running_start", (name, current_week_key(), "", now_ms))
            db.commit(); return {"action": "started", "discarded": False, "records": []}
        if not summary: raise ValueError("结束打卡前请填写本次学习内容")
        start_ms = int(active[1]); db.execute("UPDATE weeks SET running_start=NULL WHERE student_name=?", (name,))
        records = []
        if now_ms - start_ms >= 60_000:
            cursor = start_ms
            while cursor < now_ms:
                moment = datetime.fromtimestamp(cursor / 1000, SHANGHAI_TZ)
                key = current_week_key(moment)
                monday = moment.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=moment.weekday())
                end = min(now_ms, int((monday + timedelta(days=7)).timestamp() * 1000))
                rid = "record-" + secrets.token_urlsafe(16)
                db.execute("INSERT OR IGNORE INTO weeks(student_name,week_key,progress,running_start) VALUES(?,?,?,NULL)", (name, key, ""))
                db.execute("INSERT INTO time_records VALUES(?,?,?,?,?,?,?)", (rid, name, key, cursor, end, 0, summary[:500]))
                records.append({"id": rid, "start": cursor, "end": end, "manual": False, "summary": summary[:500]}); cursor = end
        db.commit()
        return {"action": "stopped", "discarded": now_ms - start_ms < 60_000, "records": records}


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    rounds = 120_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return "pbkdf2_sha256$" + str(rounds) + "$" + b64(salt) + "$" + b64(digest)


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds_text, salt_text, digest_text = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_text)
        salt = unb64(salt_text)
        expected = unb64(digest_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, base64.binascii.Error):
        return False


def clean_phone(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def public_user(phone: str, user: dict, include_phone: bool = True) -> dict:
    result = {
        "name": str(user.get("name", "")),
        "className": str(user.get("className", "")),
        "createdAt": user.get("createdAt", ""),
    }
    if include_phone:
        result["phone"] = phone
    return result


def public_state(state: dict) -> dict:
    users = state.get("users", {})
    students = {}
    for name, student in state.get("students", {}).items():
        if isinstance(student, dict):
            safe_student = deepcopy(student)
            safe_student.pop("phone", None)
            for week in safe_student.get("weeks", {}).values():
                if isinstance(week, dict):
                    week.pop("notes", None)
                    week.pop("questions", None)
                    week.pop("deletedNoteIds", None)
            students[name] = safe_student
    return {
        "students": students,
        "users": {
            str(user.get("name", "")).strip() or phone: public_user(phone, user, include_phone=False)
            for phone, user in users.items()
            if isinstance(phone, str) and isinstance(user, dict)
        },
    }


def merge_student(existing: dict, incoming: dict, actor_name: str = "") -> dict:
    """Merge one student while keeping saved goals immutable."""
    existing = existing if isinstance(existing, dict) else {}
    incoming = incoming if isinstance(incoming, dict) else {}
    result = deepcopy(existing)
    result.update(deepcopy(incoming))
    existing_weeks = existing.get("weeks", {})
    incoming_weeks = incoming.get("weeks", {})
    if not isinstance(existing_weeks, dict):
        existing_weeks = {}
    if not isinstance(incoming_weeks, dict):
        incoming_weeks = {}
    merged_weeks = deepcopy(existing_weeks)
    for week_key, incoming_week in incoming_weeks.items():
        if not isinstance(incoming_week, dict):
            continue
        existing_week = existing_weeks.get(week_key, {})
        if not isinstance(existing_week, dict):
            existing_week = {}
        existing_time_records = existing_week.get("timeRecords", [])
        if not isinstance(existing_time_records, list):
            existing_time_records = []
        existing_running_start = existing_week.get("runningStart")
        merged_week = deepcopy(existing_week)
        merged_week.update(deepcopy(incoming_week))
        existing_deleted_note_ids = existing_week.get("deletedNoteIds", [])
        incoming_deleted_note_ids = incoming_week.get("deletedNoteIds", [])
        deleted_note_ids = {
            str(note_id).strip()
            for note_id in [
                *(existing_deleted_note_ids if isinstance(existing_deleted_note_ids, list) else []),
                *(incoming_deleted_note_ids if isinstance(incoming_deleted_note_ids, list) else []),
            ]
            if str(note_id).strip()
        }
        if deleted_note_ids:
            merged_week["deletedNoteIds"] = sorted(deleted_note_ids)
        elif "deletedNoteIds" in existing_week or "deletedNoteIds" in incoming_week:
            merged_week["deletedNoteIds"] = []
        merged_week["timeRecords"] = deepcopy(existing_time_records)
        merged_week["runningStart"] = existing_running_start
        merged_week.pop("notes", None)
        merged_week.pop("questions", None)
        merged_week.pop("deletedNoteIds", None)
        existing_goal = existing_week.get("goal", {})
        incoming_goal = incoming_week.get("goal", {})
        existing_goal = existing_goal if isinstance(existing_goal, dict) else {}
        incoming_goal = incoming_goal if isinstance(incoming_goal, dict) else {}
        if existing_goal.get("text") or existing_goal.get("locked"):
            protected_goal = deepcopy(existing_goal)
            protected_goal["locked"] = True
            if not existing_goal.get("done") and incoming_goal.get("done"):
                protected_goal["done"] = True
                protected_goal["completedAt"] = incoming_goal.get("completedAt", "")
                protected_goal["updatedAt"] = incoming_goal.get("updatedAt", "")
            merged_week["goal"] = protected_goal
        elif incoming_goal.get("text"):
            locked_goal = deepcopy(incoming_goal)
            locked_goal["locked"] = True
            merged_week["goal"] = locked_goal
        merged_weeks[week_key] = merged_week
    result["weeks"] = merged_weeks
    return result


def create_student_if_missing(state: dict, phone: str, user: dict) -> None:
    name = str(user.get("name", "")).strip() or phone
    students = state.setdefault("students", {})
    student = students.setdefault(name, {"createdAt": now_iso(), "weeks": {}})
    student["phone"] = phone
    student["className"] = str(user.get("className", ""))


def authenticated_student(state: dict, token: str) -> tuple[str, str, dict] | None:
    """Resolve a student session to its persisted phone, name, and user record."""
    phone = user_from_token(token)
    users = state.get("users", {})
    if not phone or not isinstance(users, dict):
        return None
    user = users.get(phone)
    if not isinstance(user, dict):
        return None
    name = str(user.get("name", "")).strip() or phone
    return phone, name, user


def item_id(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("id", "")).strip()


def merge_notes(existing: object, incoming: object, deleted_ids: set[str]) -> list:
    """Append new notes while preserving edits/deletions made by the server."""
    result = []
    known_ids = set()
    if isinstance(existing, list):
        for note in existing:
            if not isinstance(note, dict):
                continue
            saved_note = deepcopy(note)
            note_id = item_id(saved_note)
            if not note_id:
                note_id = "note-" + secrets.token_urlsafe(16)
                saved_note["id"] = note_id
            if note_id in deleted_ids or note_id in known_ids:
                continue
            known_ids.add(note_id)
            result.append(saved_note)
    if isinstance(incoming, list):
        for note in incoming:
            if not isinstance(note, dict):
                continue
            note_id = item_id(note)
            if not note_id or note_id in deleted_ids or note_id in known_ids:
                continue
            known_ids.add(note_id)
            result.append(deepcopy(note))
    return result


def merge_replies(existing: object, incoming: object, actor_name: str = "") -> list:
    """Append new replies without allowing existing replies to be edited or removed."""
    result = deepcopy(existing) if isinstance(existing, list) else []
    known_ids = {item_id(reply) for reply in result if item_id(reply)}
    if not isinstance(incoming, list):
        return result
    for reply in incoming:
        if not isinstance(reply, dict):
            continue
        reply_id = item_id(reply)
        if reply_id and reply_id in known_ids:
            continue
        saved_reply = deepcopy(reply)
        if not reply_id:
            reply_id = "reply-" + secrets.token_urlsafe(12)
            saved_reply["id"] = reply_id
        if actor_name:
            saved_reply["author"] = actor_name
        known_ids.add(reply_id)
        result.append(saved_reply)
    return result


def merge_question_threads(existing: object, incoming: object, actor_name: str = "") -> list:
    """Append questions/replies while preserving every existing question field."""
    result = deepcopy(existing) if isinstance(existing, list) else []
    question_indexes = {
        question_id: index
        for index, question in enumerate(result)
        if (question_id := item_id(question))
    }
    if not isinstance(incoming, list):
        return result

    for question in incoming:
        if not isinstance(question, dict):
            continue
        question_id = item_id(question)
        existing_index = question_indexes.get(question_id) if question_id else None
        if existing_index is None:
            saved_question = deepcopy(question)
            if not question_id:
                question_id = "question-" + secrets.token_urlsafe(12)
                saved_question["id"] = question_id
            if actor_name:
                saved_question["asker"] = actor_name
            replies = saved_question.get("replies")
            saved_question["replies"] = merge_replies([], replies, actor_name)
            question_indexes[question_id] = len(result)
            result.append(saved_question)
            continue

        saved_question = result[existing_index]
        if not isinstance(saved_question, dict):
            continue
        saved_question["replies"] = merge_replies(
            saved_question.get("replies"), question.get("replies"), actor_name
        )
    return result


def merge_questions_only(existing: dict, incoming: dict, actor_name: str = "") -> dict:
    """Apply only question additions/replies to another student's record."""
    result = deepcopy(existing) if isinstance(existing, dict) else {}
    existing_weeks = result.get("weeks", {})
    incoming_weeks = incoming.get("weeks", {}) if isinstance(incoming, dict) else {}
    if not isinstance(existing_weeks, dict):
        existing_weeks = {}
    if not isinstance(incoming_weeks, dict):
        return result

    merged_weeks = deepcopy(existing_weeks)
    for week_key, incoming_week in incoming_weeks.items():
        if not isinstance(week_key, str) or not isinstance(incoming_week, dict):
            continue
        incoming_questions = incoming_week.get("questions")
        if not isinstance(incoming_questions, list):
            continue
        existing_week = merged_weeks.get(week_key, {})
        if not isinstance(existing_week, dict):
            existing_week = {}
        merged_week = deepcopy(existing_week)
        merged_week["questions"] = merge_question_threads(
            existing_week.get("questions"), incoming_questions, actor_name
        )
        merged_weeks[week_key] = merged_week
    result["weeks"] = merged_weeks
    return result


def auth_header(headers) -> str:
    value = headers.get("Authorization", "")
    return value[7:].strip() if value.startswith("Bearer ") else ""


def user_from_token(token: str) -> str | None:
    if not token: return None
    with sqlite3.connect(DB_FILE) as db:
        init_db(db)
        row = db.execute("SELECT subject FROM sessions WHERE token=? AND kind='student' AND expires_at>?", (token, int(time.time()))).fetchone()
        return row[0] if row else None


def is_admin_token(token: str) -> bool:
    if not token: return False
    with sqlite3.connect(DB_FILE) as db:
        init_db(db)
        return bool(db.execute("SELECT 1 FROM sessions WHERE token=? AND kind='admin' AND expires_at>?", (token, int(time.time()))).fetchone())


def save_session(token: str, kind: str, subject: str) -> None:
    with sqlite3.connect(DB_FILE) as db:
        init_db(db)
        now = int(time.time())
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)", (token, kind, subject, now + 30 * 86400))
        db.commit()


def parse_json_body(
    handler: SimpleHTTPRequestHandler, max_bytes: int = MAX_JSON_BODY_BYTES
) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length < 0 or length > max_bytes:
        raise ValueError("JSON payload is too large")
    value = json.loads(handler.rfile.read(length).decode("utf-8")) if length else {}
    if not isinstance(value, dict):
        raise ValueError("JSON payload must be an object")
    return value


def ms(value: object) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def finite_epoch_ms(value: object) -> int | None:
    """Parse a non-negative, finite epoch-millisecond value."""
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return int(numeric)


def week_key_for_epoch_ms(value: int) -> str | None:
    try:
        moment = datetime.fromtimestamp(value / 1000, SHANGHAI_TZ)
    except (OverflowError, OSError, ValueError):
        return None
    return current_week_key(moment)


def valid_week_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text if parsed.strftime("%Y-%m-%d") == text else None


def valid_item_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def duration_overlap(start_ms: int, end_ms: int, period_start: int, period_end: int) -> int:
    return max(0, min(end_ms, period_end) - max(start_ms, period_start))


def week_start_ms(now: datetime) -> int:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    return int(start.timestamp() * 1000)


def day_start_ms(now: datetime) -> int:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def student_durations(student: dict) -> tuple[int, int, int]:
    now = datetime.now(SHANGHAI_TZ)
    current_ms = int(time.time() * 1000)
    week_start = week_start_ms(now)
    day_start = day_start_ms(now)
    day_end = day_start + 24 * 60 * 60 * 1000
    week_end = week_start + 7 * 24 * 60 * 60 * 1000
    total = week_total = day_total = 0
    weeks = student.get("weeks", {}) if isinstance(student, dict) else {}
    for week in weeks.values():
        if not isinstance(week, dict):
            continue
        for record in week.get("timeRecords", []) or []:
            if not isinstance(record, dict):
                continue
            start = ms(record.get("start"))
            end = ms(record.get("end"))
            if start is None or end is None or end <= start:
                continue
            total += end - start
            week_total += duration_overlap(start, end, week_start, week_end)
            day_total += duration_overlap(start, end, day_start, day_end)
        running_start = ms(week.get("runningStart"))
        if running_start is not None and current_ms > running_start:
            total += current_ms - running_start
            week_total += duration_overlap(running_start, current_ms, week_start, week_end)
            day_total += duration_overlap(running_start, current_ms, day_start, day_end)
    return total, week_total, day_total


def current_week_key(now: datetime | None = None) -> str:
    """Return the local Monday date used by the browser as the current week key."""
    local_now = now or datetime.now(SHANGHAI_TZ)
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=SHANGHAI_TZ)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(days=start.weekday())
    return start.date().isoformat()


def ensure_student_week(state: dict, phone: str, user: dict, week_key: str) -> dict:
    """Create or normalize one student's weekly record in a mutable state copy."""
    name = str(user.get("name", "")).strip() or phone
    students = state.get("students")
    if not isinstance(students, dict):
        students = {}
        state["students"] = students
    student = students.get(name)
    if not isinstance(student, dict):
        student = {"createdAt": now_iso(), "weeks": {}}
        students[name] = student
    student.setdefault("createdAt", now_iso())
    student["phone"] = phone
    student["className"] = str(user.get("className", ""))

    weeks = student.get("weeks")
    if not isinstance(weeks, dict):
        weeks = {}
        student["weeks"] = weeks
    week = weeks.get(week_key)
    if not isinstance(week, dict):
        week = {}
        weeks[week_key] = week

    defaults = {
        "mode": "time",
        "runningStart": None,
        "timeRecords": [],
        "goal": {
            "text": "",
            "createdAt": "",
            "done": False,
            "completedAt": "",
            "locked": False,
            "updatedAt": "",
        },
        "progress": "",
        "notes": [],
        "questions": [],
    }
    for key, default in defaults.items():
        if key not in week or key in {"timeRecords", "goal", "notes", "questions"}:
            current = week.get(key)
            if key in {"timeRecords", "notes", "questions"} and not isinstance(current, list):
                week[key] = deepcopy(default)
            elif key == "goal" and not isinstance(current, dict):
                week[key] = deepcopy(default)
            elif key not in week:
                week[key] = deepcopy(default)
    goal = week["goal"]
    for key, default in defaults["goal"].items():
        goal.setdefault(key, default)
    return week


def unique_time_record_id(state: dict) -> str:
    known_ids = set()
    students = state.get("students", {})
    if isinstance(students, dict):
        for student in students.values():
            weeks = student.get("weeks", {}) if isinstance(student, dict) else {}
            if not isinstance(weeks, dict):
                continue
            for week in weeks.values():
                records = week.get("timeRecords", []) if isinstance(week, dict) else []
                if isinstance(records, list):
                    known_ids.update(item_id(record) for record in records if item_id(record))
    while True:
        record_id = "record-" + secrets.token_urlsafe(16)
        if record_id not in known_ids:
            return record_id


def active_student_timer(student: dict, now_ms: int) -> tuple[str, int] | None:
    candidates = []
    weeks = student.get("weeks", {}) if isinstance(student, dict) else {}
    if not isinstance(weeks, dict):
        return None
    for week_key, week in weeks.items():
        if not isinstance(week, dict):
            continue
        running_start = finite_epoch_ms(week.get("runningStart"))
        if running_start is not None and 0 < running_start <= now_ms:
            candidates.append((str(week_key), running_start))
    return max(candidates, key=lambda item: item[1]) if candidates else None


def append_timed_session(
    state: dict, phone: str, user: dict, start_ms: int, end_ms: int, summary: str = ""
) -> list[dict]:
    """Append a completed timer, splitting it at Shanghai week boundaries."""
    records = []
    cursor = start_ms
    while cursor < end_ms:
        moment = datetime.fromtimestamp(cursor / 1000, SHANGHAI_TZ)
        week_key = current_week_key(moment)
        week_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start -= timedelta(days=week_start.weekday())
        next_week_ms = int((week_start + timedelta(days=7)).timestamp() * 1000)
        segment_end = min(end_ms, next_week_ms)
        if segment_end <= cursor:
            break
        week = ensure_student_week(state, phone, user, week_key)
        record = {
            "id": unique_time_record_id(state),
            "start": cursor,
            "end": segment_end,
            "manual": False,
            "summary": summary,
        }
        week["timeRecords"].append(record)
        records.append(record)
        cursor = segment_end
    return records


def interval_overlaps_week(week: dict, start_ms: int, end_ms: int, now_ms: int) -> bool:
    records = week.get("timeRecords", []) if isinstance(week, dict) else []
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            record_start = finite_epoch_ms(record.get("start"))
            record_end = finite_epoch_ms(record.get("end"))
            if (
                record_start is not None
                and record_end is not None
                and record_end > record_start
                and max(start_ms, record_start) < min(end_ms, record_end)
            ):
                return True
    running_start = finite_epoch_ms(week.get("runningStart")) if isinstance(week, dict) else None
    return bool(
        running_start is not None
        and 0 < running_start < now_ms
        and max(start_ms, running_start) < min(end_ms, now_ms)
    )


def active_timer_overlaps(student: dict, start_ms: int, end_ms: int, now_ms: int) -> bool:
    weeks = student.get("weeks", {}) if isinstance(student, dict) else {}
    if not isinstance(weeks, dict):
        return False
    for week in weeks.values():
        if not isinstance(week, dict):
            continue
        running_start = finite_epoch_ms(week.get("runningStart"))
        if (
            running_start is not None
            and 0 < running_start < now_ms
            and max(start_ms, running_start) < min(end_ms, now_ms)
        ):
            return True
    return False


def student_study_status(student: dict) -> dict:
    """Describe whether a student currently has any open timer."""
    week_key = current_week_key()
    weeks = student.get("weeks", {}) if isinstance(student, dict) else {}
    now_ms = int(time.time() * 1000)
    running_week_key = week_key
    running_start = None
    if isinstance(weeks, dict):
        for candidate_week_key, candidate_week in weeks.items():
            if not isinstance(candidate_week, dict):
                continue
            candidate_start = ms(candidate_week.get("runningStart"))
            if candidate_start is None or not 0 < candidate_start <= now_ms:
                continue
            if running_start is None or candidate_start > running_start:
                running_week_key = str(candidate_week_key)
                running_start = candidate_start
    is_studying = running_start is not None
    duration = max(0, now_ms - running_start) if is_studying else 0
    return {
        "weekKey": running_week_key,
        "isStudying": is_studying,
        "runningStart": running_start if is_studying else None,
        "runningDurationMs": duration,
    }


def without_password_hash(value: object) -> object:
    """Remove credentials from a copied response, including nested records."""
    if isinstance(value, dict):
        return {
            str(key): without_password_hash(item)
            for key, item in value.items()
            if str(key).lower() not in {"passwordhash", "password"}
        }
    if isinstance(value, list):
        return [without_password_hash(item) for item in value]
    return deepcopy(value)


def student_communications(state: dict, target_name: str) -> dict:
    """Collect a student's questions/replies across every public student profile."""
    students = state.get("students", {})
    as_asker = []
    as_author = []
    own_threads = []
    if not isinstance(students, dict):
        return {"asAsker": as_asker, "asAuthor": as_author, "ownThreads": own_threads}

    for profile_name, student in students.items():
        if not isinstance(student, dict):
            continue
        weeks = student.get("weeks", {})
        if not isinstance(weeks, dict):
            continue
        profile_label = str(profile_name)
        for week_key, week in weeks.items():
            if not isinstance(week, dict):
                continue
            questions = week.get("questions", [])
            if not isinstance(questions, list):
                continue
            for question in questions:
                if not isinstance(question, dict):
                    continue
                safe_question = without_password_hash(question)
                context = {"profileName": profile_label, "weekKey": str(week_key)}
                if profile_label == target_name:
                    own_threads.append({**context, "question": safe_question})
                if (
                    profile_label != target_name
                    and str(question.get("asker", "")).strip() == target_name
                ):
                    as_asker.append({
                        **context,
                        "role": "asker",
                        "question": safe_question,
                    })
                replies = question.get("replies", [])
                if not isinstance(replies, list):
                    continue
                for reply in replies:
                    if not isinstance(reply, dict):
                        continue
                    if (
                        profile_label == target_name
                        or str(reply.get("author", "")).strip() != target_name
                    ):
                        continue
                    as_author.append({
                        **context,
                        "role": "author",
                        "questionId": str(question.get("id", "")),
                        "questionText": str(question.get("text", "")),
                        "questionCreatedAt": question.get("createdAt", ""),
                        "reply": without_password_hash(reply),
                    })

    def communication_time(item: dict) -> str:
        if item.get("role") == "author":
            reply = item.get("reply", {})
            return str(reply.get("createdAt", "")) if isinstance(reply, dict) else ""
        question = item.get("question", {})
        return str(question.get("createdAt", "")) if isinstance(question, dict) else ""

    as_asker.sort(key=communication_time, reverse=True)
    as_author.sort(key=communication_time, reverse=True)
    own_threads.sort(key=communication_time, reverse=True)
    return {"asAsker": as_asker, "asAuthor": as_author, "ownThreads": own_threads}


def remove_student_communications(state: dict, target_name: str) -> None:
    """Remove a deleted student's questions and replies from other profiles."""
    students = state.get("students", {})
    if not isinstance(students, dict):
        return
    for profile_name, student in students.items():
        if profile_name == target_name or not isinstance(student, dict):
            continue
        weeks = student.get("weeks", {})
        if not isinstance(weeks, dict):
            continue
        for week in weeks.values():
            if not isinstance(week, dict):
                continue
            questions = week.get("questions", [])
            if not isinstance(questions, list):
                continue
            kept_questions = []
            for question in questions:
                if not isinstance(question, dict):
                    continue
                if str(question.get("asker", "")).strip() == target_name:
                    continue
                kept_question = deepcopy(question)
                replies = kept_question.get("replies", [])
                if isinstance(replies, list):
                    kept_question["replies"] = [
                        reply for reply in replies
                        if isinstance(reply, dict)
                        and str(reply.get("author", "")).strip() != target_name
                    ]
                kept_questions.append(kept_question)
            week["questions"] = kept_questions


def admin_student_detail(state: dict, phone: str) -> dict | None:
    """Build the complete, password-free detail payload for one student."""
    users = state.get("users", {})
    if not isinstance(users, dict):
        return None
    user = users.get(phone)
    if not isinstance(user, dict):
        return None
    name = str(user.get("name", "")).strip() or phone
    student = state.get("students", {}).get(name, {})
    if not isinstance(student, dict):
        student = {}
    total_ms, week_ms, day_ms = student_durations(student)
    status = student_study_status(student)
    safe_user = without_password_hash(public_user(phone, user))
    safe_student = without_password_hash(student)
    return {
        "profile": safe_user,
        "student": safe_student,
        "weeks": safe_student.get("weeks", {}) if isinstance(safe_student, dict) else {},
        "summary": {
            "totalMs": total_ms,
            "weekMs": week_ms,
            "dayMs": day_ms,
        },
        "status": status,
        "communications": student_communications(state, name),
    }


def export_rows(state: dict, sort_key: str) -> list[dict]:
    users = state.get("users", {})
    students = state.get("students", {})
    rows = []
    for phone, user in users.items():
        if not isinstance(user, dict):
            continue
        name = str(user.get("name", "")).strip() or phone
        student = students.get(name, {})
        total, week, day = student_durations(student)
        status = student_study_status(student)
        rows.append({
            "phone": phone,
            "name": name,
            "className": str(user.get("className", "")),
            "totalMs": total,
            "weekMs": week,
            "dayMs": day,
            "isStudying": status["isStudying"],
            "runningStart": status["runningStart"],
            "runningDurationMs": status["runningDurationMs"],
        })
    sort_field = {"total": "totalMs", "week": "weekMs", "day": "dayMs"}.get(sort_key, "totalMs")
    rows.sort(key=lambda row: row[sort_field], reverse=True)
    return rows


def export_time_records(state: dict) -> list[dict]:
    """Return every completed check-in across every student and week.

    A timer that crosses a Monday boundary is stored as one record per week,
    so each returned row represents the exact weekly segment that was counted.
    """
    users = state.get("users", {})
    students = state.get("students", {})
    records = []
    if not isinstance(users, dict) or not isinstance(students, dict):
        return records

    for phone, user in users.items():
        if not isinstance(user, dict):
            continue
        name = str(user.get("name", "")).strip() or str(phone)
        student = students.get(name, {})
        weeks = student.get("weeks", {}) if isinstance(student, dict) else {}
        if not isinstance(weeks, dict):
            continue
        for week_key, week in weeks.items():
            if not isinstance(week, dict) or not isinstance(week.get("timeRecords"), list):
                continue
            normalized_week_key = valid_week_key(week_key) or str(week_key)
            for record in week["timeRecords"]:
                if not isinstance(record, dict):
                    continue
                start_ms = finite_epoch_ms(record.get("start"))
                end_ms = finite_epoch_ms(record.get("end"))
                if start_ms is None or end_ms is None or end_ms <= start_ms:
                    continue
                records.append({
                    "phone": str(phone),
                    "name": name,
                    "className": str(user.get("className", "")),
                    "weekKey": normalized_week_key,
                    "recordId": item_id(record),
                    "start": start_ms,
                    "end": end_ms,
                    "durationMs": end_ms - start_ms,
                    "manual": bool(record.get("manual")),
                    "summary": str(record.get("summary", "")).strip(),
                })
    records.sort(key=lambda row: (row["start"], row["name"], row["recordId"]), reverse=True)
    return records


class CheckinHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, value: dict, status: int = 200) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def require_admin(self) -> bool:
        if is_admin_token(auth_header(self.headers)):
            return True
        self.send_json({"error": "admin unauthorized"}, 401)
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/assets/"):
            assets_root = (ROOT / "assets").resolve()
            asset_path = (assets_root / unquote(parsed.path[len("/assets/") :])).resolve()
            try:
                asset_path.relative_to(assets_root)
            except ValueError:
                self.send_json({"error": "not found"}, 404)
                return
            if not asset_path.is_file():
                self.send_json({"error": "not found"}, 404)
                return
            content = asset_path.read_bytes()
            content_type = mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path == "/api/session":
            token = auth_header(self.headers)
            phone = user_from_token(token)
            loaded = read_user_and_student(phone) if phone else None
            if not loaded:
                self.send_json({"error": "请重新登录"}, 401)
                return
            user, _ = loaded
            payload = {"user": public_user(phone, user), "state": scoped_state(phone)}
            self.send_json(payload)
            return
        if parsed.path == "/api/admin/session":
            if not self.require_admin():
                return
            self.send_json({"ok": True})
            return
        if parsed.path == "/api/state":
            token = auth_header(self.headers)
            phone = user_from_token(token)
            if not phone:
                self.send_json({"error": "请重新登录"}, 401)
                return
            self.send_json(scoped_state(phone))
            return
        if parsed.path == "/api/overview":
            token = auth_header(self.headers)
            if not token or not user_from_token(token):
                self.send_json({"error": "请重新登录"}, 401)
                return
            rows = read_overview()
            self.send_json({"rows": rows})
            return
        if parsed.path == "/api/admin/users":
            if not self.require_admin():
                return
            with DATA_LOCK:
                rows = [
                    {
                        **public_user(phone, user),
                        "password": "不可查看（已加密保存）",
                        "passwordAction": "可重置为123456",
                    }
                    for phone, user in read_state().get("users", {}).items()
                    if isinstance(user, dict)
                ]
            rows.sort(key=lambda item: item["name"])
            self.send_json({"users": rows})
            return
        if parsed.path == "/api/admin/export":
            if not self.require_admin():
                return
            sort_key = parse_qs(parsed.query).get("sort", ["total"])[0]
            with DATA_LOCK:
                state = read_state()
                rows = export_rows(state, sort_key)
                records = export_time_records(state)
            self.send_json({"rows": rows, "records": records, "sort": sort_key})
            return
        if parsed.path == "/api/admin/student-detail":
            if not self.require_admin():
                return
            phone = clean_phone(parse_qs(parsed.query).get("phone", [""])[0])
            if not (6 <= len(phone) <= 20):
                self.send_json({"error": "student id must be 6-20 digits"}, 400)
                return
            with DATA_LOCK:
                detail = admin_student_detail(read_state(), phone)
            if detail is None:
                self.send_json({"error": "student not found"}, 404)
                return
            self.send_json(detail)
            return
        if parsed.path not in {"/", "/index.html"}:
            self.send_json({"error": "not found"}, 404)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = parse_json_body(self)
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json({"error": "invalid JSON payload"}, 400)
            return

        if parsed.path == "/api/register":
            phone = clean_phone(payload.get("phone"))
            password = str(payload.get("password", ""))
            confirm_password = str(payload.get("confirmPassword", ""))
            name = str(payload.get("name", "")).strip()
            class_name = str(payload.get("className", "")).strip()
            if not (6 <= len(phone) <= 20) or len(password) < 6 or not confirm_password or not name or not class_name:
                self.send_json({"error": "学号、两次密码、姓名和班级不能为空，密码至少6位"}, 400)
                return
            if password != confirm_password:
                self.send_json({"error": "两次输入的密码不一致"}, 400)
                return
            user = {"phone": phone, "name": name, "className": class_name, "passwordHash": hash_password(password), "createdAt": now_iso()}
            try:
                with DATA_LOCK:
                    with sqlite3.connect(DB_FILE) as db:
                        init_db(db)
                        db.execute("INSERT INTO users VALUES(?,?,?,?,?)", (phone, name, class_name, user["passwordHash"], user["createdAt"]))
                        db.execute("INSERT INTO students VALUES(?,?,?)", (name, phone, class_name)); db.commit()
            except sqlite3.IntegrityError:
                self.send_json({"error": "该学号或姓名已经注册"}, 409); return
            token = secrets.token_urlsafe(32)
            save_session(token, "student", phone)
            self.send_json({"token": token, "user": public_user(phone, user), "state": scoped_state(phone)})
            return

        if parsed.path == "/api/login":
            phone = clean_phone(payload.get("phone"))
            password = str(payload.get("password", ""))
            supplied_name = str(payload.get("name", "")).strip()
            loaded = read_user_and_student(phone)
            user = loaded[0] if loaded else None
            if not user or not verify_password(password, user.get("passwordHash", "")):
                self.send_json({"error": "学号或密码错误"}, 401); return
            if supplied_name and supplied_name != user["name"]:
                self.send_json({"error": "姓名与学号不匹配"}, 401); return
            token = secrets.token_urlsafe(32)
            save_session(token, "student", phone)
            self.send_json({"token": token, "user": public_user(phone, user), "state": scoped_state(phone)})
            return

        if parsed.path == "/api/student/delete-note":
            token = auth_header(self.headers)
            if not user_from_token(token):
                self.send_json({"error": "student unauthorized"}, 401)
                return
            week_key = valid_week_key(payload.get("weekKey"))
            note_id = valid_item_identifier(payload.get("noteId"))
            if not week_key or not note_id:
                self.send_json({"error": "weekKey and noteId are required"}, 400)
                return
            with DATA_LOCK:
                current = read_state()
                identity = authenticated_student(current, token)
                if not identity:
                    self.send_json({"error": "student unauthorized"}, 401)
                    return
                _, owner_name, _ = identity
                candidate = deepcopy(current)
                student = candidate.get("students", {}).get(owner_name)
                weeks = student.get("weeks", {}) if isinstance(student, dict) else {}
                week = weeks.get(week_key) if isinstance(weeks, dict) else None
                notes = week.get("notes", []) if isinstance(week, dict) else []
                if not isinstance(notes, list):
                    notes = []
                note_index = next(
                    (
                        index
                        for index, note in enumerate(notes)
                        if isinstance(note, dict) and item_id(note) == note_id
                    ),
                    None,
                )
                if note_index is None:
                    self.send_json({"error": "note not found"}, 404)
                    return
                del notes[note_index]
                week["notes"] = notes
                deleted_note_ids = week.get("deletedNoteIds", [])
                if not isinstance(deleted_note_ids, list):
                    deleted_note_ids = []
                week["deletedNoteIds"] = sorted({
                    *(
                        str(existing_id).strip()
                        for existing_id in deleted_note_ids
                        if str(existing_id).strip()
                    ),
                    note_id,
                })
                write_state(candidate)
                response_state = public_state(candidate)
            self.send_json({"ok": True, "state": response_state})
            return

        if parsed.path == "/api/student/toggle-timer":
            token = auth_header(self.headers)
            phone = user_from_token(token)
            if not phone:
                self.send_json({"error": "student unauthorized"}, 401)
                return
            try:
                with DATA_LOCK:
                    result = toggle_timer(phone, str(payload.get("summary", "")).strip())
                    response_state = scoped_state(phone)
            except ValueError as error:
                self.send_json({"error": str(error)}, 400); return
            except LookupError:
                self.send_json({"error": "student unauthorized"}, 401); return
            self.send_json({
                "ok": True,
                **result,
                "state": response_state,
            })
            return

        if parsed.path == "/api/admin/login":
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
            if (
                not username
                or not hmac.compare_digest(username, ADMIN_USERNAME)
                or not hmac.compare_digest(password, ADMIN_PASSWORD)
            ):
                self.send_json({"error": "管理员密码错误"}, 401)
                return
            token = secrets.token_urlsafe(32)
            save_session(token, "admin", username)
            self.send_json({"token": token})
            return

        if parsed.path == "/api/admin/reset-password":
            if not self.require_admin():
                return
            phone = clean_phone(payload.get("phone"))
            with DATA_LOCK:
                state = read_state()
                user = state.get("users", {}).get(phone)
                if not isinstance(user, dict):
                    self.send_json({"error": "用户不存在"}, 404)
                    return
                user["passwordHash"] = hash_password(RESET_PASSWORD)
                user["passwordResetAt"] = now_iso()
                write_state(state)
            self.send_json({"ok": True, "phone": phone, "resetPassword": RESET_PASSWORD})
            return

        if parsed.path == "/api/admin/add-time-record":
            if not self.require_admin():
                return
            phone = clean_phone(payload.get("phone"))
            start_ms = finite_epoch_ms(payload.get("start"))
            end_ms = finite_epoch_ms(payload.get("end"))
            if not (6 <= len(phone) <= 20) or start_ms is None or end_ms is None:
                self.send_json({"error": "phone, start and end are invalid"}, 400)
                return
            now_ms = int(time.time() * 1000)
            if end_ms <= start_ms or end_ms > now_ms:
                self.send_json({"error": "end must be after start and not in the future"}, 400)
                return
            start_week_key = week_key_for_epoch_ms(start_ms)
            end_week_key = week_key_for_epoch_ms(end_ms)
            if not start_week_key or start_week_key != end_week_key:
                self.send_json({"error": "start and end must be in the same Asia/Shanghai week"}, 400)
                return
            with DATA_LOCK:
                current = read_state()
                user = current.get("users", {}).get(phone)
                if not isinstance(user, dict):
                    self.send_json({"error": "student not found"}, 404)
                    return
                candidate = deepcopy(current)
                candidate_user = candidate.get("users", {}).get(phone)
                week = ensure_student_week(candidate, phone, candidate_user, start_week_key)
                candidate_name = str(candidate_user.get("name", "")).strip() or phone
                candidate_student = candidate.get("students", {}).get(candidate_name, {})
                if (
                    interval_overlaps_week(week, start_ms, end_ms, now_ms)
                    or active_timer_overlaps(candidate_student, start_ms, end_ms, now_ms)
                ):
                    self.send_json({"error": "time interval overlaps an existing record"}, 409)
                    return
                record = {
                    "id": unique_time_record_id(candidate),
                    "start": start_ms,
                    "end": end_ms,
                    "manual": True,
                }
                week["timeRecords"].append(record)
                write_state(candidate)
                detail = admin_student_detail(candidate, phone)
            self.send_json({"ok": True, "record": record, "detail": detail})
            return

        if parsed.path == "/api/admin/delete-time-record":
            if not self.require_admin():
                return
            phone = clean_phone(payload.get("phone"))
            week_key = valid_week_key(payload.get("weekKey"))
            record_id = valid_item_identifier(payload.get("recordId"))
            if not (6 <= len(phone) <= 20) or not week_key or not record_id:
                self.send_json({"error": "phone, weekKey and recordId are required"}, 400)
                return
            with DATA_LOCK:
                current = read_state()
                user = current.get("users", {}).get(phone)
                if not isinstance(user, dict):
                    self.send_json({"error": "student not found"}, 404)
                    return
                name = str(user.get("name", "")).strip() or phone
                candidate = deepcopy(current)
                student = candidate.get("students", {}).get(name)
                weeks = student.get("weeks", {}) if isinstance(student, dict) else {}
                week = weeks.get(week_key) if isinstance(weeks, dict) else None
                records = week.get("timeRecords", []) if isinstance(week, dict) else []
                if not isinstance(records, list):
                    records = []
                record_index = next(
                    (
                        index
                        for index, record in enumerate(records)
                        if isinstance(record, dict) and item_id(record) == record_id
                    ),
                    None,
                )
                if record_index is None:
                    self.send_json({"error": "time record not found"}, 404)
                    return
                record = records[record_index]
                record_start = finite_epoch_ms(record.get("start"))
                record_end = finite_epoch_ms(record.get("end"))
                now_ms = int(time.time() * 1000)
                if (
                    record_start is None
                    or record_end is None
                    or record_end <= record_start
                    or record_end > now_ms
                ):
                    self.send_json({"error": "only completed time records can be deleted"}, 400)
                    return
                del records[record_index]
                week["timeRecords"] = records
                write_state(candidate)
                detail = admin_student_detail(candidate, phone)
            self.send_json({"ok": True, "recordId": record_id, "detail": detail})
            return

        if parsed.path == "/api/admin/delete-user":
            if not self.require_admin():
                return
            phone = clean_phone(payload.get("phone"))
            if not (6 <= len(phone) <= 20):
                self.send_json({"error": "student id must be 6-20 digits"}, 400)
                return
            with DATA_LOCK:
                state = read_state()
                users = state.setdefault("users", {})
                user = users.get(phone)
                if not isinstance(user, dict):
                    self.send_json({"error": "用户不存在"}, 404)
                    return
                name = str(user.get("name", "")).strip() or phone
                del users[phone]

                students = state.setdefault("students", {})
                student_names = {
                    student_name
                    for student_name, student in students.items()
                    if student_name == name
                    or (
                        isinstance(student, dict)
                        and clean_phone(student.get("phone")) == phone
                    )
                }
                student_names.add(name)
                for student_name in student_names:
                    remove_student_communications(state, student_name)
                    students.pop(student_name, None)

                for session_token, session_phone in list(USER_SESSIONS.items()):
                    if session_phone == phone:
                        USER_SESSIONS.pop(session_token, None)
                with sqlite3.connect(DB_FILE) as session_db:
                    session_db.execute("DELETE FROM sessions WHERE kind='student' AND subject=?", (phone,))
                    session_db.commit()
                write_state(state)
                response_state = public_state(state)
            self.send_json({
                "ok": True,
                "phone": phone,
                "name": name,
                "state": response_state,
            })
            return

        self.send_json({"error": "not found"}, 404)

    def do_PUT(self) -> None:
        if urlparse(self.path).path != "/api/state":
            self.send_json({"error": "not found"}, 404)
            return
        token = auth_header(self.headers)
        if not user_from_token(token):
            self.send_json({"error": "请先登录"}, 401)
            return
        try:
            payload = parse_json_body(self)
            progress = payload.get("progress")
            if not isinstance(progress, str):
                raise ValueError("progress must be a string")
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json({"error": "invalid JSON payload"}, 400)
            return
        phone = user_from_token(token)
        loaded = read_user_and_student(phone) if phone else None
        if not loaded:
            self.send_json({"error": "登录已失效，请重新登录"}, 401)
            return
        with DATA_LOCK:
            save_progress(phone, progress)
            response_state = scoped_state(phone)
        self.send_json(response_state)

    def log_message(self, format_string: str, *args) -> None:
        if self.path.startswith("/api/"):
            super().log_message(format_string, *args)

def local_ip() -> str:
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        return address
    except OSError:
        return "你的电脑IP"


def main() -> None:
    host = os.environ.get("HOST") or (sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0")
    configured_port = os.environ.get("PORT") or (sys.argv[2] if len(sys.argv) > 2 else "8000")
    port = int(configured_port)
    with DATA_LOCK:
        read_state()
    server = ThreadingHTTPServer((host, port), CheckinHandler)
    print("YSYX 学习追踪器服务已启动")
    print(f"本机访问：http://127.0.0.1:{port}/")
    print(f"同一局域网访问：http://{local_ip()}:{port}/")
    print(f"管理员账号：{ADMIN_USERNAME}（密码通过 ADMIN_PASSWORD 环境变量配置）")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
