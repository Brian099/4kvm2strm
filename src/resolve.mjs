// 调站点自带的 wasm 生成取流地址。
//
// 站点用 Rust/WASM 做时间签名（盐值 nbmovie2024secretkey），算法未公开，
// 这里直接复用它的 build_play_url，避免逆向维护成本。
//
// 用法：
//   node resolve.mjs <dataid> <secret> [quality] [play_key]
// 输出（stdout，JSON 一行）：
//   {"ok":true,"url":"https://oss.douyinbit.com/m3u8/xxx.m3u8","quality":1080}
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import init, { build_play_url } from "./wasm/nbmovie_wasm.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36";

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

const [, , dataid, secret, qualityArg, playKeyArg] = process.argv;
if (!dataid || !secret) {
  out({ ok: false, error: "需要 dataid 和 secret 参数" });
  process.exit(2);
}

// 站点把 4k 映射成 '1'，其余原样
const quality = qualityArg || "1080";
const q = quality === "4k" ? "1" : quality;
const playKey = playKeyArg || "";
if (!playKey) {
  out({ ok: false, error: "缺少 play_key（页面里的 userlink）" });
  process.exit(2);
}

try {
  const wasmPath = path.join(HERE, "wasm", "nbmovie_wasm_bg.wasm");
  await init({ module_or_path: fs.readFileSync(wasmPath) });

  const rel = build_play_url(String(dataid), String(secret), q, String(playKey));
  const full = rel.startsWith("http") ? rel : "https://4kvm.top" + rel;

  // 站点偶发连接重置/限流，重试几次
  let r = null, lastErr = "";
  for (let i = 0; i < 3; i++) {
    try {
      r = await fetch(full, {
        headers: { "User-Agent": UA, Referer: "https://4kvm.top/" },
      });
      if (r.ok) break;
      lastErr = `HTTP ${r.status}: ${(await r.text()).slice(0, 120)}`;
      if (r.status === 401 || r.status === 403) break;   // 鉴权问题，重试无用
    } catch (e) {
      lastErr = String((e && e.message) || e);
    }
    r = null;
    await new Promise((res) => setTimeout(res, 700 * (i + 1)));
  }
  if (!r || !r.ok) {
    out({ ok: false, error: lastErr || "取流接口无响应" });
    process.exit(1);
  }
  const j = await r.json();
  if (j.code !== 200 || !j.data) {
    out({ ok: false, error: `取流失败: ${j.message || j.code}` });
    process.exit(1);
  }
  // 站点会把不可用的清晰度返回成占位值（比如 4K 的 url 是 "1"），过滤掉
  const list = (j.data.quality_urls || []).filter((x) => x && /^https?:/i.test(x.url || ""));
  const pick = list.find((x) => String(x.bitrate) === String(quality)) || list[0];
  if (!pick || !pick.url) {
    out({ ok: false, error: "没有可用的清晰度" });
    process.exit(1);
  }
  out({
    ok: true,
    url: pick.url,
    quality: pick.bitrate,
    title: pick.title,
    locked: !!pick.locked,
    isvip: !!pick.isvip,
    all: list.map((x) => ({ q: x.bitrate, title: x.title, url: x.url })),
  });
} catch (e) {
  out({ ok: false, error: String((e && e.message) || e) });
  process.exit(1);
}
