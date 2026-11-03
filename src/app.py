# -*- coding: utf-8 -*-
"""4kvm 原型服务：播放代理 + 浏览接口 + STRM 导出。

关键点（详见 ../README.md）：
  1. 取流地址由站点 wasm 现场签名，会过期 -> 每次播放前调 resolve.mjs 重新取
  2. 分片 URL 没有 .ts 后缀 -> 改写成本服务的 /seg/xxx.ts
  3. 分片开头有 73 字节假 PNG 头 -> 代理剥掉
  4. 播放列表里有诱饵分片（/ets/...，实测 404）-> 直接丢弃
  5. 分片是公开静态资源、不绑 IP -> 可以磁盘缓存
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import time

import requests
import urllib3
from flask import Flask, Response, jsonify, request, send_from_directory

import config
import crawl
import db
import maccms
import settings
import strm

urllib3.disable_warnings()

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
maccms.register(app)   # 苹果CMS 兼容接口 /api.php/provide/vod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = BASE_DIR

_filters_cache = {"at": 0.0, "data": None}
_resolve_cache = {}
_cache_lock = threading.Lock()
_session = requests.Session()
_session.headers.update({"User-Agent": config.UA})


# ---------------------------------------------------------------- 任务
class Job:
    """单任务执行器：同一时间只跑一个，记录日志与进度。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.kind = ""
        self.started = 0.0
        self.logs = []
        self.progress = {"phase": "", "done": 0, "total": 0}
        self.result = None

    def add(self, line):
        with self.lock:
            self.logs.append(line)
            if len(self.logs) > 400:
                self.logs = self.logs[-400:]

    def set_progress(self, phase, done, total):
        with self.lock:
            self.progress = {"phase": phase, "done": int(done), "total": int(total)}

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running, "kind": self.kind,
                "elapsed": int(time.time() - self.started) if self.running else 0,
                "logs": self.logs[-120:], "progress": dict(self.progress),
                "result": self.result,
            }

    def start(self, kind, fn):
        with self.lock:
            if self.running:
                return False, "已有任务在跑：%s" % self.kind
            self.running = True
            self.kind = kind
            self.started = time.time()
            self.logs = []
            self.progress = {"phase": "", "done": 0, "total": 0}
            self.result = None

        def run():
            try:
                self.result = fn()
                self.add("[完成] %s" % kind)
            except Exception as e:
                self.add("[错误] %s 失败：%r" % (kind, e))
                self.result = {"ok": False, "error": repr(e)}
            finally:
                with self.lock:
                    self.running = False

        threading.Thread(target=run, daemon=True).start()
        return True, "已开始"


job = Job()
for _mod in (strm, crawl):
    _mod.LOG_SINK = job.add
    _mod.PROGRESS = job.set_progress


def _crawl_all(pages=0, detail_limit=0, do_list=True, do_detail=True):
    """全流程：抓目录 -> 补详情。"""
    s = crawl.session()
    if do_list:
        crawl.log("① 抓目录：%s" % config.HOST)
        got, _ = crawl.crawl_list(s, pages, 1, None)
        crawl.log("目录完成：%d 部" % got)
    if do_detail:
        crawl.log("② 补详情")
        n = crawl.crawl_details(s, detail_limit)
        crawl.log("详情完成：%d 部" % n)
    return db.stats()


# ---------------------------------------------------------------- 取流
_userlink_cache = {}
_USERLINK_RE = re.compile(r"userlink:'([^']*)'")
_USERLINK_TTL = 600


def userlink(slug):
    """站点要求取流时带上页面里的 userlink（匿名也有，每次访问随机）。"""
    now = time.time()
    with _cache_lock:
        hit = _userlink_cache.get(slug)
        if hit and now - hit[0] < _USERLINK_TTL:
            return hit[1]
    try:
        r = _session.get("%s/play/%s" % (config.HOST, slug), timeout=25, verify=False,
                         headers={"Referer": config.HOST + "/"})
        if r.status_code != 200:
            return ""
        m = _USERLINK_RE.search(r.text)
        if not m:
            return ""
        val = m.group(1)
    except Exception:
        return ""
    if val:
        with _cache_lock:
            _userlink_cache[slug] = (now, val)
    return val


