"""
RoomSync - Real-Time Classroom Schedule Display and Attendance Monitoring System.

Prototype Flask app. Runs on a laptop for now; intended to later run on a
Raspberry Pi 4 with a 7-inch touchscreen and an RC522 RFID reader attached.
Until that hardware is available, RFID taps are simulated through a web
form (see /kiosk) where a tag ID can be typed or picked from a button.

The main display ("/") is a polling-driven state machine with these stages:
  welcome              -> idle screen, only teacher taps are accepted
  teacher_early_confirm -> teacher tapped within attendance_window_minutes
                            before class start; asks to confirm proceeding.
                            "No" resets to welcome; "Yes" moves to
                            teacher_early_wait. Taps earlier than this
                            window are disregarded entirely
  teacher_early_wait    -> teacher confirmed early; waiting for the actual
                            start_time to arrive (auto-advances, no further
                            action needed)
  attendance_open       -> window is open; live countdown, lasting
                            attendance_window_minutes; students can tap.
                            Closes early, before the countdown ends, the
                            moment every student has tapped in (perfect
                            attendance)
  class_ongoing         -> countdown ended (or attendance was perfect);
                            waits for class end_time, then moves to
                            session_summary. If the active class's teacher
                            taps again here (before end_time), moves to
                            end_early_confirm instead
  end_early_confirm     -> the active teacher tapped again during
                            attendance_open or class_ongoing, before
                            end_time. Asks "do you want to end the class
                            early?". "No" backs out with nothing changed —
                            back to the live attendance_open countdown if
                            that's where it was triggered from, or
                            class_ongoing if attendance was already
                            closed; "Yes" moves to session_summary with
                            is_early_end=True
  session_summary       -> shows teacher, class, and present students for a
                            Yes/No(/Cancel) confirmation. "Yes" (or 1
                            minute passing with no response) locks it in
                            and returns to welcome; "No" moves to
                            session_summary_edit, same as a normal
                            end-of-class summary. "Cancel" is only shown
                            when is_early_end is set, and abandons the
                            early-end attempt with attendance untouched —
                            landing back on the live attendance_open
                            countdown if that's where it was triggered
                            from (early_end_origin), or class_ongoing if
                            attendance was already closed
  session_summary_edit  -> the same roster, but each student can be tapped
                            to toggle present/absent; a "Confirm" button
                            (or the same 1-minute timeout) locks it in and
                            returns to welcome. Also has a Cancel button,
                            with the same is_early_end-gated behavior as
                            session_summary's

Transient messages (student tapped too early, tap confirmations, taps
rejected because no window is open, etc.) are carried as a "flash" on the
state and auto-expire, without changing the underlying stage.
"""

import json
import math
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timedelta, date
from functools import wraps

import requests
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from pywebpush import webpush, WebPushException

from config import (
    SMS_GATEWAY_IP,
    SMS_GATEWAY_PORT,
    FLASK_SECRET_KEY,
    VAPID_PRIVATE_KEY,
    VAPID_PUBLIC_KEY,
)

DB_PATH = "roomsync.db"

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY  # dev-only value lives in config.py (gitignored), see CLAUDE.md's "Known simplifications"

# VAPID key pair for Web Push (see generate_vapid_keys.py), imported above
# from config.py (gitignored) rather than hardcoded here — see CLAUDE.md's
# "Known simplifications" before any real deployment. VAPID_PUBLIC_KEY is
# also embedded into dashboard.html/student_dashboard.html as the
# PushManager applicationServerKey.
VAPID_CLAIMS_SUB = "mailto:admin@roomsync.local"  # required by the push spec; not a real inbox

# SMS gateway: a spare Android phone (its own SIM/load, no paid API) running
# a free "REST SMS Gateway" app, which exposes a local HTTP endpoint that
# sends a real SMS via that phone's SIM. SMS_GATEWAY_IP/PORT (imported
# above from config.py, not hardcoded here) must match whatever the REST
# SMS Gateway app currently displays on the phone, and the Raspberry Pi
# and the phone must be on the same WiFi network — if the phone reconnects
# to WiFi and gets a new IP, config.py goes stale and needs updating by
# hand (see CLAUDE.md's "Known simplifications").


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Web Push notifications. No background task queue — sends happen inline,
# synchronously, wherever a trigger fires (see "Notification triggers" in
# CLAUDE.md); fine for a low-traffic capstone prototype, but see "Known
# simplifications" there for what that costs. Requires HTTPS in the
# browser, so only actually delivers over the ngrok tunnel, never plain
# http://localhost.
# ---------------------------------------------------------------------------

def send_push_notification(user_type, user_id, title, body):
    """Sends a web push notification to every device this user (a teacher
    or student, by user_type + user_id) has subscribed from. Silently
    drops any subscription pywebpush reports as expired/unsubscribed
    (a 404/410 response) by deleting its row; leaves anything else (a
    transient network error, say) in place to retry next time."""
    dashboard_url = url_for("dashboard" if user_type == "teacher" else "student_dashboard")

    conn = get_db()
    subs = conn.execute(
        "SELECT subscription_id, endpoint, p256dh_key, auth_key FROM push_subscriptions "
        "WHERE user_type = ? AND user_id = ?",
        (user_type, user_id),
    ).fetchall()

    # DEBUG (temporary):
    print(f"[send_push_notification] user_type={user_type} user_id={user_id} title={title!r} -> found {len(subs)} subscription(s)")

    for sub in subs:
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh_key"], "auth": sub["auth_key"]},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=json.dumps({"title": title, "body": body, "url": dashboard_url}),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIMS_SUB},
            )
            print(f"[send_push_notification] sent OK to subscription_id={sub['subscription_id']} ({sub['endpoint'][:60]}...)")
        except WebPushException as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            print(f"[send_push_notification] WebPushException for subscription_id={sub['subscription_id']}: status={status_code} {exc}")
            if status_code in (404, 410):
                conn.execute(
                    "DELETE FROM push_subscriptions WHERE subscription_id = ?",
                    (sub["subscription_id"],),
                )
                conn.commit()
        except Exception as exc:
            # Anything else — a malformed key that fails local crypto
            # processing before any request is even sent (pywebpush raises
            # a plain ValueError for that, not WebPushException), a DNS
            # failure, etc. Never let one bad subscription crash whichever
            # request triggered this notification; just skip it and try
            # the rest.
            print(f"[send_push_notification] non-WebPush exception for subscription_id={sub['subscription_id']}: {type(exc).__name__}: {exc}")

    conn.close()


# ---------------------------------------------------------------------------
# SMS notifications, via a free "REST SMS Gateway" Android app running on a
# spare phone with its own SIM (no paid API, no new server-side hardware) —
# added per a panel recommendation during the mock defense. Same inline,
# synchronous, no-retry philosophy as Web Push above — see "Known
# simplifications" in CLAUDE.md. Requires the Pi and that phone to be on
# the same WiFi network (see SMS_GATEWAY_IP/PORT above).
# ---------------------------------------------------------------------------

def send_sms_notification(phone_number, message):
    """Sends message as a real SMS via the spare phone's SIM, by hitting
    its REST SMS Gateway app's local HTTP endpoint. No-ops immediately if
    phone_number is falsy (None/empty) — the SMS equivalent of a user
    having no push subscriptions — and never raises: any failure (gateway
    unreachable, phone off WiFi, whatever) is caught, logged, and
    swallowed, the same as a bad push subscription never crashes whichever
    request triggered the notification."""
    # DEBUG (temporary): prints on every call, even a no-op below, so a
    # falsy phone_number (silent no-op) is distinguishable in the log from
    # this function never being called at all.
    print(f"[send_sms_notification] called with phone_number={phone_number!r}")
    if not phone_number:
        print("[send_sms_notification] no-op: phone_number is falsy")
        return

    encoded_message = urllib.parse.quote(message)
    url = f"http://{SMS_GATEWAY_IP}:{SMS_GATEWAY_PORT}/v1/sms/send/?phone={phone_number}&message={encoded_message}"

    try:
        response = requests.get(url, timeout=10)
        print(f"[send_sms_notification] sent to phone_number={phone_number}: status={response.status_code}")
    except requests.exceptions.RequestException as exc:
        print(f"[send_sms_notification] failed to phone_number={phone_number}: {exc}")
    except Exception as exc:
        # Never let a bad phone number, a malformed URL, or anything else
        # unexpected crash whichever request triggered this notification.
        print(f"[send_sms_notification] unexpected error for phone_number={phone_number}: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Auth: simple session-based login for three roles, "teacher", "admin",
# and "student". session holds {"role": "teacher"|"admin"|"student",
# "user_id": <teacher_id|admin_id|student_id>}.
# ---------------------------------------------------------------------------

def login_required(role):
    """Redirect to /login unless the session belongs to a logged-in `role`."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session.get("role") != role or "user_id" not in session:
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


# ---------------------------------------------------------------------------
# Display state machine (single room, in-memory, shared by / and /kiosk)
# ---------------------------------------------------------------------------

room_state_lock = threading.Lock()

# (class_id, "YYYY-MM-DD") pairs whose session was ended early today, so the
# dashboard can show "Completed" instead of "In progress" even though
# wall-clock time hasn't reached the class's scheduled end_time yet. Purely
# in-memory, like room_state itself — reset if the process restarts.
_early_ended_sessions = set()

# (class_id, "YYYY-MM-DD") pairs already sent the "starts in 10 minutes"
# push reminder today, so advance_state()'s lazy per-request check (see
# _check_ten_minute_reminders()) only fires it once per class per day
# rather than on every poll during that 10-minute window. Also in-memory,
# same lifetime as room_state.
_ten_minute_reminders_sent = set()

room_state = {
    "stage": "welcome",
    "teacher_id": None,
    "teacher_name": None,
    "class_id": None,
    "subject_name": None,
    "room_number": None,
    "start_time": None,
    "end_time": None,
    "window_minutes": None,
    "wait_until": None,         # ISO datetime: when a confirmed early wait auto-opens
    "countdown_end_at": None,   # ISO datetime: when the open window closes
    "minutes_early": None,
    "summary_expires_at": None, # ISO datetime: when an unconfirmed session summary auto-confirms
    "is_early_end": False,      # True when the current session_summary was reached via end_early_confirm
    "early_end_origin": None,   # stage the end_early_confirm prompt was triggered from ("attendance_open"/"class_ongoing")
    "flash": None,              # {"message", "type", "until"} or None
}


def set_flash(message, msg_type="info", seconds=3):
    room_state["flash"] = {
        "message": message,
        "type": msg_type,
        "until": (datetime.now() + timedelta(seconds=seconds)).isoformat(),
    }


def _clear_expired_flash(now):
    flash = room_state.get("flash")
    if flash and datetime.fromisoformat(flash["until"]) <= now:
        room_state["flash"] = None


def _open_attendance_window(conn, now):
    # Starting a new session for this class/day should always begin at zero,
    # even if it already had attendance rows from an earlier session today.
    today = now.strftime("%Y-%m-%d")
    conn.execute(
        "DELETE FROM attendance WHERE class_id = ? AND date = ?",
        (room_state["class_id"], today),
    )
    conn.commit()

    window_minutes = room_state["window_minutes"]
    room_state["stage"] = "attendance_open"
    room_state["countdown_end_at"] = (now + timedelta(minutes=window_minutes)).isoformat()
    room_state["minutes_early"] = None
    room_state["wait_until"] = None

    subject_name = room_state["subject_name"]
    body = f"Attendance is now open for {subject_name}."
    for student in conn.execute(
        """
        SELECT s.student_id, s.phone_number
        FROM enrollments e
        JOIN students s ON s.student_id = e.student_id
        WHERE e.class_id = ?
        """,
        (room_state["class_id"],),
    ).fetchall():
        send_push_notification("student", student["student_id"], "Attendance Open", body)
        send_sms_notification(student["phone_number"], body)


def _notify_session_ended(conn, class_id, teacher_id, subject_name, today):
    """Trigger 3: fires when a session summary is actually finalized —
    called from _reset_to_welcome() only while the stage being left is
    session_summary/session_summary_edit, so a teacher declining the
    early-tap prompt (also a _reset_to_welcome() caller, but from
    teacher_early_confirm) never sends this."""
    if not teacher_id:
        return
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM enrollments WHERE class_id = ?", (class_id,)
    ).fetchone()["c"]
    present = conn.execute(
        "SELECT COUNT(*) AS c FROM attendance WHERE class_id = ? AND date = ?", (class_id, today)
    ).fetchone()["c"]
    body = f"Session ended: {present}/{total} present for {subject_name}."
    send_push_notification("teacher", teacher_id, "Session Ended", body)
    # room_state only carries teacher_id/teacher_name, not phone_number, so
    # unlike the other two triggers (which already join phone_number into
    # a query they're making anyway), this is one small extra lookup by
    # primary key — negligible next to threading a notification-only field
    # through the whole room_state dict.
    teacher = conn.execute("SELECT phone_number FROM teachers WHERE teacher_id = ?", (teacher_id,)).fetchone()
    send_sms_notification(teacher["phone_number"] if teacher else None, body)


def _reset_to_welcome(conn, now=None):
    now = now or datetime.now()
    if room_state["stage"] in ("session_summary", "session_summary_edit") and room_state["class_id"]:
        today = now.strftime("%Y-%m-%d")
        if room_state["is_early_end"]:
            _early_ended_sessions.add((room_state["class_id"], today))
        _notify_session_ended(conn, room_state["class_id"], room_state["teacher_id"], room_state["subject_name"], today)
    room_state.update({
        "stage": "welcome",
        "teacher_id": None,
        "teacher_name": None,
        "class_id": None,
        "subject_name": None,
        "room_number": None,
        "start_time": None,
        "end_time": None,
        "window_minutes": None,
        "wait_until": None,
        "countdown_end_at": None,
        "minutes_early": None,
        "summary_expires_at": None,
        "is_early_end": False,
        "early_end_origin": None,
    })


def _attendance_is_perfect(conn, class_id, today):
    """True once every student enrolled in this class has tapped in for
    this date — i.e. nobody enrolled is left who could still show up as
    absent."""
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM enrollments WHERE class_id = ?) AS total,
            (SELECT COUNT(*) FROM attendance WHERE class_id = ? AND date = ?) AS tapped
        """,
        (class_id, class_id, today),
    ).fetchone()
    return row["total"] > 0 and row["tapped"] >= row["total"]


