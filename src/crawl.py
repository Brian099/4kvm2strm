# -*- coding: utf-8 -*-
"""抓取 4kvm：目录（/filter?page=N）+ 详情（/play/<slug>）。

只存站内 id（slug / dataid），不存任何会过期的播放地址。

用法：
    python crawl.py --pages 3           # 只抓前 3 页（试水）
    python crawl.py                     # 抓全部 184 页
    python crawl.py --detail-only       # 只补详情
    python crawl.py --detail-limit 50   # 最多补 50 部的详情
"""
import argparse
import html
import json
import random
import subprocess
import re
import sys
import time
from urllib.parse import quote, unquote

import requests
import urllib3

import config
import db

urllib3.disable_warnings()

# 供 server 的任务把日志/进度接到前端
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


# 分类：/filter?classify=<id>&page=<n>（实测 1=电影 3618 部/121 页，2=电视剧 1632/55，3=动漫 241/9）
CATS = {"1": "电影", "2": "电视剧", "3": "动漫"}

# 列表卡片：定位 data-vod-id 后向后切片解析（比整块正则稳，站点小改版也不易挂）
REMARK = re.compile(r'<span[^>]*class="[^"]*(?:text-green-500|text-yellow-400|absolute bottom-0)[^"]*"[^>]*>([^<]{1,20})</span>')
PAGES = re.compile(r'共\s*(\d+)\s*页')
# 详情页分集：dataid（取流必需）+ data-episode（集数），两种属性顺序都兼容
EP = re.compile(
    r'dataid="(?P<ep_id>\d+)"[^>]*?data-episode="(?P<seq>\d+)"'
    r'|data-episode="(?P<seq2>\d+)"[^>]*?dataid="(?P<ep_id2>\d+)"',
    re.S,
)
TOTAL = re.compile(r'共\s*([\d,]+)\s*(?:部|个|条)')


class Prober:
    """常驻 Node 探测进程：判断一部片有没有「免费可播源」。

    站点把 VIP 专属片和 4K 蓝光都做成「取流接口返回 401 / 只给占位地址」，
    只有真的调一次取流接口才知道。常驻进程是为了摊掉 Node 启动开销。
    """

    def __init__(self):
        self.proc = None

    def start(self):
        if self.proc:
            return True
        try:
            self.proc = subprocess.Popen(
                [config.NODE_BIN, "probe.mjs"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", cwd=config.BASE_DIR,
            )
            line = self.proc.stdout.readline()
            return '"ready":true' in line.replace(" ", "")
        except Exception as e:
            log("[警告] 探测进程启动失败：%r" % (e,))
            self.proc = None
            return False

    def probe(self, ep_id, slug, pk, quality="1080", timeout=40):
        """返回 dict：{code, free, has4k, msg}；失败返回 None。"""
        if not self.start():
            return None
        try:
            self.proc.stdin.write(json.dumps({
                "id": str(ep_id), "secret": slug, "pk": pk, "q": quality}) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
            if not line:
                self.close()
                return None
            return json.loads(line)
        except Exception as e:
            log("[警告] 探测异常：%r" % (e,))
            self.close()
            return None

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None


def session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": config.UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": config.HOST + "/",
    })
    return s


def get(s, path, tries=3):
    for i in range(tries):
        try:
            r = s.get(config.HOST + path, timeout=25, verify=False)
            if r.status_code == 200:
                return r
            if r.status_code in (404, 410):
                return r
        except Exception as e:
            if i == tries - 1:
                print("  [警告] %s 失败: %r" % (path, e))
        time.sleep(1.5 + random.random())
    return None


def clean_pic(raw):
    """列表里的图走百度 gimg 代理，真实地址在 src= 参数里。

    注意：从 src= 里解出来的地址**不带协议头**（如 4kvm.staticimgjs.org/...），
    必须补上 https://，否则浏览器会当相对路径，封面全裂。
    """
    u = html.unescape(raw or "").strip()
    m = re.search(r'[?&]src=([^&]+)', u)
    if m:
        u = unquote(m.group(1)).strip()
    if u.startswith("//"):
        u = "https:" + u
    elif u and not re.match(r'^https?://', u, re.I):
        u = "https://" + u.lstrip("/")
    return u


def parse_list(h, cat=""):
    out, seen = [], set()
    for m in re.finditer(r'data-vod-id="([a-z0-9]+)"', h):
        slug = m.group(1)
        if slug in seen:
            continue
        block = h[m.start():m.start() + 3000]
        nm = re.search(r'alt="([^"]+)"', block)
        if not nm:
            continue
        name = html.unescape(nm.group(1)).strip()
        if not name:
            continue
        pm = re.search(r'data-src="([^"]+)"', block)
        rm = REMARK.search(block)
        seen.add(slug)
        out.append({
            "slug": slug,
            "name": name,
            "pic": clean_pic(pm.group(1)) if pm else "",
            "cat": cat,
            "remarks": html.unescape(rm.group(1)).strip() if rm else "",
        })
    return out


