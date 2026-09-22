"""
Database setup for RoomSync.

Run this file directly (`python database.py`) to (re)create roomsync.db
with the schema and a handful of sample rows. Safe to re-run: it drops
and recreates all tables each time.
"""

import sqlite3
from datetime import date

from werkzeug.security import generate_password_hash

DB_PATH = "roomsync.db"


SCHEMA = """
-- username/password_hash are login credentials the admin sets when
-- creating or editing a student (not something the student signs up for
-- themselves) — same pattern as teachers below. Both nullable, since a
-- student created before this feature (or never given portal access)
-- simply can't log in until an admin sets them. phone_number is likewise
-- nullable and admin-set — not required, only present for a student the
-- admin has given a number for — and is where SMS notifications are sent
-- (see send_sms_notification() in app.py); a student with no number set
-- just never gets an SMS, the same way one with no push subscription
-- never gets a push.
CREATE TABLE students (
    student_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    rfid_tag      TEXT UNIQUE NOT NULL,
    first_name    TEXT NOT NULL,
    last_name     TEXT NOT NULL,
    section       TEXT NOT NULL,
    username      TEXT UNIQUE,
    password_hash TEXT,
    phone_number  TEXT
);

-- username/password_hash are login credentials the admin sets when
-- creating a teacher (not something the teacher signs up for themselves).
-- phone_number is the same nullable, admin-set, SMS-notification-target
-- column as students.phone_number above.
CREATE TABLE teachers (
    teacher_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    rfid_tag      TEXT UNIQUE NOT NULL,
    first_name    TEXT NOT NULL,
    last_name     TEXT NOT NULL,
    username      TEXT UNIQUE,
    password_hash TEXT,
    phone_number  TEXT
);

CREATE TABLE admins (
    admin_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);

-- Just a catalog of room names. Added via "Add a Room" on /admin — there's
-- no separate "Assign Room" step; a room is put to use by picking it from
-- the room dropdown in Assign Class (see classes.room_number below, which
-- stores a plain copy of room_name rather than a foreign key, matching how
-- subject_name/teacher names are also stored as plain text elsewhere).
CREATE TABLE rooms (
    room_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    room_name TEXT UNIQUE NOT NULL
);

-- A class has no day-of-week concept: once room_number/start_time/end_time
-- are set, it's assumed to meet every single day at that time. Adding a
-- class ("Add a Class") only sets subject_name — teacher_id/room_number/
-- start_time/end_time all start out NULL, meaning the class isn't
-- assigned to anyone or tappable yet, until a separate "Assign Class" step
-- (see admin_assign_class() in app.py) sets teacher_id and the schedule
-- together. attendance_window_minutes is the "how early is too early"
-- cutoff before start_time for a teacher's tap, and also the length (in
-- minutes) of the attendance countdown once the window opens.
CREATE TABLE classes (
    class_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_name  TEXT NOT NULL,
    teacher_id    INTEGER,  -- NULL until assigned to a teacher
    room_number   TEXT,
    start_time    TEXT,  -- HH:MM, 24-hour; NULL until set
    end_time      TEXT,  -- HH:MM, 24-hour; NULL until set
    attendance_window_minutes INTEGER NOT NULL DEFAULT 15,
    FOREIGN KEY (teacher_id) REFERENCES teachers (teacher_id)
);

CREATE TABLE attendance (
    attendance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id    INTEGER NOT NULL,
    class_id      INTEGER NOT NULL,
    date          TEXT NOT NULL,  -- YYYY-MM-DD
    time_tapped   TEXT NOT NULL,  -- HH:MM:SS
    status        TEXT NOT NULL CHECK (status IN ('present', 'late', 'absent', 'excused')),
    FOREIGN KEY (student_id) REFERENCES students (student_id),
    FOREIGN KEY (class_id) REFERENCES classes (class_id)
);

-- A student has no classes at all until an admin enrolls them in one
-- (see /admin/students/<id>). A class's attendance-taking (the live
-- present/absent boxes, present count, and perfect-attendance check) and
-- its rosters (session summary, /summary, teacher dashboard) are scoped
-- to only the students enrolled here — not every student in the school.
-- A student can be enrolled in several classes at once.
CREATE TABLE enrollments (
    enrollment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id    INTEGER NOT NULL,
    class_id      INTEGER NOT NULL,
    FOREIGN KEY (student_id) REFERENCES students (student_id),
    FOREIGN KEY (class_id) REFERENCES classes (class_id),
    UNIQUE (student_id, class_id)
);

-- One row per device/browser a teacher or student has granted push
-- notification permission on and registered with /api/push_subscribe —
-- user_type + user_id together identify who it belongs to (there's no FK
-- to either teachers or students specifically, since user_type picks
-- which table user_id refers to). A user can have several rows (e.g. a
-- laptop and a phone both subscribed). endpoint is unique per
-- subscription (effectively per device+browser install), so re-subscribing
-- the same one is a harmless no-op rather than a duplicate row. See
-- send_push_notification() in app.py for how a row gets used, and cleaned
-- up automatically once the browser reports it as expired/unsubscribed.
CREATE TABLE push_subscriptions (
    subscription_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_type       TEXT NOT NULL CHECK (user_type IN ('teacher', 'student')),
    user_id         INTEGER NOT NULL,
    endpoint        TEXT UNIQUE NOT NULL,
    p256dh_key      TEXT NOT NULL,
    auth_key        TEXT NOT NULL
);
"""