def _check_ten_minute_reminders(conn, now):
    """Trigger 1: 10 minutes before any assigned, tappable class's
    start_time — regardless of what the shared display is currently
    doing, since this is informational for the teacher/students of
    whichever class is coming up next, not tied to room_state. Runs
    outside room_state_lock (it never touches room_state) so the push
    sends here can't block the state machine for unrelated requests."""
    today = now.strftime("%Y-%m-%d")
    classes = conn.execute(
        """
        SELECT c.class_id, c.subject_name, c.teacher_id, c.room_number, c.start_time,
               t.phone_number AS teacher_phone
        FROM classes c
        JOIN teachers t ON t.teacher_id = c.teacher_id
        WHERE c.teacher_id IS NOT NULL AND c.start_time IS NOT NULL
        """
    ).fetchall()
    for c in classes:
        key = (c["class_id"], today)
        start_dt = datetime.strptime(f"{today} {c['start_time']}", "%Y-%m-%d %H:%M")
        reminder_at = start_dt - timedelta(minutes=10)
        already_sent = key in _ten_minute_reminders_sent
        condition_met = reminder_at <= now < start_dt
        # DEBUG (temporary — see the "Debugging" note in CLAUDE.md's Web
        # Push section for how to remove this once confirmed working):
        print(
            f"[10-min reminder check] class_id={c['class_id']} subject={c['subject_name']!r} "
            f"now={now.strftime('%H:%M:%S')} start_time={c['start_time']} "
            f"reminder_at={reminder_at.strftime('%H:%M:%S')} "
            f"already_sent_today={already_sent} condition_met={condition_met}"
        )
        if already_sent:
            continue
        if condition_met:
            _ten_minute_reminders_sent.add(key)
            room_label = c["room_number"] or "the classroom"
            body = f"Reminder: {c['subject_name']} starts in 10 minutes at {room_label}."
            print(f"[10-min reminder check] FIRING for class_id={c['class_id']} -> teacher_id={c['teacher_id']} + enrolled students")
            send_push_notification("teacher", c["teacher_id"], "Class Starting Soon", body)
            send_sms_notification(c["teacher_phone"], body)
            for student in conn.execute(
                """
                SELECT s.student_id, s.phone_number
                FROM enrollments e
                JOIN students s ON s.student_id = e.student_id
                WHERE e.class_id = ?
                """,
                (c["class_id"],),
            ).fetchall():
                send_push_notification("student", student["student_id"], "Class Starting Soon", body)
                send_sms_notification(student["phone_number"], body)


def advance_state(conn, now=None):
    """Resolve any time-based transitions. Call before reading/using state."""
    now = now or datetime.now()
    _check_ten_minute_reminders(conn, now)
    with room_state_lock:
        _clear_expired_flash(now)
        stage = room_state["stage"]

        if stage == "teacher_early_wait":
            wait_until = datetime.fromisoformat(room_state["wait_until"])
            if now >= wait_until:
                _open_attendance_window(conn, now)

        elif stage == "attendance_open":
            countdown_end_at = datetime.fromisoformat(room_state["countdown_end_at"])
            today = now.strftime("%Y-%m-%d")
            if now >= countdown_end_at or _attendance_is_perfect(conn, room_state["class_id"], today):
                room_state["stage"] = "class_ongoing"

        elif stage == "class_ongoing":
            today = now.strftime("%Y-%m-%d")
            end_dt = datetime.strptime(f"{today} {room_state['end_time']}", "%Y-%m-%d %H:%M")
            if now >= end_dt:
                room_state["stage"] = "session_summary"
                room_state["summary_expires_at"] = (end_dt + timedelta(minutes=1)).isoformat()

        elif stage in ("session_summary", "session_summary_edit"):
            summary_expires_at = datetime.fromisoformat(room_state["summary_expires_at"])
            if now >= summary_expires_at:
                _reset_to_welcome(conn, now)


def get_teacher_class_today(conn, teacher_id, now):
    """The teacher's next/current class today that hasn't ended yet.
    Classes have no day-of-week concept — once a class has a start/end time
    set, it's assumed to meet every single day at that time, so this only
    filters on the time of day, not which day it is."""
    time_now = now.strftime("%H:%M")
    return conn.execute(
        """
        SELECT class_id, subject_name, room_number, start_time, end_time, attendance_window_minutes
        FROM classes
        WHERE teacher_id = ?
          AND start_time IS NOT NULL
          AND end_time IS NOT NULL
          AND end_time > ?
        ORDER BY start_time
        LIMIT 1
        """,
        (teacher_id, time_now),
    ).fetchone()


def class_time_conflict(conn, teacher_id, start_time, end_time, exclude_class_id=None):
    """The teacher's other class (if any) whose [start_time, end_time) range
    overlaps this one. Since every class with a time set recurs daily, any
    overlapping range is a standing conflict — the teacher would need to be
    in two places at once every day, not just on some particular date."""
    rows = conn.execute(
        """
        SELECT class_id, subject_name, start_time, end_time
        FROM classes
        WHERE teacher_id = ? AND start_time IS NOT NULL AND end_time IS NOT NULL
        """,
        (teacher_id,),
    ).fetchall()
    for row in rows:
        if exclude_class_id is not None and row["class_id"] == exclude_class_id:
            continue
        if start_time < row["end_time"] and end_time > row["start_time"]:
            return row
    return None


def room_time_conflict(conn, room_number, start_time, end_time, exclude_class_id=None):
    """The other class (if any, regardless of teacher) whose [start_time,
    end_time) range overlaps this one AND uses the same room_number. Two
    different teachers' classes can share a time slot freely as long as
    they're in different rooms — this only rejects double-booking the same
    physical room at the same time every day."""
    rows = conn.execute(
        """
        SELECT class_id, subject_name, start_time, end_time
        FROM classes
        WHERE room_number = ? AND start_time IS NOT NULL AND end_time IS NOT NULL
        """,
        (room_number,),
    ).fetchall()
    for row in rows:
        if exclude_class_id is not None and row["class_id"] == exclude_class_id:
            continue
        if start_time < row["end_time"] and end_time > row["start_time"]:
            return row
    return None


def student_enrollment_conflict(conn, student_id, start_time, end_time, exclude_class_id=None):
    """The student's other enrolled class (if any) whose [start_time,
    end_time) range overlaps this one. Since every class with a time set
    recurs daily, any overlap is a standing conflict — the student can't
    be in two classes at once, every day."""
    rows = conn.execute(
        """
        SELECT c.class_id, c.subject_name, c.start_time, c.end_time
        FROM enrollments e
        JOIN classes c ON c.class_id = e.class_id
        WHERE e.student_id = ? AND c.start_time IS NOT NULL AND c.end_time IS NOT NULL
        """,
        (student_id,),
    ).fetchall()
    for row in rows:
        if exclude_class_id is not None and row["class_id"] == exclude_class_id:
            continue
        if start_time < row["end_time"] and end_time > row["start_time"]:
            return row
    return None


