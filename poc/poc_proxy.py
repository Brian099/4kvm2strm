# -*- coding: utf-8 -*-
"""可行性验证：把 4kvm 的 m3u8 转成 ffmpeg/Emby 能吃的形式。

两个必须处理的问题：
  1. 分片 URL 没有 .ts 后缀  -> ffmpeg 的 HLS 解复用器直接拒绝
  2. 每个分片开头有 73 字节的假 PNG 头，后面才是真正的 MPEG-TS
本脚本在本地起一个代理，把这两件事修掉，然后用 ffprobe 验证。

用法：
    python poc_proxy.py "https://oss.douyinbit.com/m3u8/<token>.m3u8"
然后另开窗口：
    ffprobe http://127.0.0.1:8899/v/index.m3u8
"""
import base64
import io
import sys
import threading

import requests
import urllib3
from flask import Flask, Response, request

urllib3.disable_warnings()

app = Flask(__name__)
SOURCE = {"url": ""}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


def b64e(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


def ts_offset(buf):
    """找到真正的 MPEG-TS 起点（连续 4 个 188 字节同步字节）。"""
    limit = min(len(buf) - 188 * 4, 4096)
    for i in range(max(0, limit)):
        if (buf[i] == 0x47 and buf[i + 188] == 0x47
                and buf[i + 376] == 0x47 and buf[i + 564] == 0x47):
            return i
    return 0


@app.route("/v/<name>.m3u8")
def playlist(name):
    src = SOURCE["url"]
    r = requests.get(src, timeout=30, verify=False,
                     headers={"User-Agent": UA, "Referer": "https://4kvm.top/"})
    if r.status_code != 200:
        return Response("上游 %s: %s" % (r.status_code, r.text[:100]), status=502)

    base = src.rsplit("/", 1)[0] + "/"
    out = []
    dropped = 0
    for line in r.text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            out.append(s)
            continue
        if s.startswith("/ets/"):
            # 站点的诱饵分片，实测 404，直接丢掉
            dropped += 1
            continue
        full = s if s.startswith("http") else base + s
        out.append("/seg/%s.ts" % b64e(full))
    app.logger.info("播放列表：保留 %d 段，丢弃诱饵 %d 段", len(out), dropped)
    resp = Response("\n".join(out) + "\n", mimetype="application/vnd.apple.mpegurl")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/seg/<token>.ts")
def segment(token):
    url = b64d(token)
    r = requests.get(url, timeout=60, verify=False,
                     headers={"User-Agent": UA}, stream=False)
    if r.status_code != 200:
        return Response("上游 %s" % r.status_code, status=502)
    body = r.content
    off = ts_offset(body)
    payload = body[off:] if off else body
    resp = Response(payload, mimetype="video/mp2t")
    resp.headers["X-Skipped-Bytes"] = str(off)
    return resp


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    SOURCE["url"] = sys.argv[1]
    print("源播放列表:", SOURCE["url"])
    print("本地地址:  http://127.0.0.1:8899/v/index.m3u8")
    app.run(host="127.0.0.1", port=8899, threaded=True)


if __name__ == "__main__":
    main()
