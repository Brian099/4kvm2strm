# -*- coding: utf-8 -*-
"""苹果CMS V10 兼容接口（对照金牌系统的 maccms.py）。

把本地库以标准「资源站接口」暴露，TVBox 等客户端可直接订阅：

    GET|POST /api.php/provide/vod
        ac=list        -> 分类 + 数量
        ac=videolist   -> 视频列表（含播放地址）
        ac=detail      -> 同 videolist
        参数: t=分类, pg=页码, limit=每页(默认20,上限500), ids=1,2, id=单个, wd=搜索
        at=xml         -> 输出 XML
    GET /pic?url=<图片地址>  或  /pic/<base64url>   图片代理

鉴权：设置接口密码后需带 &key=<密码>。
"""
import base64
import io
import re

import requests
from flask import Blueprint, Response, request

import config
import db
import settings

bp = Blueprint("maccms", __name__)

_MAX_LIMIT = 500


def _text(v):
    return str("" if v is None else v).strip()


def _int(v, d=0):
    try:
        return int(str(v).strip())
    except Exception:
        return d


def _xml_escape(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _image_mime(blob, upstream=None):
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    up = (upstream or "").lower()
    if up.startswith("image/"):
        return upstream
    return "image/jpeg"


def _key_ok():
    need = settings.api_key()
    if not need:
        return True
    got = _text(request.values.get("key") or request.headers.get("X-Api-Key") or "")
    return got == need


# ---------------------------------------------------------------- 分类
def _classes():
    rows = db.conn().execute(
        "SELECT COALESCE(NULLIF(cat,''),'未分类') AS nm, COUNT(*) AS n "
        "FROM vod GROUP BY nm ORDER BY n DESC").fetchall()
    return [{
        "type_id": _text(i + 1),
        "type_name": r["nm"],
        "type_en": r["nm"],
        "type_pid": "0",
        "type_count": str(r["n"]),
        "_name": r["nm"],
    } for i, r in enumerate(rows)]


# ---------------------------------------------------------------- 视频
def _episodes_for(slugs):
    if not slugs:
        return {}
    marks = ",".join("?" * len(slugs))
    out = {}
    for r in db.conn().execute(
            "SELECT slug,ep_id,seq FROM episode WHERE slug IN (%s) ORDER BY slug, seq" % marks,
            list(slugs)).fetchall():
        out.setdefault(r["slug"], []).append({"ep_id": r["ep_id"], "seq": r["seq"]})
    return out


def _play_url(slug, eps):
    base = settings.public_base()
    parts = []
    for i, e in enumerate(eps):
        seq = _int(e.get("seq"), 0) or (i + 1)
        parts.append("第%d集$%s/v/%s/%s.m3u8" % (seq, base, slug, e["ep_id"]))
    return "#".join(parts)


def _vod_item(v, eps):
    return {
        "vod_id": _text(v["slug"]),
        "type_id": _text(v.get("_tid", "")),
        "type_name": _text(v.get("cat")),
        "vod_name": _text(v.get("name")),
        "vod_en": "",
        "vod_pic": _text(v.get("pic")),
        "vod_remarks": _text(v.get("remarks")),
        "vod_time": "",
        "vod_actor": "",
        "vod_director": "",
        "vod_content": "",
        "vod_year": _text(v.get("year")),
        "vod_area": _text(v.get("area")),
        "vod_lang": "",
        "vod_play_from": config.API_PLAY_FROM or "4kvm",
        "vod_play_url": _play_url(v["slug"], eps),
        "vod_douban_id": "",
        "vod_douban_score": "",
        "vod_score": "",
        "vod_score_num": "",
    }


def _query(cat_name, pg, limit, ids, wd, one_id):
    where, args = [], []
    if ids:
        where.append("slug IN (%s)" % ",".join("?" * len(ids)))
        args += [str(x) for x in ids]
    elif one_id:
        where.append("slug = ?")
        args.append(str(one_id))
    if cat_name:
        where.append("COALESCE(NULLIF(cat,''),'未分类') = ?")
        args.append(cat_name)
    if wd:
        where.append("name LIKE ?")
        args.append("%" + wd + "%")
    w = (" WHERE " + " AND ".join(where)) if where else ""

    c = db.conn()
    total = c.execute("SELECT COUNT(*) n FROM vod" + w, args).fetchone()["n"]
    if ids or one_id:
        rows = c.execute("SELECT * FROM vod" + w + " ORDER BY rowid DESC", args).fetchall()
    else:
        rows = c.execute("SELECT * FROM vod" + w + " ORDER BY rowid DESC LIMIT ? OFFSET ?",
                         args + [limit, (pg - 1) * limit]).fetchall()
    return total, [dict(r) for r in rows]


# ---------------------------------------------------------------- XML
def _xml_list(classes):
    b = io.StringIO()
    b.write('<?xml version="1.0" encoding="utf-8"?>\n<rss version="2.0">')
    b.write('<list page="1" pagecount="1" pagesize="20" recordcount="0"></list><class>')
    for c in classes:
        b.write('<ty id="%s">%s</ty>' % (_xml_escape(c["type_id"]), _xml_escape(c["type_name"])))
    b.write("</class></rss>")
    return b.getvalue()


def _xml_vods(items, pg, page_count, total):
    b = io.StringIO()
    b.write('<?xml version="1.0" encoding="utf-8"?>\n<rss version="2.0">')
    b.write('<list page="%d" pagecount="%d" pagesize="%d" recordcount="%d">'
            % (pg, page_count, len(items), total))
    for it in items:
        b.write("<video>")
        for k in ("vod_id", "type_id", "type_name", "vod_name", "vod_en", "vod_pic",
                  "vod_remarks", "vod_time", "vod_actor", "vod_director", "vod_content",
                  "vod_year", "vod_area", "vod_lang", "vod_play_from", "vod_play_url"):
            b.write("<%s>%s</%s>" % (k, _xml_escape(it.get(k)), k))
        b.write("</video>")
    b.write("</list><class></class></rss>")
    return b.getvalue()


# ---------------------------------------------------------------- 路由
@bp.route("/api.php/provide/vod", methods=["GET", "POST"])
@bp.route("/api.php/provide/vod/", methods=["GET", "POST"])
def provide():
    if not _key_ok():
        return {"code": 0, "msg": "密钥错误", "page": 1, "pagecount": 1,
                "limit": "20", "total": "0", "list": [], "class": []}

    ac = _text(request.values.get("ac")) or "list"
    want_xml = _text(request.values.get("at")).lower() == "xml"
    classes = _classes()

    if ac == "list":
        if want_xml:
            return Response(_xml_list(classes), mimetype="text/xml")
        pub = [{k: v for k, v in c.items() if not k.startswith("_")} for c in classes]
        return {"code": 1, "msg": "数据列表", "page": 1, "pagecount": 1,
                "limit": "20", "total": "0", "list": [], "class": pub}

    pg = max(1, _int(request.values.get("pg"), 1))
    limit = _int(request.values.get("limit"), 20)
    if limit < 1:
        limit = 20
    if limit > _MAX_LIMIT:
        limit = _MAX_LIMIT

    t = _text(request.values.get("t"))
    cat_name = ""
    if t:
        idx = _int(t, 0)
        if 1 <= idx <= len(classes):
            cat_name = classes[idx - 1]["_name"]
        else:
            cat_name = t

    wd = _text(request.values.get("wd"))
    one_id = _text(request.values.get("id"))
    ids = []
    raw_ids = _text(request.values.get("ids"))
    if raw_ids:
        parts = [x.strip() for x in raw_ids.split(",") if x.strip()]
        if parts:
            ids = parts

    total, rows = _query(cat_name, pg, limit, ids, wd, one_id)
    page_count = 1 if (ids or one_id) else max(1, (total + limit - 1) // limit)

    ep_map = _episodes_for([_text(r["slug"]) for r in rows])
    # type_id 反查
    name2tid = {c["_name"]: c["type_id"] for c in classes}
    items = []
    for r in rows:
        r["_tid"] = name2tid.get(_text(r.get("cat")) or "未分类", "")
        items.append(_vod_item(r, ep_map.get(_text(r["slug"]), [])))

    if want_xml:
        return Response(_xml_vods(items, pg, page_count, total), mimetype="text/xml")
    return {"code": 1, "msg": "数据列表", "page": pg, "pagecount": page_count,
            "limit": str(limit), "total": str(total), "list": items}


@bp.route("/pic")
@bp.route("/pic/<path:b64>")
def pic(b64=None):
    """图片代理：客户端访问不到图床时走后端转发。"""
    url = ""
    if b64:
        try:
            url = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
        except Exception:
            return Response("invalid b64", status=400)
    else:
        url = _text(request.args.get("url"))
    if not url.lower().startswith("http"):
        return Response("bad url", status=400)
    try:
        r = requests.get(url, timeout=20, verify=False,
                         headers={"User-Agent": config.UA, "Referer": config.HOST + "/"})
    except Exception as e:
        return Response(str(e), status=502)
    if r.status_code != 200 or not r.content:
        return Response("upstream %s" % r.status_code, status=502)
    resp = Response(r.content, mimetype=_image_mime(r.content, r.headers.get("Content-Type")))
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


def register(app):
    app.register_blueprint(bp)
