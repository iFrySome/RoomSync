"""
Dev helper: insert a temporary class that starts a couple of minutes from
now, so you can walk through the full welcome -> teacher_early_confirm ->
teacher_early_wait -> attendance_open -> class_ongoing -> welcome cycle in
the browser without waiting for the real seeded class times.

Usage:
    python seed_test_class.py                # start in 2 min, 1 min window, 4 min class
    python seed_test_class.py --start-in 5 --window 2 --duration 6
    python seed_test_class.py --teacher-id 2 # use Mark Villanueva instead of Fernandez
    python seed_test_class.py --clear        # remove all test classes this script created

All rows this script creates are tagged with the subject name prefix
"TEST:" so --clear can find and remove them safely without touching your
real seeded data.
"""

import argparse
import sqlite3
from datetime import datetime, timedelta

DB_PATH = "roomsync.db"
SUBJECT_PREFIX = "TEST:"


def clear_test_classes(conn):
    rows = conn.execute(
        "SELECT class_id FROM classes WHERE subject_name LIKE ?",
        (f"{SUBJECT_PREFIX}%",),
    ).fetchall()
    class_ids = [r[0] for r in rows]
    for class_id in class_ids:
        conn.execute("DELETE FROM attendance WHERE class_id = ?", (class_id,))
        conn.execute("DELETE FROM enrollments WHERE class_id = ?", (class_id,))
        conn.execute("DELETE FROM classes WHERE class_id = ?", (class_id,))
    conn.commit()
    print(f"Removed {len(class_ids)} test class(es).")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--teacher-id", type=int, default=1, help="teacher_id to schedule (default 1)")
    parser.add_argument("--start-in", type=int, default=2, help="minutes from now the class starts (default 2)")
    parser.add_argument("--duration", type=int, default=4, help="class length in minutes (default 4)")
    parser.add_argument("--window", type=int, default=1, help="attendance_window_minutes (default 1)")
    parser.add_argument("--room", default="Room 301", help="room_number (default 'Room 301')")
    parser.add_argument("--clear", action="store_true", help="remove all test classes created by this script")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.clear:
        clear_test_classes(conn)
        conn.close()
        return

    teacher = conn.execute(
        "SELECT * FROM teachers WHERE teacher_id = ?", (args.teacher_id,)
    ).fetchone()
    if not teacher:
        print(f"No teacher with teacher_id={args.teacher_id}. Run `python database.py` to seed teachers first.")
        conn.close()
        return

    now = datetime.now()
    start = now + timedelta(minutes=args.start_in)
    end = start + timedelta(minutes=args.duration)

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO classes (subject_name, teacher_id, room_number, start_time, end_time, attendance_window_minutes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (f"{SUBJECT_PREFIX} Demo Class", args.teacher_id, args.room, start.strftime("%H:%M"), end.strftime("%H:%M"), args.window),
    )
    class_id = cur.lastrowid
    conn.commit()
    conn.close()

    window_open_at = start - timedelta(minutes=args.window)
    print(f"Created test class_id={class_id} for {teacher['first_name']} {teacher['last_name']}")
    print(f"  Now:              {now.strftime('%H:%M:%S')}")
    print(f"  Confirm window opens at:  {window_open_at.strftime('%H:%M:%S')}")
    print(f"  Class start:      {start.strftime('%H:%M:%S')}")
    print(f"  Class end:        {end.strftime('%H:%M:%S')}")
    print(f"\nTeacher's RFID tag to tap on /kiosk: {teacher['rfid_tag']}")
    print("Run `python seed_test_class.py --clear` afterwards to remove this test class.")


if __name__ == "__main__":
    main()
