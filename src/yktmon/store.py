from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(domain TEXT PRIMARY KEY, cookie TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS courses(
 id INTEGER PRIMARY KEY, domain TEXT NOT NULL, classroom TEXT NOT NULL,
 name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, seen REAL NOT NULL,
 UNIQUE(domain,classroom));
CREATE TABLE IF NOT EXISTS problems(
 id INTEGER PRIMARY KEY, domain TEXT NOT NULL, lesson TEXT NOT NULL,
 problem TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 answer TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
 image TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(domain,lesson,problem));
CREATE INDEX IF NOT EXISTS problems_recent ON problems(created DESC);
CREATE TABLE IF NOT EXISTS deliveries(
 problem INTEGER NOT NULL REFERENCES problems(id), channel TEXT NOT NULL,
 status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', updated REAL NOT NULL, signature TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(problem,channel));
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


class Store:
    """Owned by the event loop. No global path or shared thread connection."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        # Additive migrations preserve existing real course/problem history.
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(courses)")}
        if "deleted" not in columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE courses ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0"
                )
        # Phase separates first-arrival reminders from answer delivery acknowledgements.
        if "phase" not in {
            r[1] for r in self.db.execute("PRAGMA table_info(deliveries)")
        }:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("ALTER TABLE deliveries RENAME TO deliveries_previous")
                self.db.execute(
                    "CREATE TABLE deliveries(problem INTEGER NOT NULL REFERENCES problems(id), channel TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', updated REAL NOT NULL, signature TEXT NOT NULL DEFAULT '', phase TEXT NOT NULL DEFAULT 'result', PRIMARY KEY(problem,channel,phase))"
                )
                self.db.execute(
                    "INSERT INTO deliveries(problem,channel,status,detail,updated,signature) SELECT problem,channel,status,detail,updated,signature FROM deliveries_previous"
                )
                self.db.execute("DROP TABLE deliveries_previous")
        self.migrate_records()
        self.migrate_record_activity()
        self.recover()

    def migrate_records(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(courses)")}
            if "display_name" not in columns:
                self.db.execute(
                    "ALTER TABLE courses ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"
                )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS class_records(id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL REFERENCES courses(id), title TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'paused', listening INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL)"
            )
            if "record_id" not in {
                r[1] for r in self.db.execute("PRAGMA table_info(problems)")
            }:
                self.db.execute(
                    "ALTER TABLE problems ADD COLUMN record_id INTEGER REFERENCES class_records(id)"
                )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS problems_record ON problems(record_id,created)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS record_bindings(domain TEXT NOT NULL, lesson TEXT NOT NULL, record_id INTEGER NOT NULL REFERENCES class_records(id), PRIMARY KEY(domain,lesson))"
            )
        if self.db.execute("SELECT 1 FROM metadata WHERE key='records_v1'").fetchone():
            return
        # Older versions have a reliable remote lesson ID, but no editable local record.
        with self.db:
            groups = self.db.execute(
                "SELECT domain,lesson,MIN(created) AS started FROM problems WHERE record_id IS NULL GROUP BY domain,lesson"
            ).fetchall()
            for group in groups:
                row = self.db.execute(
                    "SELECT payload FROM problems WHERE domain=? AND lesson=? ORDER BY id LIMIT 1",
                    (group["domain"], group["lesson"]),
                ).fetchone()
                name = str(json.loads(row["payload"]).get("course") or "历史课程")
                found = self.db.execute(
                    "SELECT id FROM courses WHERE domain=? AND name=? ORDER BY id LIMIT 1",
                    (group["domain"], name),
                ).fetchone()
                if found:
                    course_id = found["id"]
                else:
                    cur = self.db.execute(
                        "INSERT INTO courses(domain,classroom,name,seen) VALUES(?,?,?,?)",
                        (
                            group["domain"],
                            "legacy:" + group["lesson"],
                            name,
                            group["started"],
                        ),
                    )
                    course_id = cur.lastrowid
                title = (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(group["started"]))
                    + " · 历史课堂"
                )
                cur = self.db.execute(
                    "INSERT INTO class_records(course_id,title,state,listening,created,updated) VALUES(?,?,'archived',0,?,?)",
                    (course_id, title, group["started"], group["started"]),
                )
                self.db.execute(
                    "UPDATE problems SET record_id=? WHERE domain=? AND lesson=?",
                    (cur.lastrowid, group["domain"], group["lesson"]),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO record_bindings VALUES(?,?,?)",
                    (group["domain"], group["lesson"], cur.lastrowid),
                )
            self.db.execute("INSERT INTO metadata VALUES('records_v1','1')")

    def migrate_record_activity(self):
        # Existing paused rows did not distinguish drafts from previously-started records.
        # Use recorded evidence only; do not fabricate a start for an untouched draft.
        with self.db:
            if "started_at" not in {
                r[1] for r in self.db.execute("PRAGMA table_info(class_records)")
            }:
                self.db.execute("ALTER TABLE class_records ADD COLUMN started_at REAL")
                self.db.execute(
                    "UPDATE class_records SET started_at=created WHERE listening=1 OR state IN ('live','waiting','archived') OR EXISTS(SELECT 1 FROM problems p WHERE p.record_id=class_records.id) OR EXISTS(SELECT 1 FROM record_bindings b WHERE b.record_id=class_records.id)"
                )

    def rename_course(self, domain, id, name):
        with self.db:
            return self.db.execute(
                "UPDATE courses SET display_name=? WHERE domain=? AND id=? AND deleted=0",
                (name, domain, id),
            ).rowcount

    def record(self, domain, id):
        row = self.db.execute(
            "SELECT r.*,c.domain,c.name AS source_name,COALESCE(NULLIF(c.display_name,''),c.name) AS course_name,c.deleted AS course_deleted,(SELECT count(*) FROM problems p WHERE p.record_id=r.id) AS problem_count FROM class_records r JOIN courses c ON c.id=r.course_id WHERE r.id=? AND c.domain=?",
            (id, domain),
        ).fetchone()
        return dict(row) if row else None

    def records(self, domain, course_id=None):
        sql = "SELECT r.id FROM class_records r JOIN courses c ON c.id=r.course_id WHERE c.domain=?"
        args = [domain]
        if course_id is not None:
            sql += " AND c.id=?"
            args.append(course_id)
        sql += " ORDER BY r.created DESC,r.id DESC"
        return [
            self.record(domain, row[0]) for row in self.db.execute(sql, args).fetchall()
        ]

    def create_record(self, domain, course_id, title="", listening=False):
        if not self.get_course(domain, course_id):
            raise ValueError("课程不存在")
        if listening and self.wanted_record(domain, course_id):
            raise ValueError("此课程已有正在监听的记录，请先暂停或结束它")
        number = (
            self.db.execute(
                "SELECT count(*) FROM class_records WHERE course_id=?", (course_id,)
            ).fetchone()[0]
            + 1
        )
        now = time.time()
        title = title or f"第 {number} 次课 · " + time.strftime(
            "%m月%d日", time.localtime(now)
        )
        with self.db:
            cur = self.db.execute(
                "INSERT INTO class_records(course_id,title,state,listening,created,updated,started_at) VALUES(?,?,?,?,?,?,?)",
                (
                    course_id,
                    title,
                    "waiting" if listening else "paused",
                    int(listening),
                    now,
                    now,
                    now if listening else None,
                ),
            )
        return self.record(domain, cur.lastrowid)

    def wanted_record(self, domain, course_id):
        row = self.db.execute(
            "SELECT r.id FROM class_records r JOIN courses c ON c.id=r.course_id WHERE c.domain=? AND r.course_id=? AND r.listening=1 ORDER BY r.id LIMIT 1",
            (domain, course_id),
        ).fetchone()
        return self.record(domain, row[0]) if row else None

    def record_state(self, domain, id, state):
        row = self.record(domain, id)
        if not row:
            raise ValueError("课堂记录不存在")
        listening = state in ("waiting", "live")
        if listening:
            if row["course_deleted"]:
                raise ValueError("请先恢复已删除的课程")
            other = self.wanted_record(domain, row["course_id"])
            if other and other["id"] != id:
                raise ValueError("此课程已有正在监听的记录，请先暂停或结束它")
        with self.db:
            self.db.execute(
                "UPDATE class_records SET state=?,listening=?,updated=?,started_at=CASE WHEN ? THEN COALESCE(started_at,?) ELSE started_at END WHERE id=?",
                (state, int(listening), time.time(), int(listening), time.time(), id),
            )
        return self.record(domain, id)

    def rename_record(self, domain, id, title):
        if not self.record(domain, id):
            return False
        with self.db:
            self.db.execute(
                "UPDATE class_records SET title=?,updated=? WHERE id=?",
                (title, time.time(), id),
            )
        return True

    def bind_record(self, domain, lesson, record_id):
        if not self.record(domain, record_id):
            raise ValueError("课堂记录不存在")
        with self.db:
            self.db.execute(
                "INSERT INTO record_bindings VALUES(?,?,?) ON CONFLICT(domain,lesson) DO UPDATE SET record_id=excluded.record_id",
                (domain, lesson, record_id),
            )

    def record_problems(self, domain, record_id, limit=50, offset=0):
        if not self.record(domain, record_id):
            return []
        return [
            self.get(r[0])
            for r in self.db.execute(
                "SELECT id FROM problems WHERE domain=? AND record_id=? ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
                (domain, record_id, limit, offset),
            ).fetchall()
        ]

    def recover(self):
        with self.db:
            self.db.execute(
                "UPDATE problems SET status='pending', error='' WHERE status='processing'"
            )
            self.db.execute(
                "UPDATE deliveries SET status='failed', detail='服务中断；发送结果未知，请检查群消息后再重试' WHERE status='sending'"
            )

    def close(self):
        self.db.close()

    def session(self, domain):
        row = self.db.execute(
            "SELECT cookie FROM sessions WHERE domain=?", (domain,)
        ).fetchone()
        return row[0] if row else ""

    def save_session(self, domain, cookie):
        with self.db:
            self.db.execute(
                "INSERT INTO sessions VALUES (?,?) ON CONFLICT(domain) DO UPDATE SET cookie=excluded.cookie",
                (domain, cookie),
            )

    def course(self, domain, classroom, name):
        classroom = str(classroom or "name:" + name)
        with self.db:
            # Upgrade legacy name-only entries when a stable classroom ID is discovered.
            if not self.db.execute(
                "SELECT 1 FROM courses WHERE domain=? AND classroom=?",
                (domain, classroom),
            ).fetchone():
                self.db.execute(
                    "UPDATE courses SET classroom=? WHERE domain=? AND classroom=?",
                    (classroom, domain, "name:" + name),
                )
            self.db.execute(
                "INSERT INTO courses(domain,classroom,name,seen) VALUES(?,?,?,?) ON CONFLICT(domain,classroom) DO UPDATE SET name=excluded.name,seen=excluded.seen",
                (domain, classroom, name, time.time()),
            )
        return dict(
            self.db.execute(
                "SELECT * FROM courses WHERE domain=? AND classroom=?",
                (domain, classroom),
            ).fetchone()
        )

    def courses(self, domain):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM courses WHERE domain=? AND deleted=0 ORDER BY enabled DESC,seen DESC",
                (domain,),
            )
        ]

    def enable_course(self, domain, id, enabled):
        with self.db:
            return self.db.execute(
                "UPDATE courses SET enabled=? WHERE domain=? AND id=? AND deleted=0",
                (int(enabled), domain, id),
            ).rowcount

    def add_course(self, domain, classroom, name):
        if not classroom:
            matches = self.db.execute(
                "SELECT * FROM courses WHERE domain=? AND name=?", (domain, name)
            ).fetchall()
            if len(matches) == 1:
                classroom = matches[0]["classroom"]
            elif len(matches) > 1:
                raise ValueError("存在多门同名课程，请填写课堂 ID 区分")
        key = classroom or "name:" + name
        current = self.db.execute(
            "SELECT * FROM courses WHERE domain=? AND classroom=?", (domain, key)
        ).fetchone()
        if current and not current["deleted"]:
            raise ValueError("课程已经存在")
        course = self.course(domain, classroom, name)
        with self.db:
            self.db.execute(
                "UPDATE courses SET deleted=0,enabled=1 WHERE id=?", (course["id"],)
            )
        return self.get_course(domain, course["id"])

    def get_course(self, domain, id):
        row = self.db.execute(
            "SELECT * FROM courses WHERE domain=? AND id=? AND deleted=0", (domain, id)
        ).fetchone()
        return dict(row) if row else None

    def delete_course(self, domain, id):
        # Keep a tombstone so discovery cannot immediately recreate a deleted course.
        with self.db:
            return self.db.execute(
                "UPDATE courses SET deleted=1,enabled=0 WHERE domain=? AND id=? AND deleted=0",
                (domain, id),
            ).rowcount

    def insert(self, domain, lesson, problem, record_id=None):
        now = time.time()
        if record_id is None:
            binding = self.db.execute(
                "SELECT record_id FROM record_bindings WHERE domain=? AND lesson=?",
                (domain, lesson),
            ).fetchone()
            record_id = binding[0] if binding else None
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO problems(domain,lesson,problem,payload,created,updated,record_id) VALUES (?,?,?,?,?,?,?)",
                (
                    domain,
                    lesson,
                    problem["problem_id"],
                    json.dumps(problem, ensure_ascii=False),
                    now,
                    now,
                    record_id,
                ),
            )
        row = self.db.execute(
            "SELECT id,status FROM problems WHERE domain=? AND lesson=? AND problem=?",
            (domain, lesson, problem["problem_id"]),
        ).fetchone()
        return row["id"], bool(cur.rowcount)

    def get(self, id):
        r = self.db.execute("SELECT * FROM problems WHERE id=?", (id,)).fetchone()
        if not r:
            return None
        data = dict(r)
        data["payload"] = json.loads(data["payload"])
        data["answer"] = json.loads(data["answer"])
        data["deliveries"] = [
            dict(d)
            for d in self.db.execute(
                "SELECT channel,status,detail,signature,phase FROM deliveries WHERE problem=?",
                (id,),
            )
        ]
        return data

    def recent(self, domain, limit=50, offset=0):
        ids = self.db.execute(
            "SELECT id FROM problems WHERE domain=? ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
            (domain, limit, offset),
        ).fetchall()
        return [self.get(r[0]) for r in ids]

    def pending(self, domain):
        return [
            r[0]
            for r in self.db.execute(
                "SELECT p.id FROM problems p LEFT JOIN class_records r ON r.id=p.record_id WHERE p.domain=? AND p.status='pending' AND (p.record_id IS NULL OR r.listening=1) ORDER BY p.id",
                (domain,),
            )
        ]

    def update(self, id, **values):
        allowed = {"status", "answer", "error", "image", "payload"}
        if not values or not values.keys() <= allowed:
            raise ValueError("Invalid update")
        values = {
            k: json.dumps(v, ensure_ascii=False) if k in ("answer", "payload") else v
            for k, v in values.items()
        }
        values["updated"] = time.time()
        with self.db:
            self.db.execute(
                "UPDATE problems SET "
                + ",".join(k + "=?" for k in values)
                + " WHERE id=?",
                (*values.values(), id),
            )

    def delivery(self, id, channel, status, detail="", signature="", phase="result"):
        with self.db:
            self.db.execute(
                "INSERT INTO deliveries(problem,channel,status,detail,updated,signature,phase) VALUES (?,?,?,?,?,?,?) ON CONFLICT(problem,channel,phase) DO UPDATE SET status=excluded.status,detail=excluded.detail,updated=excluded.updated,signature=excluded.signature",
                (id, channel, status, detail, time.time(), signature, phase),
            )

    def migrate_courses(self, source: Path):
        key = "legacy_courses_v1"
        if self.db.execute("SELECT 1 FROM metadata WHERE key=?", (key,)).fetchone():
            return 0
        old = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
        old.row_factory = sqlite3.Row
        try:
            rows = old.execute("SELECT * FROM courses").fetchall()
        finally:
            old.close()
        for row in rows:
            c = self.course(row["domain"], row["classroom_id"], row["name"])
            self.enable_course(row["domain"], c["id"], bool(row["enabled"]))
        with self.db:
            self.db.execute("INSERT INTO metadata VALUES (?,?)", (key, str(len(rows))))
        return len(rows)
