# -*- coding: utf-8 -*-
"""STRM 导出（对照金牌系统的 strm.py 实现）。

目录结构：
    <root>/<分类>/<标题 (年份)>/<标题>.strm                单集（电影）
    <root>/<分类>/<标题 (年份)>/Season 01/<标题> S01E01.strm  多集

.strm 内容：
    <PUBLIC_BASE>/v/<slug>/<ep_id>.m3u8

和金牌版的差异：
  * 4kvm 的播放地址是**长期有效**的（只要 slug + ep_id 不变），不需要每次重新解析
  * 站点只有竖版封面（270×450），和金牌一样只存 poster.jpg，不做假 fanart
  * 没有「剧集状态变化」概念，manifest 只存「路径 -> 地址」

性能要点（同金牌版）：
  * 分集一次性读进内存（5.5k 部 / 10 万集量级没问题）
  * 目录只创建一次
  * 海报并行下载，存在即跳过
  * manifest 只存「路径 -> 地址」，未变化的不重写
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests
import urllib3

import config
import db
import settings

urllib3.disable_warnings()

_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]')

LOG_SINK = None
PROGRESS = None


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        try:
            print(line.encode("gbk", "replace").decode("gbk", "replace"), flush=True)
        except Exception:
            pass
    if LOG_SINK:
        try:
            LOG_SINK(line)
        except Exception:
            pass


def progress(phase, done, total):
    if PROGRESS:
        try:
            PROGRESS(phase, int(done), int(total))
        except Exception:
            pass


def safe_name(name, fallback="未命名"):
    s = _BAD.sub(" ", str(name or "")).strip()
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s or fallback)[:120]


def play_url(slug, ep_id):
    # 地址来自运行时设置（界面可改域名），改完重新导出即可
    u = "%s/v/%s/%s.m3u8" % (settings.public_base(), slug, ep_id)
    return u


def xml_escape(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def movie_nfo(v):
    return """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <title>%s</title>
  <originaltitle>%s</originaltitle>
  <year>%s</year>
  <plot>%s</plot>
  <runtime>0</runtime>
  <genre>%s</genre>
  <uniqueid type="4kvm" default="true">%s</uniqueid>
</movie>
""" % (xml_escape(v["name"]), xml_escape(v["name"]), xml_escape(v.get("year")),
       xml_escape(v.get("remarks")), xml_escape(v.get("cat")), xml_escape(v["slug"]))


def tvshow_nfo(v):
    return """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <title>%s</title>
  <year>%s</year>
  <plot>%s</plot>
  <genre>%s</genre>
  <uniqueid type="4kvm" default="true">%s</uniqueid>
</tvshow>
""" % (xml_escape(v["name"]), xml_escape(v.get("year")), xml_escape(v.get("remarks")),
       xml_escape(v.get("cat")), xml_escape(v["slug"]))


def episode_nfo(v, ep):
    return """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<episodedetails>
  <title>第%d集</title>
  <season>1</season>
  <episode>%d</episode>
  <plot>%s</plot>
