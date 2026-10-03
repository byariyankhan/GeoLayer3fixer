// Node test harness for gl-patch v4 (runs the exact block shipped in libs.js)
// usage: node test_glpatch.js <libs.js> <tilesDir> <brokenTile> <outDir>
const fs = require("fs"), path = require("path"), zlib = require("zlib"), assert = require("assert");
const [libsPath, tilesDir, brokenTile, outDir] = process.argv.slice(2);
process.env.APPDATA = path.join(outDir, "appdata");
fs.mkdirSync(path.join(process.env.APPDATA, "aescripts", "GEOlayers3"), { recursive: true });
fs.mkdirSync(outDir, { recursive: true });

// --- browser mocks: canvases are HTMLCanvasElement instances, like in CEP ---
global.HTMLCanvasElement = function () {};
HTMLCanvasElement.prototype.toDataURL = function () { return this._uris[Math.min(this._n++, this._uris.length - 1)]; };
HTMLCanvasElement.prototype.getContext = function (t) {
  if (this._kind === "webgl") return t === "2d" ? null : {};
  const self = this;
  return { getImageData: () => ({ data: self._px }), drawImage: (src) => { self._px = src._px; } };
};
function makeCanvas(w, h, uris, kind) {
  const c = Object.create(HTMLCanvasElement.prototype);
  Object.assign(c, { width: w, height: h, _uris: uris, _n: 0, _kind: kind || "2d" });
  c._px = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < c._px.length; i += 4) { c._px[i] = (i >> 2) % 251; c._px[i + 1] = (i >> 9) % 253; c._px[i + 2] = 77; c._px[i + 3] = 255; }
  return c;
}
global.document = { createElement: () => makeCanvas(0, 0, [], "2d") };
global.window = {};
const src = fs.readFileSync(libsPath, "utf8");
const block = src.indexOf("})();\n\n!function o(") > 0 ? src.slice(0, src.indexOf("})();\n\n!function o(") + 5) : src;
eval(block);
const G = window.glSafeImageFile;
assert.strictEqual(G.version, 4.1);

// ---- helpers --------------------------------------------------------------
function crc32(b) { let c = -1; for (const x of b) { c ^= x; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; } return (c ^ -1) >>> 0; }
function chunks(png) { const out = []; let o = 8; while (o < png.length) { const len = png.readUInt32BE(o); out.push({ o, len, type: png.toString("latin1", o + 4, o + 8) }); o += 12 + len; } return out; }
function setCrc(png, c) { png.writeUInt32BE(crc32(png.subarray(c.o + 4, c.o + 8 + c.len)), c.o + 8 + c.len); }
const toUri = (b) => "data:image/png;base64," + b.toString("base64");
let pass = 0;
function ok(name, cond, extra) { assert.ok(cond, name + (extra ? " :: " + extra : "")); pass++; console.log("  ok -", name); }

// ---- 1. real clean tiles pass ---------------------------------------------
const tiles = fs.readdirSync(tilesDir).filter((f) => f.endsWith(".png")).map((f) => path.join(tilesDir, f));
for (const t of tiles) {
  const b = fs.readFileSync(t), t0 = Date.now(), err = G.checkBuffer(b);
  ok(`clean tile passes: ${path.basename(t)} (${(b.length / 1e6).toFixed(1)} MB, ${Date.now() - t0} ms)`, err === null, err);
}

// ---- 2. corruptions are rejected ------------------------------------------
const good = fs.readFileSync(tiles.find((t) => t.includes("_512_")));
const realBroken = fs.readFileSync(brokenTile);
ok("REAL broken tile from AE error (valid CRCs, bad data) rejected: " + G.checkBuffer(realBroken), G.checkBuffer(realBroken) !== null);

{ // the 137x gl-patch pattern: IDAT length zeroed at a chunk boundary
  const b = Buffer.from(good), c = chunks(b).filter((x) => x.type === "IDAT")[3];
  b.writeUInt32BE(0, c.o);
  ok("zero-length IDAT header rejected: " + G.checkBuffer(b), /CRC|truncated|incomplete/.test(G.checkBuffer(b) || ""));
}
{ // bad filter byte with VALID CRC + VALID zlib (what v3 let through)
  const ihdr = good.subarray(16, 29), w = ihdr.readUInt32BE(0), h = ihdr.readUInt32BE(4);
  const idat = Buffer.concat(chunks(good).filter((x) => x.type === "IDAT").map((x) => good.subarray(x.o + 8, x.o + 8 + x.len)));
  const raw = zlib.inflateSync(idat), rl = raw.length / h; raw[7 * rl] = 0xc8;
  const z = zlib.deflateSync(raw);
  const mk = (t, d) => { const hd = Buffer.alloc(8); hd.writeUInt32BE(d.length); hd.write(t, 4, "latin1"); const o = Buffer.concat([hd, d, Buffer.alloc(4)]); o.writeUInt32BE(crc32(o.subarray(4, 8 + d.length)), 8 + d.length); return o; };
  const b = Buffer.concat([good.subarray(0, 8), mk("IHDR", Buffer.from(ihdr)), mk("IDAT", z), mk("IEND", Buffer.alloc(0))]);
  ok("bad filter byte with valid CRC+zlib rejected: " + G.checkBuffer(b), /filter/.test(G.checkBuffer(b) || ""));
  // extra data after stream
  const b2 = Buffer.concat([good.subarray(0, 8), mk("IHDR", Buffer.from(ihdr)), mk("IDAT", Buffer.concat([zlib.deflateSync(zlib.inflateSync(idat)), Buffer.from("JUNK")])), mk("IEND", Buffer.alloc(0))]);
  ok("extra data after stream rejected", /extra data/.test(G.checkBuffer(b2) || ""));
}
{ // flipped byte inside compressed data, CRC recomputed (encoder-level corruption)
  const b = Buffer.from(good), c = chunks(b).filter((x) => x.type === "IDAT")[10];
  b[c.o + 8 + 100] ^= 0x5a; setCrc(b, c);
  ok("corrupt deflate with valid CRC rejected: " + G.checkBuffer(b), G.checkBuffer(b) !== null);
}
ok("truncated file rejected", G.checkBuffer(good.subarray(0, good.length >> 1)) !== null);