def handle_teacher_tap(conn, teacher, now):
    teacher_name = f"{teacher['first_name']} {teacher['last_name']}"

    with room_state_lock:
        stage = room_state["stage"]
        active_teacher_id = room_state["teacher_id"]
        active_end_time = room_state["end_time"]

    # The teacher whose class is currently active tapping again mid-class
    # (attendance still open, or already ongoing) is a request to end the
    # class early, not a duplicate tap — as long as the scheduled end_time
    # hasn't already passed (a different teacher's tap, or a tap after
    # end_time, falls through to the normal rejection below).
    if stage in ("attendance_open", "class_ongoing") and active_teacher_id == teacher["teacher_id"]:
        today = now.strftime("%Y-%m-%d")
        end_dt = datetime.strptime(f"{today} {active_end_time}", "%Y-%m-%d %H:%M")
        if now < end_dt:
            with room_state_lock:
                room_state["flash"] = None
                room_state["stage"] = "end_early_confirm"
                room_state["early_end_origin"] = stage
            return f"{teacher_name}, do you want to end the class early?", "success"

    if stage != "welcome":
        set_flash(f"{teacher_name}, an attendance session is already in progress.", "warning")
        return "Attendance session already active for this room.", "warning"

    class_row = get_teacher_class_today(conn, teacher["teacher_id"], now)
    if not class_row:
        set_flash(f"No more classes scheduled today for {teacher_name}.", "error")
        return f"No class scheduled for {teacher_name} today.", "error"

    today = now.strftime("%Y-%m-%d")
    start_dt = datetime.strptime(f"{today} {class_row['start_time']}", "%Y-%m-%d %H:%M")
    window_minutes = class_row["attendance_window_minutes"] or 15
    window_open_at = start_dt - timedelta(minutes=window_minutes)

    if now < window_open_at:
        # More than window_minutes before class start — too early to be a
        # meaningful tap. Disregard it entirely; nothing changes on the
        # shared display.
        return (
            f"{teacher_name}'s attendance window isn't open yet — try again within "
            f"{window_minutes} minutes of class start.",
            "warning",
        )

    with room_state_lock:
        room_state["flash"] = None
        room_state.update({
            "teacher_id": teacher["teacher_id"],
            "teacher_name": teacher_name,
            "class_id": class_row["class_id"],
            "subject_name": class_row["subject_name"],
            "room_number": class_row["room_number"],
            "start_time": class_row["start_time"],
            "end_time": class_row["end_time"],
            "window_minutes": window_minutes,
        })

        if now < start_dt:
            room_state["stage"] = "teacher_early_confirm"
            room_state["minutes_early"] = math.ceil((start_dt - now).total_seconds() / 60)
        else:
            _open_attendance_window(conn, now)

    return f"Hello {teacher_name}.", "success"


def handle_student_tap(conn, student, now):
    student_name = f"{student['first_name']} {student['last_name']}"

    with room_state_lock:
        stage = room_state["stage"]
        class_id = room_state["class_id"]

    if stage == "welcome":
        set_flash("Only teachers can tap at this time.", "error", seconds=4)
        return "Only teachers can tap at this time.", "error"

    if stage != "attendance_open":
        set_flash("Attendance is not open right now.", "warning", seconds=3)
        return "Attendance is not currently open.", "warning"

    enrolled = conn.execute(
        "SELECT 1 FROM enrollments WHERE student_id = ? AND class_id = ?",
        (student["student_id"], class_id),
    ).fetchone()
    if not enrolled:
        set_flash(f"{student_name} is not enrolled in this class.", "error", seconds=4)
        return f"{student_name} is not enrolled in this class.", "error"

    today = now.strftime("%Y-%m-%d")
    already = conn.execute(
        "SELECT 1 FROM attendance WHERE student_id = ? AND class_id = ? AND date = ?",
        (student["student_id"], class_id, today),
    ).fetchone()

    if already:
        set_flash(f"{student_name} already tapped in.", "warning", seconds=3)
        return f"{student_name} already tapped in.", "warning"

    conn.execute(
        """
        INSERT INTO attendance (student_id, class_id, date, time_tapped, status)
        VALUES (?, ?, ?, ?, 'present')
        """,
        (student["student_id"], class_id, today, now.strftime("%H:%M:%S")),
    )
    conn.commit()

    set_flash(f"{student_name} - PRESENT", "success", seconds=3)
    return f"{student_name} - PRESENT", "success"


def get_display_state(conn, now=None):
    """Snapshot of room_state plus derived roster info, for the display and its poller."""
    now = now or datetime.now()
    advance_state(conn, now)

    with room_state_lock:
        state = dict(room_state)

    data = {
        "stage": state["stage"],
        "server_time": now.isoformat(),
        "teacher_name": state["teacher_name"],
        "subject_name": state["subject_name"],
        "room_number": state["room_number"],
        "start_time": state["start_time"],
        "end_time": state["end_time"],
        "minutes_early": state["minutes_early"],
        "wait_until": state["wait_until"],
        "countdown_end_at": state["countdown_end_at"],
        "summary_expires_at": state["summary_expires_at"],
        "is_early_end": state["is_early_end"],
        "flash": state["flash"],
    }

    if state["stage"] == "attendance_open" and state["class_id"]:
        today = now.strftime("%Y-%m-%d")
        roster = conn.execute(
            """
            SELECT a.status
            FROM enrollments e
            JOIN students st ON st.student_id = e.student_id
            LEFT JOIN attendance a
                   ON a.student_id = st.student_id
                  AND a.class_id = ?
                  AND a.date = ?
            WHERE e.class_id = ?
            """,
            (state["class_id"], today, state["class_id"]),
        ).fetchall()
        data["present_count"] = sum(1 for r in roster if r["status"])
        data["total_students"] = len(roster)

    elif state["stage"] in ("session_summary", "session_summary_edit") and state["class_id"]:
        today = now.strftime("%Y-%m-%d")
        roster, present_count, _late_count, _excused_count, _absent_count = get_roster_for_date(conn, state["class_id"], today)
        data["present_count"] = present_count
        data["total_students"] = len(roster)
        data["roster"] = [
            {"student_id": r["student_id"], "name": f"{r['first_name']} {r['last_name']}", "status": r["status"]}
            for r in roster
        ]

    return data


# ---------------------------------------------------------------------------
# Legacy time-based helpers (kept for /api/status and /summary compatibility)
# ---------------------------------------------------------------------------

def get_current_class(conn, now=None):
    """Return the class whose time window covers `now`, if any (classes have
    no day-of-week concept — a class with a time set meets every day)."""
    now = now or datetime.now()
    time_now = now.strftime("%H:%M")

    row = conn.execute(
        """
        SELECT c.class_id, c.subject_name, c.room_number,
               c.start_time, c.end_time, c.attendance_window_minutes,
               t.first_name || ' ' || t.last_name AS teacher_name
        FROM classes c
        JOIN teachers t ON t.teacher_id = c.teacher_id
        WHERE c.start_time IS NOT NULL AND c.end_time IS NOT NULL
          AND c.start_time <= ?
          AND c.end_time > ?
        ORDER BY c.start_time
        LIMIT 1
        """,
        (time_now, time_now),
    ).fetchone()
    return row


LOGIN_ROLES = ("teacher", "admin", "student")


@app.route("/login", methods=["GET", "POST"])
def login():
    """Single login page with a Teacher/Admin/Student toggle (see
    templates/login.html)."""
    active_role = request.values.get("role") if request.values.get("role") in LOGIN_ROLES else "teacher"

    if request.method == "POST":
        role = request.form.get("role")
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        active_role = role if role in LOGIN_ROLES else "teacher"

        conn = get_db()
        if role == "admin":
            user = conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
            user_id_key = "admin_id"
            redirect_to = "admin"
        elif role == "student":
            user = conn.execute("SELECT * FROM students WHERE username = ?", (username,)).fetchone()
            user_id_key = "student_id"
            redirect_to = "student_dashboard"
        else:
            user = conn.execute("SELECT * FROM teachers WHERE username = ?", (username,)).fetchone()
            user_id_key = "teacher_id"
            redirect_to = "dashboard"
        conn.close()

        if user and user["password_hash"] and check_password_hash(user["password_hash"], password):
            session.clear()
            session["role"] = role
            session["user_id"] = user[user_id_key]
            return redirect(url_for(redirect_to))

        flash("Invalid username or password.", "error")

    return render_template("login.html", active_role=active_role)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    """Main schedule/attendance display board (what the door screen shows)."""
    conn = get_db()
    initial_state = get_display_state(conn)
    conn.close()
    return render_template("index.html", now=datetime.now(), initial_state=initial_state)


@app.route("/api/display_state")
def api_display_state():
    """Polled by the display page (and can be polled by other clients) for live state."""
    conn = get_db()
    data = get_display_state(conn)
    conn.close()
    return jsonify(data)


@app.route("/api/teacher_confirm", methods=["POST"])
def api_teacher_confirm():
    """Yes/No response to the 'you are early' prompt, tapped on the touchscreen."""
    payload = request.get_json(silent=True) or {}
    action = payload.get("action") or request.form.get("action")
    now = datetime.now()
    conn = get_db()
    advance_state(conn, now)

    with room_state_lock:
        if room_state["stage"] == "teacher_early_confirm":
            if action == "yes":
                today = now.strftime("%Y-%m-%d")
                start_dt = datetime.strptime(f"{today} {room_state['start_time']}", "%Y-%m-%d %H:%M")
                room_state["stage"] = "teacher_early_wait"
                room_state["wait_until"] = start_dt.isoformat()
                room_state["minutes_early"] = None
            else:
                _reset_to_welcome(conn, now)

    data = get_display_state(conn)
    conn.close()
    return jsonify(data)


@app.route("/api/end_early_confirm", methods=["POST"])
def api_end_early_confirm():
    """Yes/No response to the 'do you want to end the class early?' prompt,
    tapped on the touchscreen. "No" backs out with nothing changed: it
    resumes the live attendance_open countdown (picking up right where it
    left off — countdown_end_at and any attendance rows already recorded
    were never touched) if that's where the request came from, or
    class_ongoing if attendance was already closed when it was triggered.
    "Yes" moves into the same session_summary flow used at a normal
    end-of-class, flagged as an early-end attempt via is_early_end."""
    payload = request.get_json(silent=True) or {}
    action = payload.get("action") or request.form.get("action")
    now = datetime.now()
    conn = get_db()
    advance_state(conn, now)

    with room_state_lock:
        if room_state["stage"] == "end_early_confirm":
            if action == "yes":
                room_state["stage"] = "session_summary"
                room_state["is_early_end"] = True
                room_state["summary_expires_at"] = (now + timedelta(minutes=1)).isoformat()
                # early_end_origin is kept (not cleared) here — if the
                # teacher later cancels on the summary/edit screen, that's
                # what tells it whether to resume attendance_open or fall
                # back to class_ongoing.
            else:
                room_state["stage"] = (
                    "attendance_open" if room_state["early_end_origin"] == "attendance_open" else "class_ongoing"
                )
                room_state["early_end_origin"] = None

    data = get_display_state(conn)
    conn.close()
    return jsonify(data)


@app.route("/api/session_summary_confirm", methods=["POST"])
def api_session_summary_confirm():
    """Yes/No(/Cancel) response to the end-of-session summary, tapped on the
    touchscreen. "No" opens the editable roster (session_summary_edit),
    letting the teacher correct who's present, the same whether this is a
    normal end-of-class summary or one reached via an early-end request.
    "Yes" from either the summary or the edit screen locks the summary in
    and returns to welcome, ending the class immediately even if it was
    ended early. "Cancel" is only offered (and only honored) when this
    summary was reached via an early-end request (is_early_end) — from
    either the summary screen or the edit screen — and it backs all the
    way out, abandoning the early-end attempt with attendance left exactly
    as it was. Where it lands depends on early_end_origin: back to the
    live attendance_open countdown (picking up right where it left off —
    its countdown_end_at was never touched, and its attendance rows were
    never cleared, including any edits made in session_summary_edit before
    cancelling) if that's where the request came from, or class_ongoing if
    attendance was already closed when it was triggered."""
    payload = request.get_json(silent=True) or {}
    action = payload.get("action") or request.form.get("action")
    now = datetime.now()
    conn = get_db()
    advance_state(conn, now)

    with room_state_lock:
        if room_state["stage"] == "session_summary" and action == "no":
            room_state["stage"] = "session_summary_edit"
        elif room_state["stage"] in ("session_summary", "session_summary_edit") and action == "cancel" and room_state["is_early_end"]:
            room_state["stage"] = (
                "attendance_open" if room_state["early_end_origin"] == "attendance_open" else "class_ongoing"
            )
            room_state["is_early_end"] = False
            room_state["early_end_origin"] = None
        elif room_state["stage"] in ("session_summary", "session_summary_edit") and action == "yes":
            _reset_to_welcome(conn, now)

    data = get_display_state(conn)
    conn.close()
    return jsonify(data)


