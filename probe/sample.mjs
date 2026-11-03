// 采样 build_play_url，用于离线逆向签名算法
import fs from "node:fs";
import init, { build_play_url } from "./nbmovie_wasm.js";

await init({ module_or_path: fs.readFileSync("nbmovie_wasm_bg.wasm") });

const samples = [
  ["15478", "ch1bgub68", "1080", "X1VUXEFbVQYBBA4FCgc7IUlaXEhf"],
  ["15479", "ch1bgub68", "1080", "X1VUXEFbVQYBBA4FCgc7IUlaXEhf"],
  ["15478", "ch1bgub68", "720", "X1VUXEFbVQYBBA4FCgc7IUlaXEhf"],
  ["15478", "ch1bgub68", "1080", "AAAAAAAAAAAAAAAAAAAAAAAAAAAA"],
  ["999", "abcxyz", "480", "0"],
];

const out = [];
for (const [p, v, q, k] of samples) {
  const u = build_play_url(p, v, q, k);
  const qs = new URL("http://x" + u).searchParams;
  out.push({
    p, v, q, play_key: k,
    t: qs.get("t"),
    s: qs.get("s"),
    k_enc: qs.get("k"),
    url: u,
  });
  await new Promise((r) => setTimeout(r, 30));
}
fs.writeFileSync("samples.json", JSON.stringify(out, null, 1));
console.log(JSON.stringify(out, null, 1));
