"""
Dev helper: fills roomsync.db with realistic past attendance so the
Attendance Records, Reports and student dashboards have history to show.

- Adds a set of sample students (realistic names and sections) if they
  aren't there yet. Their RFID tags are placeholders (no physical card) and
  they get no portal login.
- Enrolls them across the scheduled classes (a class needs a teacher and a
  start/end time), skipping any enrollment that already exists.
- For every weekday from SEMESTER_START up to yesterday, except holidays
  and the odd cancelled class, writes each enrolled student's status:
  present (tapped within the attendance window), late (tapped after it,
  before class ended), excused (rare), or absent (no row, which is how the
  app records an absence). Every student has their own habits, so some are
  nearly always on time and a few are often late or absent.
- Never touches today, and skips any class/date that already has rows, so
  it's safe to run more than once and leaves real taps alone.

Run:  python seed_history.py
"""

import random
import sqlite3
from datetime import date, datetime, timedelta

DB_PATH = "roomsync.db"
SEMESTER_START = date(2026, 8, 17)
HOLIDAYS = {
    date(2026, 8, 21),   # Ninoy Aquino Day
    date(2026, 8, 31),   # National Heroes Day
    date(2026, 11, 2),   # All Souls' Day
}
CANCELLED_CHANCE = 0.03  # a class occasionally doesn't meet (event, teacher away)

# (rfid_tag placeholder, first, last, section)
SAMPLE_STUDENTS = [
    ("SIM-0001", "Angelica", "Reyes", "BSIT-3"),
    ("SIM-0002", "Mark Anthony", "Villanueva", "BSIT-3"),
    ("SIM-0003", "Kristine Joy", "Bautista", "BSIT-3"),
    ("SIM-0004", "John Paul", "Mendoza", "BSIT-3"),
    ("SIM-0005", "Ella Marie", "Cruz", "BSIT-3"),
    ("SIM-0006", "Rhea Mae", "Garcia", "BSIT-2"),
    ("SIM-0007", "Christian Dave", "Ramos", "BSIT-2"),
    ("SIM-0008", "Princess Nicole", "Aquino", "BSIT-2"),
    ("SIM-0009", "Jerome", "Castillo", "BSIT-2"),
    ("SIM-0010", "Joshua", "Manalo", "BSIT-2"),
    ("SIM-0011", "Mary Grace", "Torres", "BSIT-4"),
    ("SIM-0012", "Kenneth", "Lim", "BSIT-4"),
    ("SIM-0013", "Jasmine", "Flores", "BSIT-4"),
    ("SIM-0014", "Ralph Lorenzo", "Navarro", "BSIT-4"),
    ("SIM-0015", "Trisha Mae", "Gonzales", "BSIT-1"),
    ("SIM-0016", "Carlo Miguel", "Pascual", "BSIT-1"),
    ("SIM-0017", "Danica Rose", "Salazar", "BSIT-1"),
    ("SIM-0018", "Neil Bryan", "Domingo", "BSIT-1"),
]

# Attendance habits: (chance absent, chance late, chance excused) per session.
PROFILES = {
    "diligent": (0.02, 0.04, 0.01),
    "average": (0.06, 0.12, 0.02),
    "often_late": (0.05, 0.32, 0.02),
    "often_absent": (0.20, 0.12, 0.03),
}
PROFILE_WEIGHTS = {"diligent": 5, "average": 6, "often_late": 2, "often_absent": 1}


def minutes_after(hhmm, minutes, rng):
    t = datetime.strptime(hhmm, "%H:%M") + timedelta(minutes=minutes, seconds=rng.randint(0, 59))
    return t.strftime("%H:%M:%S")