SUMMARY_TOGGLE_CYCLE = ["absent", "present", "late", "excused"]


@app.route("/api/session_summary_toggle", methods=["POST"])
def api_session_summary_toggle():
    """Cycles one student's status while editing the summary: absent ->
    present -> late -> excused -> absent, one step per tap. Absent is
    represented by the *absence* of an attendance row (matching how a
    student who never tapped in shows up everywhere else); the other
    three are an attendance row with that status."""
    payload = request.get_json(silent=True) or {}
    student_id = payload.get("student_id")
    now = datetime.now()
    conn = get_db()
    advance_state(conn, now)

    with room_state_lock:
        if room_state["stage"] == "session_summary_edit" and student_id is not None:
            class_id = room_state["class_id"]
            today = now.strftime("%Y-%m-%d")
            existing = conn.execute(
                "SELECT status FROM attendance WHERE student_id = ? AND class_id = ? AND date = ?",
                (student_id, class_id, today),
            ).fetchone()
            current_status = existing["status"] if existing else "absent"
            next_status = SUMMARY_TOGGLE_CYCLE[
                (SUMMARY_TOGGLE_CYCLE.index(current_status) + 1) % len(SUMMARY_TOGGLE_CYCLE)
            ]

            if next_status == "absent":
                conn.execute(
                    "DELETE FROM attendance WHERE student_id = ? AND class_id = ? AND date = ?",
                    (student_id, class_id, today),
                )
            elif existing:
                conn.execute(
                    "UPDATE attendance SET status = ? WHERE student_id = ? AND class_id = ? AND date = ?",
                    (next_status, student_id, class_id, today),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO attendance (student_id, class_id, date, time_tapped, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (student_id, class_id, today, now.strftime("%H:%M:%S"), next_status),
                )
            conn.commit()

    data = get_display_state(conn)
    conn.close()
    return jsonify(data)


# ---------------------------------------------------------------------------
# RFID registration ("Register RFID" button on /admin). Arms the *next* tap
# that hits /kiosk to assign a card's rfid_tag to whichever teacher/student
# an admin just clicked "Register RFID" for, instead of that tap going
# through the normal welcome/attendance flow. Single in-memory slot, same
# pattern as room_state — only one registration can be armed at a time,
# which is fine since only one admin is expected to be doing this at once.
# Because /kiosk is the exact same endpoint rfid_listener.py posts real
# RC522 taps to, this works with a real card tap with no changes needed
# there, the same way the rest of the tap flow already does.
# ---------------------------------------------------------------------------

RFID_REGISTRATION_TIMEOUT_SECONDS = 30

rfid_registration_lock = threading.Lock()

rfid_registration_state = {
    "active": False,       # True while armed and waiting for a tap
    "user_type": None,     # "teacher" | "student"
    "user_id": None,
    "person_name": None,
    "status": "idle",      # "idle" | "waiting" | "success" | "error"
    "tag": None,           # the tag captured on success
    "message": None,       # error message on failure
    "expires_at": None,    # ISO datetime the "waiting" attempt gives up
}


def _reset_rfid_registration():
    rfid_registration_state.update(
        active=False, user_type=None, user_id=None, person_name=None,
        status="idle", tag=None, message=None, expires_at=None,
    )


def _rfid_tag_owner(conn, rfid_tag):
    """Returns (user_type, user_id, first_name, last_name) for whoever
    currently holds this rfid_tag, checked across both teachers and
    students (a plain per-table UNIQUE constraint wouldn't catch the same
    tag existing in both tables at once), or None if it's unused."""
    row = conn.execute(
        "SELECT teacher_id AS id, first_name, last_name FROM teachers WHERE rfid_tag = ?", (rfid_tag,)
    ).fetchone()
    if row:
        return "teacher", row["id"], row["first_name"], row["last_name"]
    row = conn.execute(
        "SELECT student_id AS id, first_name, last_name FROM students WHERE rfid_tag = ?", (rfid_tag,)
    ).fetchone()
    if row:
        return "student", row["id"], row["first_name"], row["last_name"]
    return None


def _try_register_rfid_tap(conn, rfid_tag, now):
    """If RFID registration is currently armed, consumes this tap and
    returns a (message, message_type) tuple for the kiosk page. Returns
    None if registration isn't active (or the tap was blank), so the
    caller falls through to the normal teacher/student tap handling.

    Two modes, both set up by admin_rfid_register_start(): with a
    user_id, this writes rfid_tag onto that existing teacher/student row
    (the "Register RFID" button next to an existing row). Without one
    (user_id is None — the "Scan RFID Card" button on the Add
    Teacher/Add Student form, for someone who doesn't have a row yet),
    this only captures the tag and checks it isn't already someone
    else's — nothing is written to the database, since there's no row to
    write it onto yet; the admin page fills the tag into the Add form's
    hidden field instead, to be submitted along with the rest of that
    new person's details."""
    if not rfid_tag:
        return None

    with rfid_registration_lock:
        expires_at = rfid_registration_state["expires_at"]
        if rfid_registration_state["active"] and expires_at and now >= datetime.fromisoformat(expires_at):
            _reset_rfid_registration()
        if not rfid_registration_state["active"]:
            return None

        user_type = rfid_registration_state["user_type"]
        user_id = rfid_registration_state["user_id"]
        person_name = rfid_registration_state["person_name"]

        owner = _rfid_tag_owner(conn, rfid_tag)
        if owner and not (user_id is not None and owner[0] == user_type and owner[1] == user_id):
            message = f"Tag '{rfid_tag}' is already registered to {owner[2]} {owner[3]}."
            rfid_registration_state.update(active=False, status="error", message=message, tag=None)
            return message, "error"

        if user_id is not None:
            table = "teachers" if user_type == "teacher" else "students"
            id_column = "teacher_id" if user_type == "teacher" else "student_id"
            conn.execute(f"UPDATE {table} SET rfid_tag = ? WHERE {id_column} = ?", (rfid_tag, user_id))
            conn.commit()
            message = f"RFID tag '{rfid_tag}' registered to {person_name}."
        else:
            message = f"RFID tag '{rfid_tag}' captured."

        rfid_registration_state.update(active=False, status="success", message=message, tag=rfid_tag)
        return message, "success"


@app.route("/kiosk", methods=["GET", "POST"])
def kiosk():
    """
    Simulated RFID tap page. Stands in for the RC522 reader until the
    Raspberry Pi hardware is available. Students/teachers "tap" by typing
    their RFID tag or clicking one of the sample-tag buttons. Taps update
    the shared display state machine that the "/" screen polls.
    """
    conn = get_db()
    message = None
    message_type = "info"

    if request.method == "POST":
        rfid_tag = request.form.get("rfid_tag", "").strip()
        now = datetime.now()
        advance_state(conn, now)

        registration_result = _try_register_rfid_tap(conn, rfid_tag, now)
        if registration_result is not None:
            message, message_type = registration_result
        else:
            teacher = conn.execute(
                "SELECT * FROM teachers WHERE rfid_tag = ?", (rfid_tag,)
            ).fetchone()
            student = None
            if not teacher:
                student = conn.execute(
                    "SELECT * FROM students WHERE rfid_tag = ?", (rfid_tag,)
                ).fetchone()

            if teacher:
                message, message_type = handle_teacher_tap(conn, teacher, now)
            elif student:
                message, message_type = handle_student_tap(conn, student, now)
            else:
                message, message_type = f"Unknown tag: {rfid_tag}", "error"

    sample_students = conn.execute("SELECT * FROM students ORDER BY first_name").fetchall()
    sample_teachers = conn.execute("SELECT * FROM teachers ORDER BY first_name").fetchall()
    display_state = get_display_state(conn)
    conn.close()

    return render_template(
        "kiosk.html",
        message=message,
        message_type=message_type,
        sample_students=sample_students,
        sample_teachers=sample_teachers,
        display_state=display_state,
    )


@app.route("/admin")
@login_required(role="admin")
def admin():
    """Administrator landing page: one button per section (Teachers,
    Students, Classes, Rooms), each its own page — rather than one long
    page with everything on it."""
    return render_template("admin.html")


@app.route("/admin/teachers")
@login_required(role="admin")
def admin_teachers():
    conn = get_db()
    teachers = conn.execute("SELECT * FROM teachers ORDER BY first_name, last_name").fetchall()
    conn.close()
    return render_template("admin_teachers.html", teachers=teachers)


@app.route("/admin/students")
@login_required(role="admin")
def admin_students():
    conn = get_db()
    students = conn.execute("SELECT * FROM students ORDER BY first_name, last_name").fetchall()
    conn.close()
    return render_template("admin_students.html", students=students)


@app.route("/admin/rooms")
@login_required(role="admin")
def admin_rooms():
    conn = get_db()
    rooms = conn.execute("SELECT * FROM rooms ORDER BY room_name").fetchall()
    conn.close()
    return render_template("admin_rooms.html", rooms=rooms)


@app.route("/admin/classes")
@login_required(role="admin")
def admin_classes():
    conn = get_db()
    teachers = conn.execute("SELECT * FROM teachers ORDER BY first_name, last_name").fetchall()
    rooms = conn.execute("SELECT * FROM rooms ORDER BY room_name").fetchall()
    classes = conn.execute(
        """
        SELECT c.*, t.first_name || ' ' || t.last_name AS teacher_name
        FROM classes c
        LEFT JOIN teachers t ON t.teacher_id = c.teacher_id
        ORDER BY c.subject_name
        """
    ).fetchall()
    subjects = sorted({c["subject_name"] for c in classes})
    conn.close()
    return render_template(
        "admin_classes.html",
        teachers=teachers,
        rooms=rooms,
        classes=classes,
        subjects=subjects,
    )


@app.route("/admin/rfid/register/start", methods=["POST"])
@login_required(role="admin")
def admin_rfid_register_start():
    """Arms RFID registration for one teacher/student, so the next tap
    that hits /kiosk (real or simulated) gets consumed by it instead of
    being processed as a normal attendance/welcome tap.

    With a user_id: the "Register RFID" button next to an existing row —
    the tap gets written straight onto that row's rfid_tag.

    Without one (a brand-new person who doesn't have a row yet — the
    "Scan RFID Card" button on the Add Teacher/Add Student form): the tap
    is only captured and checked against every existing tag, not written
    anywhere; the admin page fills it into that form's hidden field."""
    user_type = request.form.get("user_type")
    user_id = request.form.get("user_id", type=int)
    if user_type not in ("teacher", "student"):
        return jsonify({"ok": False, "error": "Invalid target."}), 400

    if user_id:
        conn = get_db()
        if user_type == "teacher":
            row = conn.execute(
                "SELECT first_name, last_name FROM teachers WHERE teacher_id = ?", (user_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT first_name, last_name FROM students WHERE student_id = ?", (user_id,)
            ).fetchone()
        conn.close()
        if not row:
            return jsonify({"ok": False, "error": "Not found."}), 404
        person_name = f"{row['first_name']} {row['last_name']}"
    else:
        user_id = None
        person_name = f"a new {user_type}"

    with rfid_registration_lock:
        rfid_registration_state.update(
            active=True,
            user_type=user_type,
            user_id=user_id,
            person_name=person_name,
            status="waiting",
            tag=None,
            message=None,
            expires_at=(datetime.now() + timedelta(seconds=RFID_REGISTRATION_TIMEOUT_SECONDS)).isoformat(),
        )
    return jsonify({"ok": True})


@app.route("/admin/rfid/register/cancel", methods=["POST"])
@login_required(role="admin")
def admin_rfid_register_cancel():
    """Disarms RFID registration, whoever it was armed for."""
    with rfid_registration_lock:
        _reset_rfid_registration()
    return jsonify({"ok": True})


@app.route("/api/rfid_register_status")
@login_required(role="admin")
def api_rfid_register_status():
    """Polled by /admin's JS while waiting for a tap (see admin.html)."""
    now = datetime.now()
    with rfid_registration_lock:
        expires_at = rfid_registration_state["expires_at"]
        if rfid_registration_state["active"] and expires_at and now >= datetime.fromisoformat(expires_at):
            rfid_registration_state.update(active=False, status="error", message="Timed out waiting for a tap.")
        return jsonify(dict(rfid_registration_state))


@app.route("/admin/teachers/<int:teacher_id>")
@login_required(role="admin")
def admin_teacher_detail(teacher_id):
    """Per-teacher page: modify their classes (each meets every day once it
    has a room/time set — there's no separate schedule to arrange)."""
    conn = get_db()
    teacher = conn.execute("SELECT * FROM teachers WHERE teacher_id = ?", (teacher_id,)).fetchone()
    if not teacher:
        conn.close()
        flash("Teacher not found.", "error")
        return redirect(url_for("admin_teachers"))

    classes = conn.execute(
        "SELECT * FROM classes WHERE teacher_id = ? ORDER BY start_time", (teacher_id,)
    ).fetchall()
    rooms = conn.execute("SELECT * FROM rooms ORDER BY room_name").fetchall()

    conn.close()
    return render_template(
        "admin_teacher.html",
        teacher=teacher,
        classes=classes,
        rooms=rooms,
    )


@app.route("/admin/teachers/add", methods=["POST"])
@login_required(role="admin")
def admin_add_teacher():
    rfid_tag = request.form.get("rfid_tag", "").strip()
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    phone_number = request.form.get("phone_number", "").strip() or None
    if not (rfid_tag and first_name and last_name and username and password):
        flash("All teacher fields, including login username/password, are required.", "error")
    else:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO teachers (rfid_tag, first_name, last_name, username, password_hash, phone_number) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rfid_tag, first_name, last_name, username, generate_password_hash(password), phone_number),
            )
            conn.commit()
            flash(f"Added teacher {first_name} {last_name}.", "success")
        except sqlite3.IntegrityError:
            flash(f"RFID tag '{rfid_tag}' or username '{username}' is already in use.", "error")
        conn.close()
    return redirect(url_for("admin_teachers"))