def resolve(ep_id, slug, quality=None):
    """调 Node + wasm 拿到临时 m3u8 地址，缓存几分钟。"""
    quality = quality or config.DEFAULT_QUALITY
    key = "%s|%s|%s" % (ep_id, slug, quality)
    now = time.time()
    with _cache_lock:
        hit = _resolve_cache.get(key)
        if hit and now - hit[0] < config.RESOLVE_TTL:
            return hit[1]

    pk = userlink(slug)
    if not pk:
        return {"ok": False, "error": "取不到页面 userlink（详情页打不开？）"}

    try:
        p = subprocess.run(
            [config.NODE_BIN, os.path.join(SRC_DIR, "resolve.mjs"),
             str(ep_id), str(slug), str(quality), str(pk)],
            capture_output=True, text=True, timeout=60, cwd=SRC_DIR,
        )
    except Exception as e:
        return {"ok": False, "error": "调用 node 失败: %r" % (e,)}

    line = (p.stdout or "").strip().splitlines()
    if not line:
        return {"ok": False, "error": "node 无输出: %s" % (p.stderr or "")[:200]}
    try:
        data = json.loads(line[-1])
    except Exception:
        return {"ok": False, "error": "解析 node 输出失败: %s" % line[-1][:200]}
    if data.get("ok"):
        with _cache_lock:
            _resolve_cache[key] = (now, data)
    return data


# ---------------------------------------------------------------- 分片处理
def ts_offset(buf):
    """找真正的 MPEG-TS 起点（连续 4 个 188 字节同步字节）。"""
    limit = min(len(buf) - 188 * 4, 4096)
    for i in range(max(0, limit)):
        if (buf[i] == 0x47 and buf[i + 188] == 0x47
                and buf[i + 376] == 0x47 and buf[i + 564] == 0x47):
            return i
    return 0


def seg_cache_path(url):
    h = hashlib.sha1(url.encode()).hexdigest()
    return os.path.join(config.SEG_CACHE_DIR, h[:2], h + ".ts")


def _cache_size_mb():
    total = 0
    for root, _dirs, files in os.walk(config.SEG_CACHE_DIR):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1024 / 1024