</episodedetails>
""" % (int(ep["seq"] or 0), int(ep["seq"] or 0), xml_escape(v.get("remarks")))


class _Writer:
    """目录只建一次，文件写入统一走这里。"""

    def __init__(self):
        self._dirs = set()
        self.files = 0

    def ensure_dir(self, path):
        d = os.path.dirname(path)
        if d and d not in self._dirs:
            os.makedirs(d, exist_ok=True)
            self._dirs.add(d)

    def write(self, path, text):
        self.ensure_dir(path)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        self.files += 1


def _download_images(items, workers=8):
    """items: [(url, series_dir)]。只下竖版封面 poster.jpg，并行执行。"""

    def one(item):
        url, d = item
        p = os.path.join(d, "poster.jpg")
        if os.path.exists(p):
            return 0
        try:
            r = requests.get(url, timeout=25, verify=False,
                             headers={"User-Agent": config.UA, "Referer": config.HOST + "/"})
            if r.status_code != 200 or not r.content:
                return 0
            os.makedirs(d, exist_ok=True)
            with open(p, "wb") as f:
                f.write(r.content)
            return 1
        except Exception:
            return 0

    if not items:
        return 0
    done, total = 0, len(items)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, n in enumerate(pool.map(one, items), 1):
            done += n
            if i % 200 == 0 or i == total:
                progress("下载海报", i, total)
    return done


def _load_jobs(cat="", limit=0):
    """读库：返回 [(vod_dict, [ep_dict...])]，只保留有分集的。"""
    c = db.conn()
    if cat:
        vods = c.execute("SELECT * FROM vod WHERE cat=? ORDER BY slug", (cat,)).fetchall()
    else:
        vods = c.execute("SELECT * FROM vod ORDER BY slug").fetchall()
    if limit:
        vods = vods[:limit]

    ep_map = {}
    for r in c.execute("SELECT slug,ep_id,seq FROM episode ORDER BY slug, seq"):
        ep_map.setdefault(r["slug"], []).append(
            {"ep_id": r["ep_id"], "seq": int(r["seq"] or 0)})
    return [(dict(v), ep_map.get(v["slug"], [])) for v in vods if ep_map.get(v["slug"])]


def export(root=None, cat="", limit=0, nfo=False, images=False, clean=False, ep_nfo=False):
    t0 = time.time()
    db.init_db()
    base_url = settings.public_base()
    if not base_url or "127.0.0.1" in base_url or "localhost" in base_url:
        log("[警告] 服务地址是 %s，只有本机能访问" % (base_url or "(空)"))
        log("       如果 Emby 在 Docker 里或要从别的设备访问，请到运维页改成局域网 IP")
    root = os.path.abspath(root or config.STRM_ROOT)
    os.makedirs(root, exist_ok=True)
    manifest_path = os.path.join(root, "_manifest.json")

    # ---------- 1. 读库 + manifest ----------
    t = time.time()
    manifest = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                raw = json.load(f)
            for k, val in raw.items():
                manifest[k] = val.get("url", "") if isinstance(val, dict) else val
        except Exception:
            manifest = {}

    jobs = _load_jobs(cat, limit)
    total_jobs = len(jobs)
    total_eps = sum(len(e) for _, e in jobs)
    log("读取完成：%d 部 / %d 集，耗时 %.1fs" % (total_jobs, total_eps, time.time() - t))

    # ---------- 2. 写文件 ----------
    t = time.time()
    w = _Writer()
    now_manifest = {}
    used_dirs = {}
    pending_img = []
    total_files = written = 0
    progress("导出 STRM", 0, total_jobs)

    for idx, (v, eps) in enumerate(jobs, 1):
        if idx % 50 == 0 or idx == total_jobs:
            progress("导出 STRM", idx, total_jobs)

        type_dir = safe_name(v.get("cat") or "未分类")
        title = safe_name(v["name"] or v["slug"])
        year = str(v.get("year") or "").strip()
        folder = "%s (%s)" % (title, safe_name(year)) if year else title
        series_dir = os.path.join(root, type_dir, folder)

        # 同名同年的不同资源用 slug 区分，避免互相覆盖
        key = os.path.normcase(series_dir)
        if key in used_dirs and used_dirs[key] != v["slug"]:
            series_dir = "%s [%s]" % (series_dir, v["slug"])
            key = os.path.normcase(series_dir)
        used_dirs[key] = v["slug"]

        if len(eps) <= 1:
            ep = eps[0]
            url = play_url(v["slug"], ep["ep_id"])
            path = os.path.join(series_dir, title + ".strm")
            total_files += 1
            # 不能只比对地址：文件被手动删掉时 manifest 还留着旧记录，
            # 只比地址会误判成「没变化」而跳过。
            if manifest.get(os.path.normcase(path)) != url or not os.path.exists(path):
                w.write(path, url + "\n")
                written += 1
            now_manifest[os.path.normcase(path)] = url
            if nfo:
                w.write(os.path.join(series_dir, "movie.nfo"), movie_nfo(v))
        else:
            season_dir = os.path.join(series_dir, "Season 01")
            for ep in eps:
                seq = int(ep["seq"] or 0) or 1
                url = play_url(v["slug"], ep["ep_id"])
                path = os.path.join(season_dir, "%s S01E%02d.strm" % (title, seq))
                total_files += 1
                if manifest.get(os.path.normcase(path)) != url or not os.path.exists(path):
                    w.write(path, url + "\n")
                    written += 1
                now_manifest[os.path.normcase(path)] = url
                if ep_nfo:
                    w.write(os.path.join(season_dir, "%s S01E%02d.nfo" % (title, seq)),
                            episode_nfo(v, ep))
            if nfo:
                w.write(os.path.join(series_dir, "tvshow.nfo"), tvshow_nfo(v))

        if images and v.get("pic"):
            pending_img.append((v["pic"], series_dir))

    log("写入 .strm：%d 个（新写/更新 %d 个，未变的跳过），耗时 %.1fs"
        % (total_files, written, time.time() - t))

    # ---------- 3. 海报 ----------
    if pending_img:
        t = time.time()
        log("下载海报：%d 部，并发 %d" % (len(pending_img), config.IMAGE_WORKERS))
        got = _download_images(pending_img, workers=config.IMAGE_WORKERS)
        log("海报完成：新下载 %d 张，耗时 %.1fs" % (got, time.time() - t))

    # ---------- 4. 清理孤儿 ----------
    removed = 0
    if clean and total_files == 0 and manifest:
        # 安全兜底：本次一个文件都没生成（多半是库空了 / 抓取失败 / 卷没挂上），
        # 这时候按「孤儿」清会把上一次导出的全部文件删光，必须拦住。
        log("[警告] 本次没有生成任何 .strm（库是空的？），已跳过清理，"
            "避免误删上次的 %d 个文件" % len(manifest))
        clean = False
    if clean:
        t = time.time()
        for old in list(manifest.keys()):
            if old in now_manifest:
                continue
            try:
                if os.path.exists(old):
                    os.remove(old)
                    removed += 1
            except Exception:
                pass
            # 顺带删掉同名的 .nfo，避免留下孤立元数据
            nfo = os.path.splitext(old)[0] + ".nfo"
            try:
                if os.path.exists(nfo):
                    os.remove(nfo)
            except Exception:
                pass
        # 删掉变空的目录（自底向上）
        pruned = 0
        for d, _dirs, files in os.walk(root, topdown=False):
            if os.path.abspath(d) == os.path.abspath(root):
                continue
            try:
                if not os.listdir(d):
                    os.rmdir(d)
                    pruned += 1
            except Exception:
                pass
        if removed or pruned:
            log("清理孤儿：%d 个 .strm、%d 个空目录，耗时 %.1fs"
                % (removed, pruned, time.time() - t))

    # ---------- 5. manifest ----------
    t = time.time()
    try:
        tmp = manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(now_manifest, f, ensure_ascii=False)
        os.replace(tmp, manifest_path)
    except Exception as e:
        log("[警告] manifest 写入失败：%r" % (e,))
    log("manifest：%d 条，耗时 %.1fs" % (len(now_manifest), time.time() - t))

    progress("导出 STRM", total_jobs, total_jobs)
    result = {"files": total_files, "written": written, "removed": removed, "root": root}
    log("导出完成：共 %d 个 .strm（新写/更新 %d），删除 %d，总耗时 %.1fs -> %s"
        % (total_files, written, removed, time.time() - t0, root))
    return result