@app.route("/admin/teachers/<int:teacher_id>/update", methods=["POST"])
@login_required(role="admin")
def admin_update_teacher(teacher_id):
    rfid_tag = request.form.get("rfid_tag", "").strip()
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    phone_number = request.form.get("phone_number", "").strip() or None
    if not (rfid_tag and first_name and last_name):
        flash("All teacher fields are required.", "error")
    else:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE teachers SET rfid_tag = ?, first_name = ?, last_name = ?, phone_number = ? WHERE teacher_id = ?",
                (rfid_tag, first_name, last_name, phone_number, teacher_id),
            )
            conn.commit()
            flash("Teacher updated.", "success")
        except sqlite3.IntegrityError:
            flash(f"RFID tag '{rfid_tag}' is already in use.", "error")
        conn.close()
    return redirect(url_for("admin_teachers"))


@app.route("/admin/teachers/<int:teacher_id>/delete", methods=["POST"])
@login_required(role="admin")
def admin_delete_teacher(teacher_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM teachers WHERE teacher_id = ?", (teacher_id,))
        conn.commit()
        flash("Teacher deleted.", "success")
    except sqlite3.IntegrityError:
        flash("Cannot delete this teacher — they're still assigned to a class. Reassign or delete that class first.", "error")
    conn.close()
    return redirect(url_for("admin_teachers"))


@app.route("/admin/teachers/<int:teacher_id>/reset_password", methods=["POST"])
@login_required(role="admin")
def admin_reset_teacher_password(teacher_id):
    new_password = request.form.get("new_password", "")
    if not new_password:
        flash("Enter a new password to reset it.", "error")
    else:
        conn = get_db()
        conn.execute(
            "UPDATE teachers SET password_hash = ? WHERE teacher_id = ?",
            (generate_password_hash(new_password), teacher_id),
        )
        conn.commit()
        conn.close()
        flash("Teacher's password has been reset.", "success")
    return redirect(url_for("admin_teachers"))


@app.route("/admin/students/add", methods=["POST"])
@login_required(role="admin")
def admin_add_student():
    rfid_tag = request.form.get("rfid_tag", "").strip()
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    section = request.form.get("section", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    phone_number = request.form.get("phone_number", "").strip() or None
    if not (rfid_tag and first_name and last_name and section and username and password):
        flash("All student fields, including login username/password, are required.", "error")
    else:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO students (rfid_tag, first_name, last_name, section, username, password_hash, phone_number) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rfid_tag, first_name, last_name, section, username, generate_password_hash(password), phone_number),
            )
            conn.commit()
            flash(f"Added student {first_name} {last_name}. Enroll them in a class from their Edit page.", "success")
        except sqlite3.IntegrityError:
            flash(f"RFID tag '{rfid_tag}' or username '{username}' is already in use.", "error")
        conn.close()
    return redirect(url_for("admin_students"))


@app.route("/admin/students/<int:student_id>/reset_password", methods=["POST"])
@login_required(role="admin")
def admin_reset_student_password(student_id):
    new_password = request.form.get("new_password", "")
    if not new_password:
        flash("Enter a new password to reset it.", "error")
    else:
        conn = get_db()
        conn.execute(
            "UPDATE students SET password_hash = ? WHERE student_id = ?",
            (generate_password_hash(new_password), student_id),
        )
        conn.commit()
        conn.close()
        flash("Student's password has been reset.", "success")
    return redirect(url_for("admin_student_detail", student_id=student_id))


@app.route("/admin/students/<int:student_id>/update", methods=["POST"])
@login_required(role="admin")
def admin_update_student(student_id):
    rfid_tag = request.form.get("rfid_tag", "").strip()
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    section = request.form.get("section", "").strip()
    phone_number = request.form.get("phone_number", "").strip() or None
    student_context = request.form.get("student_context", type=int)
    if not (rfid_tag and first_name and last_name and section):
        flash("All student fields are required.", "error")
    else:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE students SET rfid_tag = ?, first_name = ?, last_name = ?, section = ?, phone_number = ? "
                "WHERE student_id = ?",
                (rfid_tag, first_name, last_name, section, phone_number, student_id),
            )
            conn.commit()
            flash("Student updated.", "success")
        except sqlite3.IntegrityError:
            flash(f"RFID tag '{rfid_tag}' is already in use.", "error")
        conn.close()
    if student_context:
        return redirect(url_for("admin_student_detail", student_id=student_context))
    return redirect(url_for("admin_students"))


@app.route("/admin/students/<int:student_id>/delete", methods=["POST"])
@login_required(role="admin")
def admin_delete_student(student_id):
    conn = get_db()
    try:
        # Enrollments aren't a historical record worth blocking on — clear
        # them freely. Attendance history still blocks the delete below.
        conn.execute("DELETE FROM enrollments WHERE student_id = ?", (student_id,))
        conn.execute("DELETE FROM students WHERE student_id = ?", (student_id,))
        conn.commit()
        flash("Student deleted.", "success")
    except sqlite3.IntegrityError:
        flash("Cannot delete this student — they have attendance records tied to them.", "error")
    conn.close()
    return redirect(url_for("admin_students"))


@app.route("/admin/students/<int:student_id>")
@login_required(role="admin")
def admin_student_detail(student_id):
    """Per-student page: edit their credentials and manage which classes
    they're enrolled in. Enrolling is a two-step pick — a subject, then
    which specific offering of it (teacher + schedule) — since more than
    one teacher could be teaching sections of the same subject_name."""
    conn = get_db()
    student = conn.execute("SELECT * FROM students WHERE student_id = ?", (student_id,)).fetchone()
    if not student:
        conn.close()
        flash("Student not found.", "error")
        return redirect(url_for("admin_students"))

    enrollments = conn.execute(
        """
        SELECT e.class_id, c.subject_name, c.room_number, c.start_time, c.end_time,
               t.first_name || ' ' || t.last_name AS teacher_name
        FROM enrollments e
        JOIN classes c ON c.class_id = e.class_id
        JOIN teachers t ON t.teacher_id = c.teacher_id
        WHERE e.student_id = ?
        ORDER BY c.subject_name, c.start_time
        """,
        (student_id,),
    ).fetchall()
    enrolled_class_ids = {row["class_id"] for row in enrollments}

    # Only classes with a room/time already set are real, tappable offerings
    # — a class still missing those isn't ready to be enrolled into.
    tappable_classes = conn.execute(
        """
        SELECT c.class_id, c.subject_name, c.room_number, c.start_time, c.end_time,
               t.first_name || ' ' || t.last_name AS teacher_name
        FROM classes c
        JOIN teachers t ON t.teacher_id = c.teacher_id
        WHERE c.start_time IS NOT NULL AND c.end_time IS NOT NULL
        ORDER BY c.subject_name, c.start_time
        """
    ).fetchall()
    enrollable_classes = [dict(c) for c in tappable_classes if c["class_id"] not in enrolled_class_ids]
    enrollable_subjects = sorted({c["subject_name"] for c in enrollable_classes})

    conn.close()
    return render_template(
        "admin_student.html",
        student=student,
        enrollments=enrollments,
        enrollable_classes=enrollable_classes,
        enrollable_subjects=enrollable_subjects,
    )


@app.route("/admin/students/<int:student_id>/enroll", methods=["POST"])
@login_required(role="admin")
def admin_enroll_student(student_id):
    class_id = request.form.get("class_id", type=int)
    conn = get_db()
    class_row = (
        conn.execute(
            "SELECT subject_name, start_time, end_time FROM classes WHERE class_id = ?", (class_id,)
        ).fetchone()
        if class_id
        else None
    )

    if not class_row or not class_row["start_time"] or not class_row["end_time"]:
        flash("Pick a valid class schedule to enroll in.", "error")
    else:
        conflict = student_enrollment_conflict(
            conn, student_id, class_row["start_time"], class_row["end_time"]
        )
        if conflict:
            flash(
                f"This student is already enrolled in {conflict['subject_name']} "
                f"({conflict['start_time']}-{conflict['end_time']}) every day, which overlaps "
                f"{class_row['start_time']}-{class_row['end_time']} — pick a non-overlapping class.",
                "error",
            )
        else:
            try:
                conn.execute(
                    "INSERT INTO enrollments (student_id, class_id) VALUES (?, ?)",
                    (student_id, class_id),
                )
                conn.commit()
                flash(
                    f"Enrolled in {class_row['subject_name']} ({class_row['start_time']}–{class_row['end_time']}).",
                    "success",
                )
            except sqlite3.IntegrityError:
                flash("Already enrolled in that class.", "error")
    conn.close()
    return redirect(url_for("admin_student_detail", student_id=student_id))