_REGIONS = ("中国大陆", "中国", "香港", "台湾", "美国", "日本", "韩国", "英国", "法国",
            "德国", "印度", "泰国", "意大利", "西班牙", "加拿大", "澳大利亚", "俄罗斯")


def parse_meta(h):
    """详情页的 meta keywords 是逗号分隔的结构化信息：
       ,<片名>,<年份>,<类型...>,<国家>,<地区>,<语言>,<导演>
       例：,牧神记 年番,2024,动画,奇幻,冒险,中国,中国大陆,汉语普通话,沈乐平
    """
    m = re.search(r'<meta[^>]*name="keywords"[^>]*content="([^"]*)"', h)
    if not m:
        return {}
    parts = [x.strip() for x in html.unescape(m.group(1)).split(",")]
    parts = [x for x in parts if x]
    info = {"year": "", "area": "", "lang": ""}
    if len(parts) >= 2:
        for p in parts:
            if re.fullmatch(r"(19|20)\d{2}", p):
                info["year"] = p
                break
        for p in parts:
            if p in _REGIONS:
                info["area"] = p
                break
        for p in parts:
            if p.endswith("语") or "普通话" in p:
                info["lang"] = p
                break
    return info


def parse_detail(h):
    eps, seen = [], set()
    for m in EP.finditer(h):
        ep_id = m.group("ep_id") or m.group("ep_id2")
        seq = m.group("seq") or m.group("seq2")
        if not ep_id or ep_id in seen:
            continue
        seen.add(ep_id)
        eps.append({"ep_id": ep_id, "seq": int(seq or 0) or (len(eps) + 1), "name": ""})
    eps.sort(key=lambda e: e["seq"])
    return eps


def crawl_list(s, pages, start=1, cats=None):
    """按分类抓目录：/filter?classify=<id>&page=<n>（classify 参数来自 4KVM.py，已实测）"""
    cats = cats or list(CATS)
    got = 0
    for tid in cats:
        cat_name = CATS.get(tid, "")
        first = get(s, "/filter?classify=%s" % tid)
        if not first or first.status_code != 200:
            log("分类 %s 首页失败，跳过" % cat_name)
            continue
        m = TOTAL.search(first.text)
        pm = PAGES.search(first.text)
        total = int(m.group(1).replace(",", "")) if m else 0
        max_page = int(pm.group(1)) if pm else 0
        if not max_page and total:
            max_page = (total + config.PAGE_SIZE - 1) // config.PAGE_SIZE
        log("[%s] 共 %d 部 / %d 页" % (cat_name, total, max_page))

        last = min(max_page or pages, start + pages - 1) if pages else (max_page or 1)
        for pg in range(start, last + 1):
            if pg == 1:
                r = first
            else:
                r = get(s, "/filter?classify=%s&page=%d" % (tid, pg))
            if not r or r.status_code != 200:
                log("  %s 第 %d 页失败，跳过" % (cat_name, pg))
                continue
            items = parse_list(r.text, cat_name)
            for it in items:
                db.upsert_vod(it)
            got += len(items)
            db.set_state("last_%s" % tid, pg)
            log("  %s 第 %d/%d 页：%d 部（累计 %d）" % (cat_name, pg, last, len(items), got))
            if not items:
                break
            time.sleep(config.LIST_SLEEP + random.random() * 0.5)
    return got, 0


def crawl_search(s, keyword, pages=1):
    """站内搜索。

    实测（2026-11）：/search?q=xxx 返回 200 但**没有任何结果卡片**；
    换 ?wd= / ?keyword= 会被跳回首页。站内搜索实际不可用，
    搜索请直接查本地库（app.py 的 /api/list?q=）。
    """
    got = 0
    for pg in range(1, pages + 1):
        path = "/search?q=" + quote(keyword)
        if pg > 1:
            path += "&page=%d" % pg
        r = get(s, path)
        if not r or r.status_code != 200:
            log("搜索第 %d 页失败" % pg)
            continue
        items = parse_list(r.text, "搜索")
        for it in items:
            db.upsert_vod(it)
        got += len(items)
        log("搜索「%s」第 %d 页：%d 部" % (keyword, pg, len(items)))
        if not items:
            break
        time.sleep(config.LIST_SLEEP)
    return got