def _trim_cache():
    if not config.SEG_CACHE:
        return
    if _cache_size_mb() < config.SEG_CACHE_MAX_MB:
        return
    files = []
    for root, _dirs, fs in os.walk(config.SEG_CACHE_DIR):
        for f in fs:
            p = os.path.join(root, f)
            try:
                files.append((os.path.getmtime(p), p))
            except OSError:
                pass
    files.sort()
    for _mt, p in files[: max(1, len(files) // 3)]:
        try:
            os.remove(p)
        except OSError:
            pass


def fetch_segment(url):
    """取分片并剥掉假 PNG 头，带磁盘缓存。"""
    if config.SEG_CACHE:
        p = seg_cache_path(url)
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
    r = _session.get(url, timeout=60, verify=False)
    if r.status_code != 200:
        return None
    body = r.content
    off = ts_offset(body)
    payload = body[off:] if off else body
    if config.SEG_CACHE and len(payload) > 1000:
        p = seg_cache_path(url)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(payload)
        os.replace(tmp, p)
        if int(time.time()) % 50 == 0:
            _trim_cache()
    return payload


def b64e(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


# ---------------------------------------------------------------- 路由
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/hls.min.js")
def ui_hls():
    """播放器用的 hls.js，内置在镜像里，不依赖外网 CDN。"""
    return send_from_directory(BASE_DIR, "hls.min.js")


@app.route("/api/stats")
def api_stats():
    s = db.stats()
    s["ok"] = True
    s["seg_cache_mb"] = round(_cache_size_mb(), 1) if config.SEG_CACHE else 0
    s["seg_mode"] = config.SEG_MODE
    s["strm_root"] = config.STRM_ROOT
    s["public_base"] = settings.public_base()
    s["api_key"] = settings.api_key()
    s["host"] = config.HOST
    s["cms_url"] = _cms_url()
    return jsonify(s)


def _cms_url():
    u = "%s/api.php/provide/vod" % settings.public_base()
    k = settings.api_key()
    return (u + "?key=" + k) if k else u


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    """读写运行时设置：服务地址 / 接口密码。改完可顺带重新导出 STRM。"""
    if request.method == "GET":
        return jsonify({
            "ok": True,
            "settings": {"public_base": settings.public_base(), "api_key": settings.api_key()},
            "cms_url": _cms_url(),
            "strm_root": config.STRM_ROOT,
        })

    d = request.get_json(silent=True) or {}
    pairs = {}
    if "public_base" in d:
        u = str(d.get("public_base") or "").strip().rstrip("/")
        if u and not u.startswith("http"):
            u = "http://" + u
        pairs["public_base"] = u
    if "api_key" in d:
        pairs["api_key"] = str(d.get("api_key") or "").strip()
    if pairs:
        settings.set_many(pairs)

    regen = bool(d.get("regen_strm"))
    if regen:
        job.start("重新导出 STRM", lambda: strm.export(
            nfo=bool(d.get("nfo")), images=bool(d.get("images")), clean=True))

    return jsonify({
        "ok": True,
        "settings": {"public_base": settings.public_base(), "api_key": settings.api_key()},
        "cms_url": _cms_url(),
        "regen": regen,
        "msg": "已保存" + ("，正在重新导出 STRM" if regen else ""),
    })


@app.route("/api/list")
def api_list():
    limit = min(int(request.args.get("limit", 60)), 200)
    offset = int(request.args.get("offset", 0))
    kw = (request.args.get("q") or "").strip()
    cat = (request.args.get("cat") or "").strip()
    year = (request.args.get("year") or "").strip()
    include_vip = request.args.get("vip") == "1"
    rows, total = db.list_vods(limit=limit, offset=offset, keyword=kw, cat=cat,
                               year=year, only_ok=not include_vip)
    return jsonify({"ok": True, "total": total, "list": rows})


@app.route("/api/filters")
def api_filters():
    """浏览页的筛选栏：分类 + 年份（各带数量）。缓存 30 秒。"""
    now = time.time()
    if _filters_cache["data"] and now - _filters_cache["at"] < 30:
        return jsonify(_filters_cache["data"])
    data = {"ok": True}
    data.update(db.filters())
    _filters_cache["at"] = now
    _filters_cache["data"] = data
    return jsonify(data)


@app.route("/api/eps")
def api_eps():
    slug = (request.args.get("slug") or "").strip()
    if not slug:
        return jsonify({"ok": False, "error": "缺少 slug"}), 400
    return jsonify({"ok": True, "list": db.episodes(slug)})


@app.route("/v/<slug>/<ep>.m3u8")
@app.route("/v/<slug>/<ep>")
def playlist(slug, ep):
    """播放入口：现取临时 m3u8 -> 丢诱饵 -> 分片改写成 /seg/xxx.ts。"""
    quality = request.args.get("q") or None
    info = resolve(ep, slug, quality)
    if not info.get("ok"):
        return jsonify({"ok": False, "error": info.get("error")}), 502

    src = info["url"]
    try:
        r = _session.get(src, timeout=30, verify=False,
                         headers={"Referer": config.HOST + "/"})
    except Exception as e:
        return jsonify({"ok": False, "error": repr(e)}), 502
    if r.status_code != 200:
        # token 过期：清缓存，让下一次重新签名
        with _cache_lock:
            _resolve_cache.pop("%s|%s|%s" % (ep, slug, quality or config.DEFAULT_QUALITY), None)
        return jsonify({"ok": False, "error": "上游 %s: %s" % (r.status_code, r.text[:80])}), 502

    base = src.rsplit("/", 1)[0] + "/"
    mode = (request.args.get("mode") or config.SEG_MODE).lower()
    if mode not in ("direct", "proxy"):
        mode = config.SEG_MODE

    out, dropped = [], 0
    for line in r.text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            out.append(s)
            continue
        if s.startswith("/ets/"):
            dropped += 1          # 诱饵分片（实测 404），丢掉
            continue
        full = s if s.startswith("http") else base + s
        if mode == "direct":
            # 直连：分片地址原样给客户端，由客户端自己去 CDN 拉
            # （ffmpeg/Emby 实测能接受「无扩展名 + 73 字节假 PNG 头」的分片）
            out.append(full)
        else:
            out.append("/seg/%s.ts" % b64e(full))

    resp = Response("\n".join(out) + "\n", mimetype="application/vnd.apple.mpegurl")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Dropped-Decoys"] = str(dropped)
    resp.headers["X-Quality"] = str(info.get("quality", ""))
    resp.headers["X-Seg-Mode"] = mode
    return resp


@app.route("/seg/<token>.ts")
def segment(token):
    try:
        url = b64d(token)
    except Exception:
        return Response("bad token", status=400)
    if not url.lower().startswith("http"):
        return Response("bad token", status=400)
    payload = fetch_segment(url)
    if payload is None:
        return Response("upstream error", status=502)
    resp = Response(payload, mimetype="video/mp2t")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/export", methods=["POST"])
def api_export():
    """导出 STRM（后台任务，进度可查）。"""
    d = request.get_json(silent=True) or {}
    cat = (d.get("cat") or "").strip()
    limit = int(d.get("limit") or 0)
    nfo = bool(d.get("nfo"))
    images = bool(d.get("images"))
    ep_nfo = bool(d.get("ep_nfo"))
    clean = bool(d.get("clean", True))
    root = (d.get("root") or "").strip() or None

    ok, msg = job.start("导出 STRM", lambda: strm.export(
        root=root, cat=cat, limit=limit, nfo=nfo, images=images,
        clean=clean, ep_nfo=ep_nfo))
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 409)


# ---------------------------------------------------------------- 自动更新
_next_run = [0.0]


def _auto_pipeline():
    """自动更新：抓目录 -> 补详情 -> 导出 STRM。"""
    s = crawl.session()
    crawl.log("=== 自动更新开始 ===")
    crawl.log("① 抓目录")
    crawl.crawl_list(s, 0, 1, None)
    crawl.log("② 补详情")
    crawl.crawl_details(s, 0)
    if config.AUTO_EXPORT:
        crawl.log("③ 导出 STRM")
        strm.export(nfo=bool(config.AUTO_NFO), images=bool(config.AUTO_IMAGES), clean=True)
    settings.mark_finished()
    crawl.log("=== 自动更新完成：%s ===" % db.stats())
    return db.stats()


def _auto_loop():
    if config.BOOTSTRAP:
        time.sleep(10)
    elif config.INTERVAL_HOURS > 0:
        _next_run[0] = time.time() + config.INTERVAL_HOURS * 3600
        time.sleep(config.INTERVAL_HOURS * 3600)
    else:
        return

    while True:
        while job.running:
            time.sleep(5)
        job.start("自动更新", _auto_pipeline)
        while job.running:
            time.sleep(5)
        if config.INTERVAL_HOURS <= 0:
            crawl.log("未设置 VM_INTERVAL_HOURS，自动更新只跑这一次")
            _next_run[0] = 0.0
            return
        _next_run[0] = time.time() + config.INTERVAL_HOURS * 3600
        time.sleep(config.INTERVAL_HOURS * 3600)


@app.route("/api/crawl", methods=["POST"])
def api_crawl():
    """触发抓取。mode: list=只抓目录 / detail=只补详情 / all=全流程"""
    d = request.get_json(silent=True) or {}
    mode = (d.get("mode") or "all").strip()
    pages = int(d.get("pages") or 0)
    detail_limit = int(d.get("detail_limit") or 0)

    if mode == "list":
        ok, msg = job.start("抓目录", lambda: _crawl_all(pages, 0, True, False))
    elif mode == "detail":
        ok, msg = job.start("补详情", lambda: _crawl_all(0, detail_limit, False, True))
    else:
        ok, msg = job.start("全流程抓取", lambda: _crawl_all(pages, detail_limit, True, True))
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 409)


@app.route("/api/job")
def api_job():
    return jsonify({"ok": True, "job": job.snapshot(), "next_run": _next_run[0]})


def main():
    db.init_db()
    print("4kvm 服务")
    print("  站点    %s" % config.HOST)
    print("  数据库  %s" % config.DB_PATH)
    print("  地址    http://127.0.0.1:%d/" % config.LISTEN_PORT)
    print("  播放    %s/v/<slug>/<ep>.m3u8" % config.PUBLIC_BASE)
    print("  分片    %s 模式（direct=客户端直连 CDN，proxy=本服务转发）" % config.SEG_MODE)
    print("  STRM    %s" % config.STRM_ROOT)
    print("  自动更新 %s" % (
        ("启动即跑一次" if config.BOOTSTRAP else "不自动跑")
        + ("，每 %.1f 小时" % config.INTERVAL_HOURS if config.INTERVAL_HOURS > 0 else "")))
    if config.BOOTSTRAP or config.INTERVAL_HOURS > 0:
        threading.Thread(target=_auto_loop, daemon=True).start()
    print("  CMS     %s" % _cms_url())
    try:
        from waitress import serve
        print("  WSGI    waitress（threads=%d）" % config.THREADS)
        serve(app, host=config.LISTEN_HOST, port=config.LISTEN_PORT,
              threads=config.THREADS, ident="4kvm")
    except ImportError:
        print("  WSGI    Flask 开发服务器（未装 waitress）")
        app.run(host=config.LISTEN_HOST, port=config.LISTEN_PORT, threaded=True)


if __name__ == "__main__":
    main()