def create_and_seed():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.executescript(
        """
        DROP TABLE IF EXISTS push_subscriptions;
        DROP TABLE IF EXISTS enrollments;
        DROP TABLE IF EXISTS attendance;
        DROP TABLE IF EXISTS classes;
        DROP TABLE IF EXISTS rooms;
        DROP TABLE IF EXISTS students;
        DROP TABLE IF EXISTS teachers;
        DROP TABLE IF EXISTS admins;
        """
    )
    cur.executescript(SCHEMA)

    # --- Sample students ---
    # Sample login credentials (username / plaintext password) — same
    # pattern as teachers below: real passwords are never stored, only
    # the hash goes into the database.
    # phone_number is only set for one sample student (Juan Dela Cruz) to
    # demonstrate SMS notifications end-to-end — the rest are left NULL to
    # demonstrate it's optional, same as username/password_hash.
    students = [
        ("04A1B2C3", "Juan", "Dela Cruz", "BSIT-3A", "jdelacruz", "student123", "09763977125"),
        ("04D4E5F6", "Maria", "Santos", "BSIT-3A", "msantos", "student123", None),
        ("04A7B8C9", "Pedro", "Reyes", "BSIT-3A", "preyes", "student123", None),
        ("04D0E1F2", "Ana", "Garcia", "BSIT-3B", "agarcia", "student123", None),
        ("04A3B4C5", "Jose", "Ramirez", "BSIT-3B", "jramirez", "student123", None),
    ]
    cur.executemany(
        """
        INSERT INTO students (rfid_tag, first_name, last_name, section, username, password_hash, phone_number)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (rfid, first, last, section, username, generate_password_hash(password), phone_number)
            for rfid, first, last, section, username, password, phone_number in students
        ],
    )

    # --- Sample teachers ---
    # Sample login credentials (username / plaintext password) — real
    # passwords are never stored; only the hash goes into the database.
    # Same phone_number pattern as students: only Liza Fernandez has one
    # set, for testing SMS notifications; the rest are left NULL.
    teachers = [
        ("T-1001", "Liza", "Fernandez", "lfernandez", "teach123", "09763977125"),
        ("T-1002", "Mark", "Villanueva", "mvillanueva", "teach123", None),
        ("T-1003", "Carla", "Mendoza", "cmendoza", "teach123", None),
    ]
    cur.executemany(
        """
        INSERT INTO teachers (rfid_tag, first_name, last_name, username, password_hash, phone_number)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (rfid, first, last, username, generate_password_hash(password), phone_number)
            for rfid, first, last, username, password, phone_number in teachers
        ],
    )

    # --- Sample admin account (username: admin / password: admin123) ---
    cur.execute(
        "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
        ("admin", generate_password_hash("admin123")),
    )

    # --- Sample rooms ---
    cur.executemany(
        "INSERT INTO rooms (room_name) VALUES (?)",
        [("Room 301",), ("Room 302",), ("Room 401",)],
    )

    # --- Sample classes (teacher_id 1=Fernandez, 2=Villanueva, 3=Mendoza) ---
    # Each one already has a room/time set, so each meets every day at that
    # time (there's no separate day-of-week schedule to configure).
    classes = [
        ("Data Structures and Algorithms", 1, "Room 301", "08:00", "09:30", 15),
        ("Web Development", 2, "Room 301", "09:30", "11:00", 15),
        ("Database Management Systems", 3, "Room 301", "13:00", "14:30", 10),
    ]
    cur.executemany(
        """
        INSERT INTO classes (subject_name, teacher_id, room_number, start_time, end_time, attendance_window_minutes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        classes,
    )

    # A class fresh out of "Add a Class" — no teacher or schedule yet,
    # demonstrating the state "Assign Class" is meant to fill in.
    cur.execute("INSERT INTO classes (subject_name) VALUES (?)", ("Software Engineering",))

    # --- Sample enrollments (student_id, class_id) ---
    # 1=Juan, 2=Maria, 3=Pedro, 4=Ana, 5=Jose; 1=DSA, 2=Web Dev, 3=DBMS.
    enrollments = [
        (1, 1), (2, 1), (3, 1),
        (1, 2), (4, 2), (5, 2),
        (2, 3), (3, 3), (4, 3),
    ]
    cur.executemany(
        "INSERT INTO enrollments (student_id, class_id) VALUES (?, ?)",
        enrollments,
    )

    # --- Sample attendance records (for today) ---
    today = date.today().isoformat()
    attendance_rows = [
        (1, 1, today, "08:03:12", "present"),
        (2, 1, today, "08:04:45", "present"),
        (3, 1, today, "08:16:02", "late"),
        (1, 2, today, "09:31:10", "present"),
        (4, 2, today, "09:33:00", "present"),
    ]
    cur.executemany(
        "INSERT INTO attendance (student_id, class_id, date, time_tapped, status) VALUES (?, ?, ?, ?, ?)",
        attendance_rows,
    )

    conn.commit()
    conn.close()
    print(f"roomsync.db created and seeded at: {DB_PATH}")
    print("Sample admin login: admin / admin123")
    print("Sample teacher logins: lfernandez / teach123, mvillanueva / teach123, cmendoza / teach123")
    print(
        "Sample student logins: jdelacruz / student123, msantos / student123, "
        "preyes / student123, agarcia / student123, jramirez / student123"
    )


if __name__ == "__main__":
    create_and_seed()