def judge_vip(prober, eps, slug, pk):
    """判断这部片该不该剔除。

    返回 (vip, reason)：
      (True,  "401")             明确要求 VIP
      (True,  "no-free-source")  取流成功但没有任何免费清晰度（再探一集确认过）
      (False, "ok")              正常
      (None,  "probe-failed")    探测失败，别动它
    """
    info = prober.probe(eps[0]["ep_id"], slug, pk)
    if info is None:
        return None, "probe-failed"

    code = info.get("code")
    if code == 401:
        return True, "401"
    if code != 200:
        return None, "code=%s" % code          # 404 之类的服务端抖动，不动它
    if info.get("free"):
        return False, "ok"

    # code=200 但没有免费清晰度：可能是服务端抖动，换一集再确认一次
    if len(eps) > 1:
        info2 = prober.probe(eps[1]["ep_id"], slug, pk)
        if info2 is None:
            return None, "probe-failed"
        if info2.get("code") == 200 and info2.get("free"):
            return False, "ok"                 # 第二集能播 -> 不剔除
        if info2.get("code") == 401:
            return True, "401"
    return True, "no-free-source"


def crawl_details(s, limit=0, check_vip=True):
    """补详情。check_vip=True 时顺带探测取流接口，VIP 专属/无免费源的片直接标记剔除。"""
    rows = db.conn().execute(
        "SELECT slug,name FROM vod WHERE detail_at=0 ORDER BY rowid LIMIT ?",
        (limit or 10 ** 9,)).fetchall()
    log("待补详情：%d 部（VIP 探测：%s）" % (len(rows), "开" if check_vip else "关"))
    prober = Prober() if check_vip else None
    ok = vip = 0
    for i, row in enumerate(rows, 1):
        slug = row["slug"]
        r = get(s, "/play/" + slug)
        if not r or r.status_code != 200:
            log("  [%d/%d] %s 失败" % (i, len(rows), slug))
            continue
        eps = parse_detail(r.text)
        if not eps:
            log("  [%d/%d] %s 没解析到分集" % (i, len(rows), slug))
            continue
        # 探测有没有免费可播源（VIP 专属片、只有 4K 占位地址的片都会被剔除）
        if prober:
            m = re.search(r"userlink:'([^']*)'", r.text)
            pk = m.group(1) if m else "0"
            is_vip, why = judge_vip(prober, eps, slug, pk)
            if is_vip is None:
                log("  [%d/%d] %s 探测失败(%s)，跳过（下轮重试）"
                    % (i, len(rows), row["name"], why))
                time.sleep(config.DETAIL_SLEEP)
                continue
            if is_vip:
                db.mark_vip(slug, 1)
                vip += 1
                log("  [%d/%d] %s -> 剔除（%s）" % (i, len(rows), row["name"], why))
                time.sleep(config.DETAIL_SLEEP)
                continue

        meta = parse_meta(r.text)
        db.replace_episodes(slug, eps)
        c = db.conn()
        c.execute("UPDATE vod SET ep_count=?, detail_at=?, year=?, area=?, vip=0 WHERE slug=?",
                  (len(eps), int(time.time()), meta.get("year", ""),
                   meta.get("area", ""), slug))
        c.commit()
        ok += 1
        progress("补详情", i, len(rows))
        if i % 20 == 0 or i == len(rows):
            log("  详情 %d/%d（%s：%d 集）" % (i, len(rows), row["name"], len(eps)))
        time.sleep(config.DETAIL_SLEEP + random.random() * 0.5)
    if prober:
        prober.close()
    log("详情完成：入库 %d 部，剔除 %d 部（VIP/无源）" % (ok, vip))
    return ok


def recheck_vip(s, limit=0):
    """对已入库的片重新探测一遍，把 VIP 专属/无免费源的剔除。

    用于「VIP 探测」上线之前已经抓进来的数据。
    """
    rows = db.conn().execute(
        "SELECT slug,name FROM vod WHERE vip=0 AND ep_count>0 ORDER BY rowid LIMIT ?",
        (limit or 10 ** 9,)).fetchall()
    log("待复检：%d 部" % len(rows))
    prober = Prober()
    ok = vip = fail = 0
    for i, row in enumerate(rows, 1):
        slug = row["slug"]
        eps = db.episodes(slug)
        if not eps:
            continue
        try:
            h = s.get(config.HOST + "/play/" + slug, timeout=25, verify=False).text
        except Exception:
            fail += 1
            continue
        m = re.search(r"userlink:'([^']*)'", h)
        pk = m.group(1) if m else "0"
        is_vip, why = judge_vip(prober, eps, slug, pk)
        if is_vip is None:
            fail += 1
        elif is_vip:
            db.mark_vip(slug, 1)
            vip += 1
            log("  [%d/%d] %s -> 剔除（%s）" % (i, len(rows), row["name"], why))
        else:
            ok += 1
        progress("复检 VIP", i, len(rows))
        if i % 50 == 0 or i == len(rows):
            log("  复检 %d/%d（保留 %d，剔除 %d，失败 %d）" % (i, len(rows), ok, vip, fail))
        time.sleep(config.DETAIL_SLEEP * 0.6)
    prober.close()
    log("复检完成：保留 %d，剔除 %d，失败 %d" % (ok, vip, fail))
    return vip