@app.route("/admin/students/<int:student_id>/unenroll", methods=["POST"])
@login_required(role="admin")
def admin_unenroll_student(student_id):
    class_id = request.form.get("class_id", type=int)
    class_context = request.form.get("class_context", type=int)
    conn = get_db()
    conn.execute(
        "DELETE FROM enrollments WHERE student_id = ? AND class_id = ?",
        (student_id, class_id),
    )
    conn.commit()
    conn.close()
    flash("Removed from class. Their attendance history for it is kept.", "success")
    if class_context:
        return redirect(url_for("admin_class_enroll", class_id=class_context))
    return redirect(url_for("admin_student_detail", student_id=student_id))


@app.route("/admin/classes/<int:class_id>/enroll")
@login_required(role="admin")
def admin_class_enroll(class_id):
    """Admin's class-centric enrollment page — the "Enroll Students"
    button on the Classes table. The reverse direction of
    admin_student_detail()'s own "Enroll in a Class" form: pick students
    to add to *this* class, instead of picking a class to add one
    student to."""
    conn = get_db()
    class_info = conn.execute(
        """
        SELECT c.*, t.first_name || ' ' || t.last_name AS teacher_name
        FROM classes c
        LEFT JOIN teachers t ON t.teacher_id = c.teacher_id
        WHERE c.class_id = ?
        """,
        (class_id,),
    ).fetchone()
    if not class_info:
        conn.close()
        flash("Class not found.", "error")
        return redirect(url_for("admin_classes"))

    enrolled = conn.execute(
        """
        SELECT st.student_id, st.first_name, st.last_name, st.section
        FROM enrollments e
        JOIN students st ON st.student_id = e.student_id
        WHERE e.class_id = ?
        ORDER BY st.last_name, st.first_name
        """,
        (class_id,),
    ).fetchall()
    enrolled_ids = {r["student_id"] for r in enrolled}

    enrollable_students = [
        s for s in conn.execute(
            "SELECT student_id, first_name, last_name, section FROM students ORDER BY last_name, first_name"
        ).fetchall()
        if s["student_id"] not in enrolled_ids
    ]
    conn.close()

    return render_template(
        "admin_class_enroll.html",
        class_info=class_info,
        enrolled=enrolled,
        enrollable_students=enrollable_students,
    )


@app.route("/admin/classes/<int:class_id>/enroll/add", methods=["POST"])
@login_required(role="admin")
def admin_class_enroll_add(class_id):
    student_id = request.form.get("student_id", type=int)
    conn = get_db()
    class_row = conn.execute(
        "SELECT subject_name, start_time, end_time FROM classes WHERE class_id = ?", (class_id,)
    ).fetchone()

    if not student_id or not class_row or not class_row["start_time"] or not class_row["end_time"]:
        flash("Pick a student to enroll.", "error")
    else:
        conflict = student_enrollment_conflict(
            conn, student_id, class_row["start_time"], class_row["end_time"]
        )
        if conflict:
            flash(
                f"This student is already enrolled in {conflict['subject_name']} "
                f"({conflict['start_time']}-{conflict['end_time']}) every day, which overlaps "
                f"{class_row['start_time']}-{class_row['end_time']} — pick a different student.",
                "error",
            )
        else:
            try:
                conn.execute(
                    "INSERT INTO enrollments (student_id, class_id) VALUES (?, ?)",
                    (student_id, class_id),
                )
                conn.commit()
                flash("Student enrolled.", "success")
            except sqlite3.IntegrityError:
                flash("Already enrolled in that class.", "error")
    conn.close()
    return redirect(url_for("admin_class_enroll", class_id=class_id))


@app.route("/admin/rooms/add", methods=["POST"])
@login_required(role="admin")
def admin_add_room():
    """Just adds a room by name — there's no "Assign Room" step; a room is
    put to use by picking it from the room dropdown in Assign Class."""
    room_name = request.form.get("room_name", "").strip()
    if not room_name:
        flash("Room name is required.", "error")
    else:
        conn = get_db()
        try:
            conn.execute("INSERT INTO rooms (room_name) VALUES (?)", (room_name,))
            conn.commit()
            flash(f"Added room {room_name}.", "success")
        except sqlite3.IntegrityError:
            flash(f"Room '{room_name}' already exists.", "error")
        conn.close()
    return redirect(url_for("admin_rooms"))


