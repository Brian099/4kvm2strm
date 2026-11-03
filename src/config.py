# -*- coding: utf-8 -*-
"""4kvm 原型系统配置。"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- 站点 ----------------
HOST = os.environ.get("VM_HOST", "https://4kvm.top").rstrip("/")
UA = os.environ.get(
    "VM_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
)

# 抓取限速（站点有限流，别太快）
LIST_SLEEP = float(os.environ.get("VM_LIST_SLEEP", "1.0"))
DETAIL_SLEEP = float(os.environ.get("VM_DETAIL_SLEEP", "0.8"))
PAGE_SIZE = 30          # /filter 每页条数（站点固定 30）

# ---------------- 数据 ----------------
DB_PATH = os.environ.get("VM_DB", os.path.join(BASE_DIR, "data", "4kvm.db"))
STRM_ROOT = os.environ.get("VM_STRM", os.path.join(BASE_DIR, "strm"))

# ---------------- 服务 ----------------
LISTEN_HOST = os.environ.get("VM_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("VM_LISTEN_PORT", "8900"))
PUBLIC_BASE = os.environ.get("VM_PUBLIC_BASE", "http://127.0.0.1:8900").rstrip("/")

# 默认清晰度：4k / 1080 / 720 / 480（站点内部把 4k 映射成 '1'）
DEFAULT_QUALITY = os.environ.get("VM_QUALITY", "1080")

# 分片模式：
#   direct = 播放列表里的分片地址直接指向 CDN，客户端自己去拉（服务器零视频带宽）★实测可行
#   proxy  = 分片经本服务转发（可剥 73 字节假 PNG 头 + 磁盘缓存，兼容性最好）
SEG_MODE = os.environ.get("VM_SEG_MODE", "direct").strip().lower()
if SEG_MODE not in ("direct", "proxy"):
    SEG_MODE = "direct"

# 分片磁盘缓存（仅 proxy 模式生效；分片是公开静态资源，可以放心缓存）
SEG_CACHE = int(os.environ.get("VM_SEG_CACHE", "1"))
SEG_CACHE_DIR = os.environ.get("VM_SEG_CACHE_DIR", os.path.join(BASE_DIR, "data", "segcache"))
SEG_CACHE_MAX_MB = int(os.environ.get("VM_SEG_CACHE_MAX_MB", "2048"))

# ---------------- 自动更新 ----------------
# 启动后是否立刻跑一次「抓目录 -> 补详情 -> 导出 STRM」
BOOTSTRAP = int(os.environ.get("VM_BOOTSTRAP", "1"))
# 定时更新间隔（小时），0=关闭定时（只在启动时跑一次）
INTERVAL_HOURS = float(os.environ.get("VM_INTERVAL_HOURS", "24"))
# 自动更新时是否同时导出 STRM
AUTO_EXPORT = int(os.environ.get("VM_AUTO_EXPORT", "1"))
# 自动导出是否带 NFO / 海报
AUTO_NFO = int(os.environ.get("VM_AUTO_NFO", "1"))
AUTO_IMAGES = int(os.environ.get("VM_AUTO_IMAGES", "1"))

# 海报下载并发
IMAGE_WORKERS = int(os.environ.get("VM_IMAGE_WORKERS", "8"))

# 苹果CMS 兼容接口密钥：留空=公开
API_KEY = os.environ.get("VM_API_KEY", "").strip()
# 接口里 vod_play_from 的线路名
API_PLAY_FROM = os.environ.get("VM_API_PLAY_FROM", "4kvm").strip()

# WSGI 线程数（direct 模式下不占线程，proxy 模式每路播放占一个）
THREADS = int(os.environ.get("VM_THREADS", "16"))

# 取流地址缓存秒数（站点 m3u8 token 实测十几分钟过期，这里保守些）
RESOLVE_TTL = int(os.environ.get("VM_RESOLVE_TTL", "300"))

# Node 可执行文件（用来跑站点自带的 wasm）
NODE_BIN = os.environ.get("VM_NODE", "node")
