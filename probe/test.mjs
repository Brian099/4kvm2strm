// 用站点自带的 WASM 构造取流地址，验证可行性
import fs from "node:fs";
import init, { build_play_url } from "./nbmovie_wasm.js";

const cfg = JSON.parse(fs.readFileSync("cfg.json", "utf8"));
const wasmBuf = fs.readFileSync("nbmovie_wasm_bg.wasm");

await init({ module_or_path: wasmBuf });

const dataid = cfg.dataids[0];
const secret = cfg.slug;
const quality = "1080"; // 站点映射：'4k'->'1'，其余原样
const playKey = cfg.myuserlink || "0";

const url = build_play_url(dataid, secret, quality, playKey);
console.log("build_play_url ->", url);

const full = url.startsWith("http") ? url : "https://4kvm.top" + url;
const r = await fetch(full, {
  headers: {
    "User-Agent":
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    Referer: "https://4kvm.top/",
  },
});
console.log("HTTP", r.status, r.headers.get("content-type"));
const text = await r.text();
console.log("body:", text.slice(0, 1200));
