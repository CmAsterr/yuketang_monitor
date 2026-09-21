"""Persistent QQ binding, target snapshots and outbox. Uses the app-owned SQLite connection."""

import hashlib
import json
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_groups(id INTEGER PRIMARY KEY, app_id TEXT NOT NULL, openid TEXT NOT NULL, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, permission TEXT NOT NULL DEFAULT '待验证', created REAL NOT NULL, UNIQUE(app_id,openid));
CREATE TABLE IF NOT EXISTS qq_course_groups(course_id INTEGER NOT NULL REFERENCES courses(id), group_id INTEGER NOT NULL REFERENCES qq_groups(id), PRIMARY KEY(course_id,group_id));
CREATE TABLE IF NOT EXISTS qq_targets(problem_id INTEGER NOT NULL REFERENCES problems(id), group_id INTEGER NOT NULL REFERENCES qq_groups(id), app_id TEXT NOT NULL, openid TEXT NOT NULL, PRIMARY KEY(problem_id,group_id));
CREATE TABLE IF NOT EXISTS qq_snapshots(problem_id INTEGER PRIMARY KEY REFERENCES problems(id));
CREATE TABLE IF NOT EXISTS qq_outbox(id INTEGER PRIMARY KEY, dedupe TEXT NOT NULL UNIQUE, problem_id INTEGER REFERENCES problems(id), group_id INTEGER NOT NULL REFERENCES qq_groups(id), app_id TEXT NOT NULL, openid TEXT NOT NULL, phase TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, message_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '', next_at REAL NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS qq_lesson_events(app_id TEXT NOT NULL, domain TEXT NOT NULL, lesson TEXT NOT NULL, phase TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(app_id,domain,lesson,phase));
CREATE INDEX IF NOT EXISTS qq_outbox_pending ON qq_outbox(state,group_id,id);
"""


class QQStore:
    def __init__(self, store):
        self.store = store
        self.db = store.db
        self.db.executescript(SCHEMA)
        with self.db:
            self.db.execute(
                "UPDATE qq_outbox SET state='unknown',error='服务在发送期间退出，结果未知；请核对群消息后手动重试' WHERE state='sending'"
            )

    def groups(self, app_id):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT id,app_id,name,enabled,permission,created FROM qq_groups WHERE app_id=? ORDER BY id",
                (app_id,),
            )
        ]

    def group(self, id, app_id):
        r = self.db.execute(
            "SELECT * FROM qq_groups WHERE id=? AND app_id=?", (id, app_id)
        ).fetchone()
        return dict(r) if r else None

    def bind(self, app_id, openid, name):
        with self.db:
            self.db.execute(
                "INSERT INTO qq_groups(app_id,openid,name,created) VALUES(?,?,?,?) ON CONFLICT(app_id,openid) DO UPDATE SET name=excluded.name,enabled=1,permission='待验证'",
                (app_id, openid, name, time.time()),
            )
        return self.db.execute(
            "SELECT id FROM qq_groups WHERE app_id=? AND openid=?", (app_id, openid)
        ).fetchone()[0]

    def update_group(self, id, app_id, *, name=None, enabled=None):
        if not self.group(id, app_id):
            raise ValueError("群不存在或属于另一机器人")
        with self.db:
            if name is not None:
                self.db.execute("UPDATE qq_groups SET name=? WHERE id=?", (name, id))
            if enabled is not None:
                self.db.execute(
                    "UPDATE qq_groups SET enabled=? WHERE id=?", (int(enabled), id)
                )
                if not enabled:
                    self.db.execute(
                        "UPDATE qq_outbox SET state='cancelled',error='通知群已停用',updated=? WHERE group_id=? AND state='pending'",
                        (time.time(), id),
                    )

    def permission(self, app_id, openid, value):
        with self.db:
            self.db.execute(
                "UPDATE qq_groups SET permission=? WHERE app_id=? AND openid=?",
                (value, app_id, openid),
            )

    def mapping(self, course_id, app_id):
        return [
            r[0]
            for r in self.db.execute(
                "SELECT m.group_id FROM qq_course_groups m JOIN qq_groups g ON g.id=m.group_id WHERE m.course_id=? AND g.app_id=?",
                (course_id, app_id),
            )
        ]

    def set_mapping(self, course_id, app_id, ids):
        if any(not self.group(id, app_id) for id in ids):
            raise ValueError("选择的群不属于当前机器人")
        with self.db:
            self.db.execute(
                "DELETE FROM qq_course_groups WHERE course_id=? AND group_id IN (SELECT id FROM qq_groups WHERE app_id=?)",
                (course_id, app_id),
            )
            self.db.executemany(
                "INSERT INTO qq_course_groups VALUES(?,?)",
                [(course_id, id) for id in set(ids)],
            )

    def snapshot(self, problem_id, course_id, app_id, enabled):
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO qq_snapshots VALUES(?)", (problem_id,)
            )
            if not cur.rowcount:
                return
            if enabled and course_id is not None:
                self.db.execute(
                    "INSERT INTO qq_targets SELECT ?,g.id,g.app_id,g.openid FROM qq_groups g JOIN qq_course_groups m ON m.group_id=g.id WHERE m.course_id=? AND g.app_id=? AND g.enabled=1",
                    (problem_id, course_id, app_id),
                )

    def targets(self, problem_id):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM qq_targets WHERE problem_id=?", (problem_id,)
            )
        ]

    def enqueue(self, target, phase, payload, *, problem_id=None, attempt=None):
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        version = hashlib.sha256(encoded.encode()).hexdigest()
        dedupe = f"{target['app_id']}:{target['group_id']}:{problem_id}:{phase}:{attempt or version}"
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO qq_outbox(dedupe,problem_id,group_id,app_id,openid,phase,payload,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    dedupe,
                    problem_id,
                    target["group_id"],
                    target["app_id"],
                    target["openid"],
                    phase,
                    encoded,
                    now,
                    now,
                ),
            )
        return self.db.execute(
            "SELECT id FROM qq_outbox WHERE dedupe=?", (dedupe,)
        ).fetchone()[0]

    def get(self, id):
        row = self.db.execute("SELECT * FROM qq_outbox WHERE id=?", (id,)).fetchone()
        return dict(row) if row else None

    def pending_groups(self, app_id):
        return [
            r[0]
            for r in self.db.execute(
                "SELECT DISTINCT group_id FROM qq_outbox WHERE app_id=? AND state='pending'",
                (app_id,),
            )
        ]

    def next(self, group_id, app_id):
        # A transient delay blocks only this group. Other groups have independent workers.
        row = self.db.execute(
            "SELECT * FROM qq_outbox WHERE group_id=? AND app_id=? AND state='pending' ORDER BY id LIMIT 1",
            (group_id, app_id),
        ).fetchone()
        return dict(row) if row and row["next_at"] <= time.time() else None

    def update(self, id, state, *, error="", code="", message_id="", next_at=0):
        with self.db:
            self.db.execute(
                "UPDATE qq_outbox SET state=?,error=?,error_code=?,message_id=?,next_at=?,updated=? WHERE id=?",
                (state, error, str(code), message_id, next_at, time.time(), id),
            )

    def claim(self, id):
        with self.db:
            return bool(
                self.db.execute(
                    "UPDATE qq_outbox SET state='sending',attempts=attempts+1,updated=? WHERE id=? AND state='pending'",
                    (time.time(), id),
                ).rowcount
            )

    def cancel_app(self, app_id):
        with self.db:
            self.db.execute(
                "UPDATE qq_outbox SET state='cancelled',error='已切换机器人，旧目标任务取消',updated=? WHERE app_id=? AND state='pending'",
                (time.time(), app_id),
            )

    def history(self, app_id, limit=100, problem_id=None):
        sql = "SELECT o.id,o.problem_id,o.phase,o.state,o.attempts,o.message_id,o.error,o.error_code,o.created,o.updated,g.name AS group_name FROM qq_outbox o JOIN qq_groups g ON g.id=o.group_id WHERE o.app_id=?"
        args = [app_id]
        if problem_id is not None:
            sql += " AND o.problem_id=?"
            args.append(problem_id)
        sql += " ORDER BY o.id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args)]