def class_length(c):
    start = datetime.strptime(c["start_time"], "%H:%M")
    end = datetime.strptime(c["end_time"], "%H:%M")
    return int((end - start).total_seconds() // 60)


def main():
    rng = random.Random()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1. Sample students
    added_students = 0
    for tag, first, last, section in SAMPLE_STUDENTS:
        if not conn.execute("SELECT 1 FROM students WHERE rfid_tag = ?", (tag,)).fetchone():
            conn.execute(
                "INSERT INTO students (rfid_tag, first_name, last_name, section) VALUES (?, ?, ?, ?)",
                (tag, first, last, section),
            )
            added_students += 1

    classes = conn.execute(
        """
        SELECT class_id, subject_name, start_time, end_time, attendance_window_minutes
        FROM classes
        WHERE teacher_id IS NOT NULL AND start_time IS NOT NULL AND end_time IS NOT NULL
        ORDER BY start_time
        """
    ).fetchall()
    if not classes:
        print("No scheduled classes (teacher + start/end time) to add history for.")
        conn.commit()
        return

    # 2. Enroll every student in every scheduled class that doesn't overlap
    #    one they're already in — a full block schedule, the way a section
    #    takes its subjects. Same overlap rule as student_enrollment_conflict()
    #    in app.py, so back-to-back classes (09:30 end, 09:30 start) are fine.
    def overlaps(a, b):
        return a["start_time"] < b["end_time"] and b["start_time"] < a["end_time"]

    by_id = {c["class_id"]: c for c in classes}
    added_enrollments = 0
    for (sid,) in conn.execute("SELECT student_id FROM students ORDER BY student_id").fetchall():
        mine = [
            by_id[r["class_id"]]
            for r in conn.execute("SELECT class_id FROM enrollments WHERE student_id = ?", (sid,))
            if r["class_id"] in by_id
        ]
        for c in classes:
            if any(c["class_id"] == other["class_id"] or overlaps(c, other) for other in mine):
                continue
            conn.execute("INSERT INTO enrollments (student_id, class_id) VALUES (?, ?)", (sid, c["class_id"]))
            mine.append(c)
            added_enrollments += 1

    # 3. Each student's habits: taken from their existing history when they
    #    have some (so a student who's often late stays often late in every
    #    class), otherwise a random profile.
    profile_names = list(PROFILE_WEIGHTS)
    profile_of = {}
    today = date.today()

    def observed_profile(student_id):
        held = absent = late = excused = 0
        for (cid,) in conn.execute("SELECT class_id FROM enrollments WHERE student_id = ?", (student_id,)).fetchall():
            dates = [r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM attendance WHERE class_id = ? AND date < ?", (cid, today.isoformat()))]
            mine = dict(conn.execute(
                "SELECT date, status FROM attendance WHERE class_id = ? AND student_id = ? AND date < ?",
                (cid, student_id, today.isoformat())).fetchall())
            if not mine:
                continue  # not part of this class's history yet
            for d in dates:
                held += 1
                status = mine.get(d, "absent")
                absent += status == "absent"
                late += status == "late"
                excused += status == "excused"
        if held < 10:
            return None
        return (absent / held, late / held, excused / held)

    def profile(student_id):
        if student_id not in profile_of:
            name = rng.choices(profile_names, weights=[PROFILE_WEIGHTS[n] for n in profile_names])[0]
            profile_of[student_id] = observed_profile(student_id) or PROFILES[name]
        return profile_of[student_id]

    def class_profile(student_id, c):
        """Habits nudged per class: early-morning classes see more lates."""
        p_absent, p_late, p_excused = profile(student_id)
        if c["start_time"] < "08:30":
            p_late = min(p_late * 1.3 + 0.02, 0.6)
        return p_absent, p_late, p_excused

    def tap(c, sid):
        """One student's (time_tapped, status) for one session, or None if absent."""
        window = c["attendance_window_minutes"] or 15
        p_absent, p_late, p_excused = class_profile(sid, c)
        roll = rng.random()
        if roll < p_absent:
            return None
        if roll < p_absent + p_excused:
            return minutes_after(c["end_time"], rng.randint(1, 20), rng), "excused"
        if roll < p_absent + p_excused + p_late:
            late_by = min(window + int(rng.expovariate(1 / 10)), class_length(c) - 1)
            return minutes_after(c["start_time"], late_by, rng), "late"
        on_time = min(int(rng.triangular(0, window, 2)), window - 1)
        return minutes_after(c["start_time"], on_time, rng), "present"

    # Profiles are read before any new rows are written below.
    for (sid,) in conn.execute("SELECT DISTINCT student_id FROM enrollments").fetchall():
        profile(sid)

    # 4. Past sessions for classes/dates with no history yet
    added_rows = sessions = 0
    for c in classes:
        enrolled = [r["student_id"] for r in conn.execute("SELECT student_id FROM enrollments WHERE class_id = ?", (c["class_id"],))]
        if not enrolled:
            continue
        existing_dates = {r["date"] for r in conn.execute("SELECT DISTINCT date FROM attendance WHERE class_id = ?", (c["class_id"],))}

        day = SEMESTER_START
        while day < today:
            iso = day.isoformat()
            # Only extend a class's history forward; a past weekday it has no
            # rows for was a cancelled class (or a real day off) — leave it.
            held = (day.weekday() < 5 and day not in HOLIDAYS and iso not in existing_dates
                    and (not existing_dates or iso > max(existing_dates)))
            if held and rng.random() >= CANCELLED_CHANCE:
                rows = []
                for sid in enrolled:
                    result = tap(c, sid)
                    if result:  # None = absent = no row
                        rows.append((sid, *result))
                if not rows:  # a held session always has someone in it
                    rows.append((enrolled[0], minutes_after(c["start_time"], 1, rng), "present"))
                conn.executemany(
                    "INSERT INTO attendance (student_id, class_id, date, time_tapped, status) VALUES (?, ?, ?, ?, ?)",
                    [(sid, c["class_id"], iso, t, s) for sid, t, s in rows],
                )
                added_rows += len(rows)
                sessions += 1
            day += timedelta(days=1)

    # 5. A student enrolled in a class that already has history, but with no
    #    rows of their own in it, would show absent for every past session.
    #    Give them the same kind of record as everyone else in that class.
    backfilled = 0
    for c in classes:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM attendance WHERE class_id = ? AND date < ? ORDER BY date",
            (c["class_id"], today.isoformat()))]
        if not dates:
            continue
        for (sid,) in conn.execute(
            """SELECT e.student_id FROM enrollments e
               WHERE e.class_id = ? AND NOT EXISTS (
                   SELECT 1 FROM attendance a WHERE a.class_id = e.class_id AND a.student_id = e.student_id)""",
            (c["class_id"],),
        ).fetchall():
            rows = [(sid, c["class_id"], d, *r) for d in dates if (r := tap(c, sid))]
            conn.executemany(
                "INSERT INTO attendance (student_id, class_id, date, time_tapped, status) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            backfilled += len(rows)

    conn.commit()
    conn.close()
    print(f"Added {added_students} students, {added_enrollments} enrollments, "
          f"{sessions} new past sessions ({added_rows} attendance rows), "
          f"{backfilled} rows for students new to a class.")


if __name__ == "__main__":
    main()