// ---- 3. canvas.toDataURL guard (covers every GEOlayers tile path) -----------
const brokenUri = toUri(realBroken), goodUri = toUri(good);
const decode = (u) => Buffer.from(u.slice(u.indexOf(",") + 1), "base64");

(async () => {
  // a) clean PNG passes through untouched
  let cv = makeCanvas(64, 64, [goodUri]);
  ok("guard: clean PNG returned unchanged", cv.toDataURL("image/png") === goodUri);
  ok("guard: default type (png) handled", makeCanvas(64, 64, [goodUri]).toDataURL() === goodUri);

  // b) broken PNG from a 2D canvas (tile merger d()) -> replaced by own encoder, pixel-exact
  cv = makeCanvas(1024, 1024, [brokenUri]);
  let t0 = Date.now();
  let out = cv.toDataURL("image/png");
  let png = decode(out);
  ok(`guard: broken PNG replaced (1024px, ${Date.now() - t0} ms), result valid`, out !== brokenUri && G.checkBuffer(png) === null);
  fs.writeFileSync(path.join(outDir, "own.png"), png);
  fs.writeFileSync(path.join(outDir, "own_expected.rgba"), Buffer.from(cv._px.buffer));

  // c) broken PNG from a WebGL canvas (GL renderer m()) -> pixels read through a 2D copy
  cv = makeCanvas(512, 512, [brokenUri], "webgl");
  out = cv.toDataURL("image/png");
  ok("guard: WebGL canvas handled via 2D copy", out !== brokenUri && G.checkBuffer(decode(out)) === null);
  fs.writeFileSync(path.join(outDir, "own_webgl.png"), decode(out));
  fs.writeFileSync(path.join(outDir, "own_webgl_expected.rgba"), Buffer.from(cv._px.buffer));

  // d) 4096px timing (worst case, blocks the panel this long only when Chromium failed)
  cv = makeCanvas(4096, 4096, [brokenUri]);
  t0 = Date.now();
  out = cv.toDataURL("image/png");
  ok(`guard: 4096px replacement ${Date.now() - t0} ms, valid`, G.checkBuffer(decode(out)) === null);

  // e) JPEG untouched
  cv = makeCanvas(64, 64, ["data:image/jpeg;base64,/9j/AAAA"]);
  ok("guard: JPEG untouched", cv.toDataURL("image/jpeg", 0.9) === "data:image/jpeg;base64,/9j/AAAA");

  // f) end-to-end through GEOlayers' tile-merger route: d() -> S() = writeDataUriAtomic
  cv = makeCanvas(256, 256, [brokenUri]);
  const target = path.join(outDir, "merger_tile.png");
  await G.writeDataUriAtomic(cv.toDataURL("image/png"), target);
  ok("tile-merger route: broken encode still ends as a valid file on disk", G.checkBuffer(fs.readFileSync(target)) === null);

  // g) zoom-level merge route (main.js -> writeCanvasAtomic)
  cv = makeCanvas(128, 128, [brokenUri]);
  const t2 = path.join(outDir, "merge_tile.png");
  await G.writeCanvasAtomic(cv, "image/png", undefined, cv.toDataURL("image/png"), t2);
  ok("zoom-merge route: valid file on disk", G.checkBuffer(fs.readFileSync(t2)) === null);

  ok("stats counted", G.stats.toDataUrlBad === 5 && G.stats.ownEncoder === 5, JSON.stringify(G.stats));

  // f) cache check deletes a broken cached tile so GEOlayers re-renders it
  const cached = path.join(outDir, "cached_broken.png");
  fs.writeFileSync(cached, realBroken);
  ok("broken cached tile is removed and re-rendered", G.cachedFileIsValid(cached) === false && !fs.existsSync(cached));
  const cachedGood = path.join(outDir, "cached_good.png");
  fs.writeFileSync(cachedGood, good);
  ok("good cached tile is reused", G.cachedFileIsValid(cachedGood) === true);

  const log = fs.readFileSync(G.logPath(), "utf8");
  ok("log explains what happened", /returned a broken PNG/.test(log) && /replaced by own encoder/.test(log) && /loaded v4.1 .*canvas guard on/.test(log));
  console.log(`\n${pass} checks passed`);
})().catch((e) => { console.error("FAIL", e); process.exit(1); });
