// 生成一个新鲜的取流地址（站点用 WASM 做时间签名，必须现场算）
import fs from "node:fs";
import path from "node:path";
import init, { build_play_url } from "./nbmovie_wasm.js";

const cfg = JSON.parse(fs.readFileSync("cfg.json", "utf8"));
await init({ module_or_path: fs.readFileSync("nbmovie_wasm_bg.wasm") });

const dataid = process.argv[2] || cfg.dataids[0];
const secret = process.argv[3] || cfg.slug;
const quality = process.argv[4] || "1080";
const playKey = process.argv[5] || cfg.myuserlink || "0";

const rel = build_play_url(dataid, secret, quality, playKey);
const full = rel.startsWith("http") ? rel : "https://4kvm.top" + rel;

const r = await fetch(full, {
  headers: {
    "User-Agent":
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    Referer: "https://4kvm.top/",
  },
});
const j = await r.json();
if (j.code !== 200 || !j.data) {
  console.error("取流失败:", JSON.stringify(j));
  process.exit(1);
}
const q = j.data.quality_urls || [];
console.log(JSON.stringify({ play_id: j.data.play_id, quality_urls: q }, null, 1));
if (q[0]) fs.writeFileSync(path.join("..", "poc", "last_url.txt"), q[0].url);
