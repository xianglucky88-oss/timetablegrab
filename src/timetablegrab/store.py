"""单一职责：本地 sqlite3 持久化。

保留遗留 records 表的全部既有接口（向后兼容）；新增时刻表域表：
feed_versions / activation_journal / version_tables / version_diffs /
validation_reports / alignment_reports / raw_archives。
所有写操作在一个事务内由调用方 commit，保证激活切换的原子性。
"""
from __future__ import annotations
import json
import sqlite3

from . import config

# 向后兼容：历史调用方可能引用 store.DB_PATH
DB_PATH = config.DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feed_versions(
  feed_source TEXT NOT NULL,
  version_token TEXT NOT NULL,
  effective_date TEXT NOT NULL,
  published_at TEXT NOT NULL,
  state TEXT NOT NULL,
  manifest TEXT NOT NULL,
  report TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(feed_source, version_token)
);
CREATE TABLE IF NOT EXISTS activation_journal(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  feed_source TEXT NOT NULL,
  version_token TEXT NOT NULL,
  phase TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS version_tables(
  feed_source TEXT NOT NULL,
  version_token TEXT NOT NULL,
  table_name TEXT NOT NULL,
  rows TEXT NOT NULL,
  PRIMARY KEY(feed_source, version_token, table_name)
);
CREATE TABLE IF NOT EXISTS version_diffs(
  feed_source TEXT NOT NULL,
  base_version TEXT NOT NULL,
  target_version TEXT NOT NULL,
  report TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(feed_source, base_version, target_version)
);
CREATE TABLE IF NOT EXISTS validation_reports(
  feed_source TEXT NOT NULL,
  version_token TEXT NOT NULL,
  report TEXT NOT NULL,
  PRIMARY KEY(feed_source, version_token)
);
CREATE TABLE IF NOT EXISTS alignment_reports(
  feed_source TEXT NOT NULL,
  old_version TEXT NOT NULL,
  new_version TEXT NOT NULL,
  report TEXT NOT NULL,
  PRIMARY KEY(feed_source, old_version, new_version)
);
CREATE TABLE IF NOT EXISTS raw_archives(
  feed_source TEXT NOT NULL,
  version_token TEXT NOT NULL,
  shard_name TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  content BLOB NOT NULL,
  PRIMARY KEY(feed_source, version_token, shard_name)
);
"""


def connect():
    # 动态读取，使测试可把 DB_PATH 重定向到临时库而不污染真实数据库
    con = sqlite3.connect(config.DB_PATH)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(_SCHEMA)
    return con


# ---- 遗留接口（旧合法输入与旧测试保持可用）----
def upsert(con, record):
    con.execute("INSERT INTO records(key,data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (record.key, json.dumps(record.data, ensure_ascii=False)))


def commit(con):
    con.commit()


def count(con):
    return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]


# ---- 原始文件与来源版本归档 ----
def archive_raw(con, source, token, shard_name, digest, content: bytes):
    con.execute(
        "INSERT OR REPLACE INTO raw_archives VALUES(?,?,?,?,?)",
        (source, token, shard_name, digest, sqlite3.Binary(content)))


def raw_archive(con, source, token):
    return con.execute(
        "SELECT shard_name, sha256, content FROM raw_archives "
        "WHERE feed_source=? AND version_token=? ORDER BY shard_name",
        (source, token)).fetchall()


# ---- 版本登记与状态机 ----
def upsert_feed_version(con, source, token, effective_date, published_at,
                        state, manifest: dict, report: dict | None = None):
    con.execute(
        "INSERT INTO feed_versions VALUES(?,?,?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(feed_source, version_token) DO UPDATE SET "
        "effective_date=excluded.effective_date, state=excluded.state, "
        "manifest=excluded.manifest, report=excluded.report",
        (source, token, effective_date, published_at, state,
         json.dumps(manifest, ensure_ascii=False),
         json.dumps(report, ensure_ascii=False) if report else None))


def get_feed_version(con, source, token):
    row = con.execute(
        "SELECT feed_source,version_token,effective_date,published_at,state,"
        "manifest,report FROM feed_versions WHERE feed_source=? AND version_token=?",
        (source, token)).fetchone()
    if not row:
        return None
    return {"feed_source": row[0], "version_token": row[1], "effective_date": row[2],
            "published_at": row[3], "state": row[4],
            "manifest": json.loads(row[5]),
            "report": json.loads(row[6]) if row[6] else None}


def list_versions(con, source):
    return [dict(zip(("feed_source", "version_token", "effective_date",
                      "published_at", "state"), r))
            for r in con.execute(
                "SELECT feed_source,version_token,effective_date,published_at,state "
                "FROM feed_versions WHERE feed_source=? ORDER BY published_at",
                (source,))]


def set_state(con, source, token, state):
    con.execute(
        "UPDATE feed_versions SET state=? WHERE feed_source=? AND version_token=?",
        (state, source, token))


def latest_version_token(con, source):
    """最近登记的可用版本（active/staged/superseded），作为差分对齐基线。

    rejected/canceled/withdrawn 不参与基线，避免把坏版本当作对照。
    """
    row = con.execute(
        "SELECT version_token FROM feed_versions WHERE feed_source=? "
        "AND state IN ('active','staged','superseded') "
        "ORDER BY published_at DESC, version_token DESC LIMIT 1",
        (source,)).fetchone()
    return row[0] if row else None


def active_version(con, source, on_date: str | None = None):
    """当前生效版本。

    on_date 给定时做“时点查询”：返回该日期当天应生效的版本——
    取 effective_date<=on_date 中最晚者（含已 superseded 的历史版本），
    避免把未来生效的当前 active 版本错误拼接到历史日期查询里。
    """
    if on_date is None:
        row = con.execute(
            "SELECT version_token FROM feed_versions "
            "WHERE feed_source=? AND state='active' "
            "ORDER BY effective_date DESC, published_at DESC LIMIT 1",
            (source,)).fetchone()
        return row[0] if row else None
    row = con.execute(
        "SELECT version_token FROM feed_versions WHERE feed_source=? "
        "AND state IN ('active','superseded') AND effective_date<=? "
        "ORDER BY effective_date DESC, published_at DESC LIMIT 1",
        (source, on_date)).fetchone()
    return row[0] if row else None


def staged_for_date(con, source, effective_date):
    row = con.execute(
        "SELECT version_token FROM feed_versions WHERE feed_source=? "
        "AND effective_date=? AND state='staged'", (source, effective_date)).fetchone()
    return row[0] if row else None


def write_journal(con, source, token, phase, detail: dict | None = None):
    con.execute(
        "INSERT INTO activation_journal(feed_source,version_token,phase,detail) "
        "VALUES(?,?,?,?)",
        (source, token, phase,
         json.dumps(detail, ensure_ascii=False) if detail else None))


def last_journal(con, source):
    row = con.execute(
        "SELECT id,version_token,phase,detail FROM activation_journal "
        "WHERE feed_source=? ORDER BY id DESC LIMIT 1", (source,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "version_token": row[1], "phase": row[2],
            "detail": json.loads(row[3]) if row[3] else None}


def journal_tail(con, source, limit=20):
    rows = con.execute(
        "SELECT version_token,phase,detail,created_at FROM activation_journal "
        "WHERE feed_source=? ORDER BY id DESC LIMIT ?", (source, limit)).fetchall()
    return [{"version_token": r[0], "phase": r[1],
             "detail": json.loads(r[2]) if r[2] else None, "at": r[3]}
            for r in reversed(rows)]


# ---- 表数据 / 报告 / 差分 ----
def replace_version_rows(con, source, token, table_name, rows: list[dict]):
    con.execute(
        "INSERT OR REPLACE INTO version_tables VALUES(?,?,?,?)",
        (source, token, table_name, json.dumps(rows, ensure_ascii=False)))


def version_rows(con, source, token, table_name):
    row = con.execute(
        "SELECT rows FROM version_tables WHERE feed_source=? AND version_token=? "
        "AND table_name=?", (source, token, table_name)).fetchone()
    return json.loads(row[0]) if row else None


def save_diff(con, source, base, target, report: dict):
    con.execute("INSERT OR REPLACE INTO version_diffs VALUES(?,?,?,?,datetime('now'))",
                (source, base, target, json.dumps(report, ensure_ascii=False)))


def get_diff(con, source, base, target):
    row = con.execute(
        "SELECT report FROM version_diffs WHERE feed_source=? AND base_version=? "
        "AND target_version=?", (source, base, target)).fetchone()
    return json.loads(row[0]) if row else None


def all_diffs(con, source):
    return [json.loads(r[0]) for r in con.execute(
        "SELECT report FROM version_diffs WHERE feed_source=? "
        "ORDER BY created_at", (source,))]


def save_validation_report(con, source, token, report: dict):
    con.execute("INSERT OR REPLACE INTO validation_reports VALUES(?,?,?)",
                (source, token, json.dumps(report, ensure_ascii=False)))


def get_validation_report(con, source, token):
    row = con.execute(
        "SELECT report FROM validation_reports WHERE feed_source=? AND version_token=?",
        (source, token)).fetchone()
    return json.loads(row[0]) if row else None


def save_alignment_report(con, source, old, new, report: dict):
    con.execute("INSERT OR REPLACE INTO alignment_reports VALUES(?,?,?,?)",
                (source, old, new, json.dumps(report, ensure_ascii=False)))


def get_alignment_report(con, source, old, new):
    row = con.execute(
        "SELECT report FROM alignment_reports WHERE feed_source=? AND old_version=? "
        "AND new_version=?", (source, old, new)).fetchone()
    return json.loads(row[0]) if row else None