def verify_vip(s, limit=0):
    """复核已被标记 vip 的片，把误判的恢复回来。

    标记时可能赶上服务端抖动，这里重新探两集：只要有一集能播就恢复
    （vip=0、detail_at=0，下一轮补详情会重新写入分集）。
    """
    rows = db.conn().execute(
        "SELECT slug,name FROM vod WHERE vip<>0 ORDER BY rowid LIMIT ?",
        (limit or 10 ** 9,)).fetchall()
    log("待复核：%d 部" % len(rows))
    prober = Prober()
    keep = restored = fail = 0
    for i, row in enumerate(rows, 1):
        slug = row["slug"]
        try:
            h = s.get(config.HOST + "/play/" + slug, timeout=25, verify=False).text
        except Exception:
            fail += 1
            continue
        ids = [{"ep_id": x, "seq": n + 1}
               for n, x in enumerate(re.findall(r'dataid="(\d+)"', h)[:2])]
        if not ids:
            fail += 1
            continue
        m = re.search(r"userlink:'([^']*)'", h)
        pk = m.group(1) if m else "0"
        is_vip, why = judge_vip(prober, ids, slug, pk)
        if is_vip is None:
            fail += 1
        elif is_vip:
            keep += 1
        else:
            c = db.conn()
            c.execute("UPDATE vod SET vip=0, detail_at=0, ep_count=0 WHERE slug=?", (slug,))
            c.commit()
            restored += 1
            log("  [%d/%d] %s -> 恢复（其实能播）" % (i, len(rows), row["name"]))
        progress("复核 VIP", i, len(rows))
        if i % 20 == 0 or i == len(rows):
            log("  复核 %d/%d（确认 VIP %d，恢复 %d，失败 %d）"
                % (i, len(rows), keep, restored, fail))
        time.sleep(config.DETAIL_SLEEP * 0.6)
    prober.close()
    log("复核完成：确认 VIP %d，恢复 %d，失败 %d" % (keep, restored, fail))
    return restored


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=0, help="抓多少页目录（0=全部）")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--detail-only", action="store_true")
    ap.add_argument("--detail-limit", type=int, default=0)
    ap.add_argument("--verify-vip", action="store_true",
                    help="复核已标记的 VIP 片，把误判的恢复回来")
    ap.add_argument("--recheck-vip", action="store_true",
                    help="对已入库的片重新探测并剔除 VIP/无源")
    ap.add_argument("--no-vip-check", action="store_true",
                    help="补详情时不探测 VIP（更快，但会留下播不了的片）")
    ap.add_argument("--skip-detail", action="store_true")
    ap.add_argument("--cats", default="", help="只抓这些分类，逗号分隔（1=电影 2=电视剧 3=动漫）")
    ap.add_argument("--search", default="", help="站内搜索（实测不可用，仅保留排查用）")
    args = ap.parse_args()

    db.init_db()
    s = session()
    print("目标站点：%s" % config.HOST)
    print("当前库：%s" % db.stats())

    if args.verify_vip:
        n = verify_vip(s, args.detail_limit)
        print("复核完成，恢复 %d 部" % n)
        print("最终库：%s" % db.stats())
        return 0

    if args.recheck_vip:
        n = recheck_vip(s, args.detail_limit)
        print("复检完成，剔除 %d 部" % n)
        print("最终库：%s" % db.stats())
        return 0

    if args.search:
        print("[注意] 站点搜索实测不可用（返回空结果），建议改用本地库搜索")
        n = crawl_search(s, args.search, args.pages or 1)
        print("搜索完成：%d 部" % n)
        print("最终库：%s" % db.stats())
        return 0

    if not args.detail_only:
        pages = args.pages or 0          # 0 = 每类全部页
        cats = [c.strip() for c in args.cats.split(",") if c.strip()] or None
        got, _ = crawl_list(s, pages, args.start, cats)
        print("目录抓取完成：%d 部" % got)

    if not args.skip_detail:
        n = crawl_details(s, args.detail_limit, check_vip=not args.no_vip_check)
        print("详情抓取完成：%d 部" % n)

    print("最终库：%s" % db.stats())


if __name__ == "__main__":
    sys.exit(main())
