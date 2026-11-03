// 常驻探测进程：从 stdin 逐行读 {id,secret,pk,q}，输出 {id,code,free,msg}
// 用来在补详情时判断「这部片有没有免费可播源」。
// 常驻是为了摊掉 Node 启动开销（每部启一次 Node 大约 300ms，5000 部就是 25 分钟）。
import fs from "node:fs";
import readline from "node:readline";
import init, { build_play_url } from "./wasm/nbmovie_wasm.js";

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36";

await init({ module_or_path: fs.readFileSync("./wasm/nbmovie_wasm_bg.wasm") });
process.stdout.write(JSON.stringify({ ready: true }) + "\n");

const rl = readline.createInterface({ input: process.stdin });

for await (const line of rl) {
  const s = line.trim();
  if (!s) continue;
  let job;
  try {
    job = JSON.parse(s);
  } catch (e) {
    process.stdout.write(JSON.stringify({ id: null, code: -2, msg: "bad json" }) + "\n");
    continue;
  }
  const q = String(job.q || "1080");
  try {
    const rel = build_play_url(String(job.id), String(job.secret), q, String(job.pk || "0"));
    const r = await fetch("https://4kvm.top" + rel, {
      headers: { "User-Agent": UA, Referer: "https://4kvm.top/" },
    });
    const text = await r.text();
    let j;
    try {
      j = JSON.parse(text);
    } catch (e) {
      process.stdout.write(
        JSON.stringify({ id: job.id, code: -3, msg: text.slice(0, 80) }) + "\n"
      );
      continue;
    }
    const urls = (j.data && j.data.quality_urls) || [];
    // 只算「没锁 + 有真实 http 地址」的清晰度
    const free = urls.filter((u) => u && !u.locked && !u.isvip && /^https?:/i.test(u.url || ""));
    process.stdout.write(
      JSON.stringify({
        id: job.id,
        code: j.code,
        msg: j.message || "",
        total: urls.length,
        free: free.length,
        freeTitles: free.map((u) => u.title),
        has4k: urls.some((u) => String(u.bitrate) === "2160000"),
      }) + "\n"
    );
  } catch (e) {
    process.stdout.write(
      JSON.stringify({ id: job.id, code: -1, msg: String((e && e.message) || e) }) + "\n"
    );
  }
}
