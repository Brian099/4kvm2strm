# -*- coding: utf-8 -*-
"""SQLite 存储。只存站内 id，不存任何会过期的 URL。"""
import os
import sqlite3
import threading

import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS vod (
    slug       TEXT PRIMARY KEY,   -- /play/<slug> 里的 slug，取流时的 secret
    vod_id     TEXT,               -- 页面上的 data-vod-id
    name       TEXT,
    pic        TEXT,
    cat        TEXT,
    year       TEXT,
    area       TEXT,
    remarks    TEXT,
    ep_count   INTEGER DEFAULT 0,
    detail_at  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS episode (
    slug   TEXT NOT NULL,          -- 所属剧
    ep_id  TEXT NOT NULL,          -- dataid，取流必需
    seq    INTEGER,                -- 第几集
    name   TEXT,
    PRIMARY KEY (slug, ep_id)
);
CREATE INDEX IF NOT EXISTS idx_ep_slug ON episode(slug, seq);
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def conn():
    if not hasattr(_local, "c"):
        os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.c = c
    return _local.c


def init_db():
    c = conn()
    c.executescript(SCHEMA)
    c.commit()


def upsert_vod(v):
    c = conn()
    c.execute(
        "INSERT INTO vod(slug,vod_id,name,pic,cat,year,area,remarks,ep_count,detail_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) "
        # 注意：列表抓取时 ep_count / detail_at 是 0，不能覆盖掉已有的详情状态，
        # 否则每次刷新目录都会把「已补详情」的标记清掉，导致重复抓详情。
        "ON CONFLICT(slug) DO UPDATE SET "
        "vod_id=excluded.vod_id, name=excluded.name, pic=excluded.pic, "
        "cat=excluded.cat, "
        "year=CASE WHEN excluded.year<>'' THEN excluded.year ELSE vod.year END, "
        "area=CASE WHEN excluded.area<>'' THEN excluded.area ELSE vod.area END, "
        "remarks=CASE WHEN excluded.remarks<>'' THEN excluded.remarks ELSE vod.remarks END, "
        "ep_count=CASE WHEN excluded.ep_count>0 THEN excluded.ep_count ELSE vod.ep_count END, "
        "detail_at=CASE WHEN excluded.detail_at>0 THEN excluded.detail_at ELSE vod.detail_at END",
        (v["slug"], v.get("vod_id", ""), v.get("name", ""), v.get("pic", ""),
         v.get("cat", ""), v.get("year", ""), v.get("area", ""), v.get("remarks", ""),
         int(v.get("ep_count", 0)), int(v.get("detail_at", 0))),
    )
    c.commit()


def replace_episodes(slug, eps):
    c = conn()
    c.execute("DELETE FROM episode WHERE slug=?", (slug,))
    c.executemany(
        "INSERT OR REPLACE INTO episode(slug,ep_id,seq,name) VALUES(?,?,?,?)",
        [(slug, e["ep_id"], int(e["seq"]), e.get("name", "")) for e in eps],
    )
    c.commit()


def episodes(slug):
    return [dict(r) for r in conn().execute(
        "SELECT ep_id,seq,name FROM episode WHERE slug=? ORDER BY seq", (slug,)).fetchall()]


def _vod_where(keyword="", cat="", year=""):
    where, args = [], []
    if keyword:
        where.append("name LIKE ?")
        args.append("%" + keyword + "%")
    if cat:
        where.append("COALESCE(NULLIF(cat,''),'未分类') = ?")
        args.append(cat)
    if year:
        where.append("year = ?")
        args.append(str(year))
    return ((" WHERE " + " AND ".join(where)) if where else ""), args


def list_vods(limit=60, offset=0, keyword="", cat="", year=""):
    w, args = _vod_where(keyword, cat, year)
    c = conn()
    rows = c.execute("SELECT * FROM vod" + w + " ORDER BY rowid DESC LIMIT ? OFFSET ?",
                     args + [limit, offset]).fetchall()
    total = c.execute("SELECT COUNT(*) n FROM vod" + w, args).fetchone()["n"]
    return [dict(r) for r in rows], total


def filters():
    """筛选栏数据：分类（带数量）+ 年份（带数量）。"""
    c = conn()
    cats = [{"name": r["c"], "count": r["n"]} for r in c.execute(
        "SELECT COALESCE(NULLIF(cat,''),'未分类') AS c, COUNT(*) AS n "
        "FROM vod GROUP BY c ORDER BY n DESC").fetchall()]
    years = [{"year": r["y"], "count": r["n"]} for r in c.execute(
        "SELECT year AS y, COUNT(*) AS n FROM vod "
        "WHERE year<>'' AND year IS NOT NULL GROUP BY y ORDER BY y DESC").fetchall()]
    return {"cats": cats, "years": years}


def stats():
    c = conn()
    return {
        "vods": c.execute("SELECT COUNT(*) n FROM vod").fetchone()["n"],
        "episodes": c.execute("SELECT COUNT(*) n FROM episode").fetchone()["n"],
        "detailed": c.execute("SELECT COUNT(*) n FROM vod WHERE detail_at>0").fetchone()["n"],
    }


def get_setting(key, default=""):
    r = conn().execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    c = conn()
    c.execute("INSERT INTO setting(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    c.commit()


def set_state(key, value):
    c = conn()
    c.execute("INSERT INTO sync_state(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    c.commit()


def get_state(key, default=""):
    r = conn().execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default
