import io
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import server


class StudyTrackerStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_file = server.DB_FILE
        server.DB_FILE = Path(self.temp_dir.name) / "state.sqlite3"

    def tearDown(self):
        server.DB_FILE = self.original_db_file
        self.temp_dir.cleanup()

    def create_user(self, phone="123456", name="Student"):
        server.read_state()
        with sqlite3.connect(server.DB_FILE) as db:
            server.init_db(db)
            db.execute(
                "INSERT INTO users VALUES(?,?,?,?,?)",
                (phone, name, "Class 1", "hash", server.now_iso()),
            )
            db.execute("INSERT INTO students VALUES(?,?,?)", (name, phone, "Class 1"))
            db.commit()

    def test_fresh_database_is_not_erased_by_first_full_read(self):
        self.create_user()

        state = server.read_state()

        self.assertIn("123456", state["users"])
        self.assertIn("Student", state["students"])

    def put_state(self, payload):
        body = json.dumps(payload).encode()
        responses = []
        handler_type = type(
            "StateHandler",
            (server.CheckinHandler,),
            {"send_json": lambda _, value, status=200: responses.append((status, value))},
        )
        handler = handler_type.__new__(handler_type)
        handler.path = "/api/state"
        handler.headers = {
            "Authorization": "Bearer test-token",
            "Content-Length": str(len(body)),
        }
        handler.rfile = io.BytesIO(body)

        handler.do_PUT()

        return len(body), responses

    def test_state_route_accepts_compact_payload(self):
        self.create_user()
        server.save_session("test-token", "student", "123456")

        compact_length, compact_responses = self.put_state({"progress": "compact"})

        self.assertLess(compact_length, server.MAX_JSON_BODY_BYTES)
        self.assertEqual(compact_responses[0][0], 200)
        week_key = server.current_week_key()
        self.assertEqual(
            server.read_state()["students"]["Student"]["weeks"][week_key]["progress"],
            "compact",
        )

    def test_state_route_requires_progress_field(self):
        self.create_user()
        server.save_session("test-token", "student", "123456")
        week_key = server.current_week_key()
        full_state_payload = {
            "students": {
                "Student": {
                    "weeks": {week_key: {"progress": "ignored"}}
                }
            }
        }
        _, responses = self.put_state(full_state_payload)

        self.assertEqual(responses[0][0], 400)
        self.assertNotIn(
            week_key,
            server.read_state()["students"]["Student"]["weeks"],
        )

    def test_all_weeks_and_submitted_content_are_exported(self):
        self.create_user()
        state = server.read_state()
        state["students"]["Student"]["weeks"] = {
            "2026-07-20": {
                "progress": "",
                "runningStart": None,
                "timeRecords": [
                    {
                        "id": "old-record",
                        "start": 1784505600000,
                        "end": 1784509200000,
                        "manual": False,
                        "summary": "old week work",
                    }
                ],
            },
            "2026-07-27": {
                "progress": "",
                "runningStart": None,
                "timeRecords": [
                    {
                        "id": "new-record",
                        "start": 1785110400000,
                        "end": 1785114000000,
                        "manual": False,
                        "summary": "new week work",
                    }
                ],
            },
        }
        server.write_state(state)

        loaded = server.read_state()
        records = server.export_time_records(loaded)

        self.assertEqual(
            set(loaded["students"]["Student"]["weeks"]),
            {"2026-07-20", "2026-07-27"},
        )
        self.assertEqual(
            {record["summary"] for record in records},
            {"old week work", "new week work"},
        )

    def test_cross_week_timer_is_split_and_counted_in_each_week(self):
        self.create_user()
        now = datetime.now(server.SHANGHAI_TZ)
        current_week_start = server.week_start_ms(now)
        previous_week_key = server.current_week_key(now - timedelta(days=7))
        start_ms = current_week_start - 60_000
        with sqlite3.connect(server.DB_FILE) as db:
            server.init_db(db)
            db.execute(
                "INSERT INTO weeks VALUES(?,?,?,?)",
                ("Student", previous_week_key, "", start_ms),
            )
            db.commit()

        result = server.toggle_timer("123456", "cross-week work")
        state = server.read_state()
        weeks = state["students"]["Student"]["weeks"]

        self.assertEqual(result["action"], "stopped")
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(result["records"][0]["end"], current_week_start)
        self.assertEqual(
            set(weeks),
            {previous_week_key, server.current_week_key(now)},
        )
        self.assertTrue(
            all(record["summary"] == "cross-week work" for record in result["records"])
        )

    def test_overview_counts_only_current_week_part_of_active_timer(self):
        self.create_user()
        now = datetime.now(server.SHANGHAI_TZ)
        current_week_start = server.week_start_ms(now)
        previous_week_key = server.current_week_key(now - timedelta(days=7))
        with sqlite3.connect(server.DB_FILE) as db:
            server.init_db(db)
            db.execute(
                "INSERT INTO weeks VALUES(?,?,?,?)",
                ("Student", previous_week_key, "", current_week_start - 60_000),
            )
            db.commit()

        before = int(time.time() * 1000)
        row = server.read_overview()[0]
        after = int(time.time() * 1000)

        self.assertTrue(row["isStudying"])
        self.assertGreaterEqual(row["weekMs"], before - current_week_start)
        self.assertLessEqual(row["weekMs"], after - current_week_start)
        self.assertGreaterEqual(row["totalMs"] - row["weekMs"], 60_000)


if __name__ == "__main__":
    unittest.main()