@app.route("/admin/rooms/<int:room_id>/delete", methods=["POST"])
@login_required(role="admin")
def admin_delete_room(room_id):
    conn = get_db()
    room = conn.execute("SELECT room_name FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
    if not room:
        flash("Room not found.", "error")
    else:
        in_use = conn.execute(
            "SELECT 1 FROM classes WHERE room_number = ? LIMIT 1", (room["room_name"],)
        ).fetchone()
        if in_use:
            flash(
                f"Cannot delete room '{room['room_name']}' — it's still assigned to a class. "
                "Assign that class to a different room first.",
                "error",
            )
        else:
            conn.execute("DELETE FROM rooms WHERE room_id = ?", (room_id,))
            conn.commit()
            flash("Room deleted.", "success")
    conn.close()
    return redirect(url_for("admin_rooms"))


@app.route("/admin/classes/add", methods=["POST"])
@login_required(role="admin")
def admin_add_class():
    """Just creates the class by name — no teacher, room, or time yet.
    Assigning a teacher and schedule is a separate step (see
    admin_assign_class)."""
    subject_name = request.form.get("subject_name", "").strip()

    if not subject_name:
        flash("Subject name is required.", "error")
    else:
        conn = get_db()
        conn.execute("INSERT INTO classes (subject_name) VALUES (?)", (subject_name,))
        conn.commit()
        conn.close()
        flash(
            f"Added class {subject_name}. Assign it to a teacher and schedule it below once you're ready.",
            "success",
        )
    return redirect(url_for("admin_classes"))


@app.route("/admin/classes/assign", methods=["POST"])
@login_required(role="admin")
def admin_assign_class():
    """Assigns a class to a teacher and sets its room/time. Used both by
    the main "Assign Class" form on /admin — which picks a *subject*, not
    a specific row, since the same subject can now have several sections
    — and by each class card on a teacher's own page (a specific
    class_id/teacher_id passed as hidden fields, along with
    teacher_context, fixed to that page's teacher).

    From the subject-based form: if that subject already has an
    unassigned row (freshly added via "Add a Class" and never scheduled),
    this fills that one in — a first-time assignment, nothing to
    preserve. Otherwise — the subject's existing row(s) all already
    belong to some teacher, the same one being submitted here or a
    different one — this always adds a brand new row (a new "section")
    rather than overwriting any of them, as long as the new schedule
    doesn't conflict with anything that teacher (or that room) already
    has. That's how the same teacher ends up able to teach two or more
    sections of one subject at different times.

    The one path that still edits a row in place rather than adding a
    new one is teacher_context: each class card on a teacher's own admin
    page means "reschedule this specific class", not "add a new
    section" — that's the only place teacher_context gets submitted (the
    main Assign Class form never sends it), so its presence unambiguously
    signals an in-place edit."""
    class_id = request.form.get("class_id", type=int)
    subject_name = request.form.get("subject_name", "").strip()
    teacher_id = request.form.get("teacher_id", type=int)
    room_number = request.form.get("room_number", "").strip()
    start_time = request.form.get("start_time", "").strip()
    end_time = request.form.get("end_time", "").strip()
    attendance_window_minutes = request.form.get("attendance_window_minutes", type=int) or 15
    teacher_context = request.form.get("teacher_context", type=int)

    if not ((class_id or subject_name) and teacher_id):
        flash("Pick a class and a teacher to assign.", "error")
    elif bool(start_time) != bool(end_time):
        flash("Set both a start time and an end time, or leave both blank.", "error")
    elif start_time and start_time >= end_time:
        flash("Class start time must be before its end time.", "error")
    elif attendance_window_minutes < 1:
        flash("Attendance window must be at least 1 minute.", "error")
    else:
        conn = get_db()

        if class_id:
            # A specific row (a teacher's own class card) — always this
            # exact one, never a substitute.
            picked = conn.execute(
                "SELECT class_id, subject_name, teacher_id FROM classes WHERE class_id = ?", (class_id,)
            ).fetchone()
            if not picked:
                flash("That class no longer exists.", "error")
                conn.close()
                if teacher_context:
                    return redirect(url_for("admin_teacher_detail", teacher_id=teacher_context))
                return redirect(url_for("admin_classes"))
        else:
            # A subject, not a row — reuse an unassigned row of it if one
            # exists (so "Add a Class" + "Assign Class" for a brand new
            # subject just fills that one row in); otherwise there's no
            # row to reuse, and this will insert a brand new section.
            picked = conn.execute(
                "SELECT class_id, subject_name, teacher_id FROM classes "
                "WHERE subject_name = ? AND teacher_id IS NULL ORDER BY class_id LIMIT 1",
                (subject_name,),
            ).fetchone()
            if not picked:
                picked = {"class_id": None, "subject_name": subject_name, "teacher_id": None}

        # In-place edit only when this came from that exact teacher's own
        # class card (teacher_context); a brand new row (nothing to
        # reuse) or an already-assigned row being re-picked for anyone
        # else — same teacher or a different one — always adds a new
        # section instead of overwriting it.
        is_in_place_edit = teacher_context is not None and teacher_context == teacher_id
        is_new_section = picked["class_id"] is None or (picked["teacher_id"] is not None and not is_in_place_edit)
        # Only excluded from conflict checks when editing that same row in
        # place — a new section's schedule should be checked against every
        # class including the one it was picked from (even if it's the
        # same teacher's own other section of this same subject).
        conflict_exclude = None if is_new_section else picked["class_id"]

        teacher_conflict = None
        room_conflict = None
        if start_time and end_time:
            teacher_conflict = class_time_conflict(conn, teacher_id, start_time, end_time, exclude_class_id=conflict_exclude)
            if not teacher_conflict and room_number:
                room_conflict = room_time_conflict(conn, room_number, start_time, end_time, exclude_class_id=conflict_exclude)

        if teacher_conflict:
            flash(
                f"This teacher already teaches {teacher_conflict['subject_name']} "
                f"({teacher_conflict['start_time']}-{teacher_conflict['end_time']}) every day, which overlaps "
                f"{start_time}-{end_time} — pick a non-overlapping time.",
                "error",
            )
        elif room_conflict:
            flash(
                f"Room '{room_number}' is already booked for {room_conflict['subject_name']} "
                f"({room_conflict['start_time']}-{room_conflict['end_time']}) every day, which overlaps "
                f"{start_time}-{end_time} — pick a different room or time. Two different teachers can "
                f"share a time slot fine, just not the same room.",
                "error",
            )
        elif is_new_section:
            conn.execute(
                """
                INSERT INTO classes (subject_name, teacher_id, room_number, start_time, end_time, attendance_window_minutes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (picked["subject_name"], teacher_id, room_number or None, start_time or None,
                 end_time or None, attendance_window_minutes),
            )
            conn.commit()
            flash(f"Added a new section of {picked['subject_name']} for this teacher.", "success")
        else:
            conn.execute(
                """
                UPDATE classes
                SET teacher_id = ?, room_number = ?, start_time = ?,
                    end_time = ?, attendance_window_minutes = ?
                WHERE class_id = ?
                """,
                (teacher_id, room_number or None, start_time or None,
                 end_time or None, attendance_window_minutes, picked["class_id"]),
            )
            conn.commit()
            flash("Class assigned.", "success")
        conn.close()

    if teacher_context:
        return redirect(url_for("admin_teacher_detail", teacher_id=teacher_context))
    return redirect(url_for("admin_classes"))


@app.route("/admin/classes/<int:class_id>/delete", methods=["POST"])
@login_required(role="admin")
def admin_delete_class(class_id):
    teacher_context = request.form.get("teacher_context", type=int)
    conn = get_db()
    # Deleting a class deletes its attendance history and enrollments along
    # with it, rather than getting stuck behind foreign keys. There's a
    # confirm() prompt in the UI before this route is ever called, so this
    # isn't a silent wipe.
    conn.execute("DELETE FROM attendance WHERE class_id = ?", (class_id,))
    conn.execute("DELETE FROM enrollments WHERE class_id = ?", (class_id,))
    conn.execute("DELETE FROM classes WHERE class_id = ?", (class_id,))
    conn.commit()
    conn.close()
    flash("Class deleted.", "success")

    if teacher_context:
        return redirect(url_for("admin_teacher_detail", teacher_id=teacher_context))
    return redirect(url_for("admin_classes"))


def get_roster_for_date(conn, class_id, date_str):
    """Full roster of students *enrolled in this class* for one date, with
    present/late/excused/absent counts — an enrolled student with no
    attendance row that day shows as 'absent'. A student not enrolled in
    this class never appears here, even if they happen to have an old
    attendance row for it (e.g. from before being unenrolled). Shared by
    /summary (today only) and the teacher dashboard's history section
    (any past date)."""
    roster = conn.execute(
        """
        SELECT st.student_id, st.first_name, st.last_name, st.section,
               a.time_tapped, COALESCE(a.status, 'absent') AS status
        FROM enrollments e
        JOIN students st ON st.student_id = e.student_id
        LEFT JOIN attendance a
               ON a.student_id = st.student_id
              AND a.class_id = ?
              AND a.date = ?
        WHERE e.class_id = ?
        ORDER BY st.last_name, st.first_name
        """,
        (class_id, date_str, class_id),
    ).fetchall()
    present_count = sum(1 for r in roster if r["status"] == "present")
    late_count = sum(1 for r in roster if r["status"] == "late")
    excused_count = sum(1 for r in roster if r["status"] == "excused")
    absent_count = sum(1 for r in roster if r["status"] == "absent")
    return roster, present_count, late_count, excused_count, absent_count


@app.route("/dashboard")
@login_required(role="teacher")
def dashboard():
    """Teacher-facing dashboard: today's sessions for this teacher's classes
    (with live/completed attendance counts), plus a filterable history of
    past sessions across all of their classes."""
    conn = get_db()
    teacher_id = session["user_id"]
    teacher = conn.execute("SELECT * FROM teachers WHERE teacher_id = ?", (teacher_id,)).fetchone()

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    # Every class with a room/time set meets every day, so "today's
    # sessions" is just this teacher's classes that have a time set.
    todays_classes = conn.execute(
        """
        SELECT class_id, subject_name, room_number, start_time, end_time
        FROM classes
        WHERE teacher_id = ? AND start_time IS NOT NULL AND end_time IS NOT NULL
        ORDER BY start_time
        """,
        (teacher_id,),
    ).fetchall()

    today_sessions = []
    for c in todays_classes:
        roster, present_count, late_count, excused_count, absent_count = get_roster_for_date(conn, c["class_id"], today)
        ended_early = (c["class_id"], today) in _early_ended_sessions
        status = "Completed" if ended_early or now.strftime("%H:%M") >= c["end_time"] else \
                 "In progress" if now.strftime("%H:%M") >= c["start_time"] else "Not started"
        today_sessions.append({
            "class_id": c["class_id"],
            "subject_name": c["subject_name"],
            "room_number": c["room_number"],
            "start_time": c["start_time"],
            "end_time": c["end_time"],
            "status": status,
            "present_count": present_count,
            "late_count": late_count,
            "excused_count": excused_count,
            "absent_count": absent_count,
            "total": len(roster),
        })

    all_classes = conn.execute(
        "SELECT * FROM classes WHERE teacher_id = ? ORDER BY start_time", (teacher_id,)
    ).fetchall()

    filter_class_id = request.args.get("class_id", type=int)
    filter_date = request.args.get("date", "").strip()
    if filter_class_id and all_classes and filter_class_id not in {c["class_id"] for c in all_classes}:
        filter_class_id = None  # not this teacher's class

    history_roster = None
    history_counts = None
    history_dates = []

    if filter_class_id and filter_date:
        history_roster, present_count, late_count, excused_count, absent_count = get_roster_for_date(conn, filter_class_id, filter_date)
        history_counts = {"present": present_count, "late": late_count, "excused": excused_count, "absent": absent_count}
    elif filter_class_id:
        total_students = conn.execute(
            "SELECT COUNT(*) AS c FROM enrollments WHERE class_id = ?", (filter_class_id,)
        ).fetchone()["c"]
        rows = conn.execute(
            """
            SELECT date,
                   SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END) AS present_count,
                   SUM(CASE WHEN status = 'late' THEN 1 ELSE 0 END) AS late_count,
                   SUM(CASE WHEN status = 'excused' THEN 1 ELSE 0 END) AS excused_count
            FROM attendance
            WHERE class_id = ?
            GROUP BY date
            ORDER BY date DESC
            """,
            (filter_class_id,),
        ).fetchall()
        history_dates = [
            {
                "date": r["date"],
                "present_count": r["present_count"],
                "late_count": r["late_count"],
                "excused_count": r["excused_count"],
                "absent_count": total_students - r["present_count"] - r["late_count"] - r["excused_count"],
            }
            for r in rows
        ]

    conn.close()
    return render_template(
        "dashboard.html",
        teacher=teacher,
        today=today,
        today_sessions=today_sessions,
        all_classes=all_classes,
        filter_class_id=filter_class_id,
        filter_date=filter_date,
        history_roster=history_roster,
        history_counts=history_counts,
        history_dates=history_dates,
        vapid_public_key=VAPID_PUBLIC_KEY,
    )


@app.route("/dashboard/sms_broadcast", methods=["POST"])
@login_required(role="teacher")
def dashboard_sms_broadcast():
    """Lets a teacher send a one-off SMS to everyone enrolled in one of
    their own classes — separate from the automatic notification triggers
    (10-minute reminder, attendance opening, session ended), this is a
    message the teacher writes themselves, whenever they want (e.g. "no
    class today" or "bring your project files tomorrow"). The class and
    teacher's name are prefixed onto the actual text sent, so a student
    receiving it out of context (no app open, just a bare SMS) can still
    tell at a glance who it's from and which class it's about — the
    teacher only types the body."""
    teacher_id = session["user_id"]
    class_id = request.form.get("class_id", type=int)
    message = request.form.get("message", "").strip()

    conn = get_db()
    class_row = conn.execute(
        """
        SELECT c.subject_name, t.first_name || ' ' || t.last_name AS teacher_name
        FROM classes c
        JOIN teachers t ON t.teacher_id = c.teacher_id
        WHERE c.class_id = ? AND c.teacher_id = ?
        """,
        (class_id, teacher_id),
    ).fetchone()

    if not class_row:
        flash("Pick one of your own classes to message.", "error")
    elif not message:
        flash("Enter a message to send.", "error")
    else:
        students = conn.execute(
            """
            SELECT s.phone_number
            FROM enrollments e
            JOIN students s ON s.student_id = e.student_id
            WHERE e.class_id = ?
            """,
            (class_id,),
        ).fetchall()
        recipients = [s["phone_number"] for s in students if s["phone_number"]]
        full_message = f"{class_row['teacher_name']} ({class_row['subject_name']}): {message}"
        for phone_number in recipients:
            send_sms_notification(phone_number, full_message)

        no_phone_count = len(students) - len(recipients)
        if not students:
            flash(f"No students enrolled in {class_row['subject_name']}.", "error")
        else:
            note = f" ({no_phone_count} enrolled student(s) have no phone number on file.)" if no_phone_count else ""
            flash(f"Sent to {len(recipients)} of {len(students)} students in {class_row['subject_name']}.{note}", "success")

    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/dashboard/attendance/update", methods=["POST"])
@login_required(role="teacher")
def dashboard_attendance_update():
    """Lets a teacher correct one student's attendance status for a past
    (or today's) date in one of their own classes — used by both the
    Attendance History section on /dashboard and a student's monthly log
    on dashboard_student_monthly(). Unlike the live session_summary_edit
    toggle (where absent means no row at all — there's nothing to
    correct or look back on there, it's just the default before a tap),
    an explicit correction here always writes a real row, absent
    included: this is a teacher deliberately recording "yes, I checked,
    this student was absent that day," not just an untouched gap, so it
    stays visible and editable in these two logs instead of vanishing.
    get_roster_for_date() and every other reader still treat a missing
    row as absent too, so this doesn't change how absence displays
    anywhere else — it only means an explicitly-reviewed day keeps a
    record of having been reviewed."""
    teacher_id = session["user_id"]
    student_id = request.form.get("student_id", type=int)
    class_id = request.form.get("class_id", type=int)
    date_str = request.form.get("date", "").strip()
    status = request.form.get("status", "").strip()
    redirect_to = request.form.get("redirect_to", "").strip()
    month = request.form.get("month", "").strip()

    conn = get_db()
    owns_class = conn.execute(
        "SELECT 1 FROM classes WHERE class_id = ? AND teacher_id = ?", (class_id, teacher_id)
    ).fetchone()
    is_enrolled = conn.execute(
        "SELECT 1 FROM enrollments WHERE student_id = ? AND class_id = ?", (student_id, class_id)
    ).fetchone()

    if not (owns_class and is_enrolled and date_str and status in SUMMARY_TOGGLE_CYCLE):
        flash("Couldn't update that attendance record.", "error")
    else:
        existing = conn.execute(
            "SELECT status FROM attendance WHERE student_id = ? AND class_id = ? AND date = ?",
            (student_id, class_id, date_str),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE attendance SET status = ? WHERE student_id = ? AND class_id = ? AND date = ?",
                (status, student_id, class_id, date_str),
            )
        else:
            conn.execute(
                """
                INSERT INTO attendance (student_id, class_id, date, time_tapped, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (student_id, class_id, date_str, datetime.now().strftime("%H:%M:%S"), status),
            )
        conn.commit()
        flash("Attendance updated.", "success")
    conn.close()

    if redirect_to == "monthly":
        return redirect(url_for("dashboard_student_monthly", class_id=class_id, student_id=student_id, month=month))
    return redirect(url_for("dashboard", class_id=class_id, date=date_str))


@app.route("/dashboard/classes/<int:class_id>")
@login_required(role="teacher")
def dashboard_class_roster(class_id):
    """A teacher's own view of who's enrolled in one of their classes —
    the "View" button next to each entry under My Classes on /dashboard.
    Only reachable for a class actually assigned to this teacher."""
    teacher_id = session["user_id"]
    conn = get_db()
    class_info = conn.execute(
        """
        SELECT c.*, t.first_name || ' ' || t.last_name AS teacher_name
        FROM classes c
        JOIN teachers t ON t.teacher_id = c.teacher_id
        WHERE c.class_id = ? AND c.teacher_id = ?
        """,
        (class_id, teacher_id),
    ).fetchone()
    if not class_info:
        conn.close()
        flash("Class not found.", "error")
        return redirect(url_for("dashboard"))

    students = conn.execute(
        """
        SELECT st.student_id, st.first_name, st.last_name, st.section
        FROM enrollments e
        JOIN students st ON st.student_id = e.student_id
        WHERE e.class_id = ?
        ORDER BY st.last_name, st.first_name
        """,
        (class_id,),
    ).fetchall()
    conn.close()
    return render_template(
        "class_roster.html",
        class_info=class_info,
        students=students,
        back_url=url_for("dashboard"),
        back_label="Back to Dashboard",
        can_view_monthly=True,
    )


@app.route("/dashboard/classes/<int:class_id>/students/<int:student_id>")
@login_required(role="teacher")
def dashboard_student_monthly(class_id, student_id):
    """A teacher's per-student, per-month attendance summary for one of
    their own classes — the "View" button next to each student on the
    class roster page (/dashboard/classes/<id>). Only present/late/
    excused/absent rows that actually exist in the attendance table
    count toward the totals and the day-by-day log; a day with no row
    for this student isn't assumed absent — the class might simply not
    have been held/tracked that day — the same convention already used
    for a student's own history on /student-dashboard."""
    teacher_id = session["user_id"]
    month = request.args.get("month", "").strip()
    try:
        month_date = datetime.strptime(month, "%Y-%m") if month else datetime.now()
    except ValueError:
        month_date = datetime.now()
    month = month_date.strftime("%Y-%m")

    conn = get_db()
    class_info = conn.execute(
        "SELECT * FROM classes WHERE class_id = ? AND teacher_id = ?",
        (class_id, teacher_id),
    ).fetchone()
    if not class_info:
        conn.close()
        flash("Class not found.", "error")
        return redirect(url_for("dashboard"))

    student = conn.execute(
        """
        SELECT st.* FROM students st
        JOIN enrollments e ON e.student_id = st.student_id
        WHERE st.student_id = ? AND e.class_id = ?
        """,
        (student_id, class_id),
    ).fetchone()
    if not student:
        conn.close()
        flash("That student isn't enrolled in this class.", "error")
        return redirect(url_for("dashboard_class_roster", class_id=class_id))

    logs = conn.execute(
        """
        SELECT date, status, time_tapped
        FROM attendance
        WHERE student_id = ? AND class_id = ? AND date LIKE ?
        ORDER BY date
        """,
        (student_id, class_id, f"{month}-%"),
    ).fetchall()
    conn.close()

    counts = {"present": 0, "late": 0, "excused": 0, "absent": 0}
    for row in logs:
        counts[row["status"]] += 1

    first_of_month = month_date.replace(day=1)
    prev_month = (first_of_month - timedelta(days=1)).strftime("%Y-%m")
    next_month = (first_of_month + timedelta(days=32)).replace(day=1).strftime("%Y-%m")

    return render_template(
        "dashboard_student_monthly.html",
        class_info=class_info,
        student=student,
        month=month,
        month_label=month_date.strftime("%B %Y"),
        prev_month=prev_month,
        next_month=next_month,
        logs=logs,
        counts=counts,
    )


@app.route("/admin/classes/<int:class_id>/students")
@login_required(role="admin")
def admin_class_roster(class_id):
    """Admin's view of who's enrolled in one class — the "View Students"
    button both on the main Classes table and on each class card in a
    teacher's admin detail page. Shares the same template as
    dashboard_class_roster(), just without the ownership restriction (an
    admin can look at any class).

    The ?source= query param says which of those two pages linked here,
    so "back" returns to the right one — it can't be inferred from the
    class's own data (e.g. whether it has a teacher), since either page
    can link to the same class."""
    conn = get_db()
    class_info = conn.execute(
        """
        SELECT c.*, t.first_name || ' ' || t.last_name AS teacher_name
        FROM classes c
        LEFT JOIN teachers t ON t.teacher_id = c.teacher_id
        WHERE c.class_id = ?
        """,
        (class_id,),
    ).fetchone()
    if not class_info:
        conn.close()
        flash("Class not found.", "error")
        return redirect(url_for("admin_classes"))

    students = conn.execute(
        """
        SELECT st.student_id, st.first_name, st.last_name, st.section
        FROM enrollments e
        JOIN students st ON st.student_id = e.student_id
        WHERE e.class_id = ?
        ORDER BY st.last_name, st.first_name
        """,
        (class_id,),
    ).fetchall()
    conn.close()

    source = request.args.get("source")
    if source == "teacher" and class_info["teacher_id"]:
        back_url = url_for("admin_teacher_detail", teacher_id=class_info["teacher_id"])
        back_label = "Back to Teacher"
    else:
        back_url = url_for("admin_classes")
        back_label = "Back to Classes"

    return render_template(
        "class_roster.html",
        class_info=class_info,
        students=students,
        back_url=back_url,
        back_label=back_label,
    )


def get_student_attendance_history(conn, student_id, class_id):
    """Every date this student has an attendance row for this class, most
    recent first — the per-student analogue of get_roster_for_date(), just
    scoped to one student's own history in one class instead of a whole
    class's roster on one date."""
    return conn.execute(
        """
        SELECT date, status, time_tapped
        FROM attendance
        WHERE student_id = ? AND class_id = ?
        ORDER BY date DESC
        """,
        (student_id, class_id),
    ).fetchall()


@app.route("/student-dashboard")
@login_required(role="student")
def student_dashboard():
    """Student-facing dashboard: the classes this student is enrolled in,
    plus a filterable history of their own attendance across those
    classes. Everything here is scoped to session["user_id"] — a student
    only ever sees their own enrollments and attendance, never anyone
    else's."""
    conn = get_db()
    student_id = session["user_id"]
    student = conn.execute("SELECT * FROM students WHERE student_id = ?", (student_id,)).fetchone()

    enrollments = conn.execute(
        """
        SELECT e.class_id, c.subject_name, c.room_number, c.start_time, c.end_time,
               t.first_name || ' ' || t.last_name AS teacher_name
        FROM enrollments e
        JOIN classes c ON c.class_id = e.class_id
        LEFT JOIN teachers t ON t.teacher_id = c.teacher_id
        WHERE e.student_id = ?
        ORDER BY c.subject_name
        """,
        (student_id,),
    ).fetchall()
    enrolled_class_ids = {e["class_id"] for e in enrollments}

    filter_class_id = request.args.get("class_id", type=int)
    filter_date = request.args.get("date", "").strip()
    if filter_class_id and filter_class_id not in enrolled_class_ids:
        filter_class_id = None  # not this student's class

    history_rows = []
    history_entry = None
    if filter_class_id:
        history_rows = get_student_attendance_history(conn, student_id, filter_class_id)
        if filter_date:
            history_entry = next((r for r in history_rows if r["date"] == filter_date), None)

    conn.close()
    return render_template(
        "student_dashboard.html",
        student=student,
        enrollments=enrollments,
        filter_class_id=filter_class_id,
        filter_date=filter_date,
        history_rows=history_rows,
        history_entry=history_entry,
        vapid_public_key=VAPID_PUBLIC_KEY,
    )


@app.route("/summary")
def summary():
    """End-of-session attendance summary, filterable by class."""
    conn = get_db()
    classes = conn.execute(
        """
        SELECT c.*, t.first_name || ' ' || t.last_name AS teacher_name
        FROM classes c
        JOIN teachers t ON t.teacher_id = c.teacher_id
        ORDER BY c.start_time
        """
    ).fetchall()

    selected_class_id = request.args.get("class_id", type=int)
    if not selected_class_id and classes:
        selected_class_id = classes[0]["class_id"]

    roster = []
    present_count = late_count = excused_count = absent_count = 0
    selected_class = None

    if selected_class_id:
        selected_class = conn.execute(
            """
            SELECT c.*, t.first_name || ' ' || t.last_name AS teacher_name
            FROM classes c
            JOIN teachers t ON t.teacher_id = c.teacher_id
            WHERE c.class_id = ?
            """,
            (selected_class_id,),
        ).fetchone()

        roster, present_count, late_count, excused_count, absent_count = get_roster_for_date(
            conn, selected_class_id, date.today().isoformat()
        )

    conn.close()
    return render_template(
        "summary.html",
        classes=classes,
        selected_class=selected_class,
        roster=roster,
        present_count=present_count,
        late_count=late_count,
        excused_count=excused_count,
        absent_count=absent_count,
        today=date.today().isoformat(),
    )


@app.route("/api/status")
def api_status():
    """Simple JSON endpoint (current class name, present count, total students)."""
    conn = get_db()
    current_class = get_current_class(conn)
    data = {"current_class": None, "present_count": 0, "total_students": 0}

    if current_class:
        today = date.today().isoformat()
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM enrollments WHERE class_id = ?", (current_class["class_id"],)
        ).fetchone()["c"]
        present = conn.execute(
            "SELECT COUNT(*) AS c FROM attendance WHERE class_id = ? AND date = ?",
            (current_class["class_id"], today),
        ).fetchone()["c"]
        data["current_class"] = current_class["subject_name"]
        data["present_count"] = present
        data["total_students"] = total

    conn.close()
    return jsonify(data)


@app.route("/api/push_subscribe", methods=["POST"])
def api_push_subscribe():
    """Registers a browser push subscription for the logged-in teacher or
    student (whichever role the session holds — this route doesn't accept
    a role/user_id from the client, only from the session, so a caller
    can't register a subscription on someone else's behalf). Called by the
    small JS snippet on dashboard.html/student_dashboard.html after the
    user grants notification permission. Re-subscribing the same
    endpoint (e.g. the page reloading) is a harmless no-op, caught via the
    endpoint UNIQUE constraint."""
    role = session.get("role")
    user_id = session.get("user_id")
    if role not in ("teacher", "student") or not user_id:
        return jsonify({"error": "not logged in"}), 401

    payload = request.get_json(silent=True) or {}
    endpoint = payload.get("endpoint")
    keys = payload.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not (endpoint and p256dh and auth):
        return jsonify({"error": "invalid subscription"}), 400

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO push_subscriptions (user_type, user_id, endpoint, p256dh_key, auth_key) "
            "VALUES (?, ?, ?, ?, ?)",
            (role, user_id, endpoint, p256dh, auth),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # already subscribed with this endpoint
    conn.close()
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # host="0.0.0.0" (rather than the Flask default of 127.0.0.1-only) so
    # other devices on the same WiFi/hotspot can reach this — the Pi's
    # rfid_listener.py when app.py runs on a laptop instead of the Pi
    # itself, or any other browser wanting to view the display.
    app.run(host="0.0.0.0", debug=True, port=5000, threaded=True)
