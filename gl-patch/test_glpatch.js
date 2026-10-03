// Node test harness for gl-patch v4 (runs the exact block shipped in libs.js)
// usage: node test_glpatch.js <libs.js> <tilesDir> <brokenTile> <outDir>
const fs = require("fs"), path = require("path"), zlib = require("zlib"), assert = require("assert");
const [libsPath, tilesDir, brokenTile, outDir] = process.argv.slice(2);
process.env.APPDATA = path.join(outDir, "appdata");
fs.mkdirSync(path.join(process.env.APPDATA, "aescripts", "GEOlayers3"), { recursive: true });
fs.mkdirSync(outDir, { recursive: true });

const src = fs.readFileSync(libsPath, "utf8");
const block = src.slice(0, src.indexOf("})();\n\n!function o(") + 5);
global.window = {};
eval(block);
const G = window.glSafeImageFile;
assert.strictEqual(G.version, 4);

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

// ---- 3. writeCanvasAtomic: retry, then own encoder --------------------------
function mockCanvas(w, h, uris) {
  const px = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < px.length; i += 4) { px[i] = (i >> 2) % 251; px[i + 1] = (i >> 9) % 253; px[i + 2] = 77; px[i + 3] = 255; }
  let n = 0;
  return { width: w, height: h, px, calls: () => n,
    getContext: () => ({ getImageData: () => ({ data: px }) }),
    toDataURL: () => uris[Math.min(n++, uris.length - 1)] };
}
const brokenUri = toUri(realBroken), goodUri = toUri(good);

(async () => {
  // a) first encode broken, second good -> retry wins
  let cv = mockCanvas(64, 64, [goodUri]);
  let target = path.join(outDir, "retry.png");
  await G.writeCanvasAtomic(cv, "image/png", undefined, brokenUri, target);
  ok("broken first encode -> re-encoded, good file written", fs.readFileSync(target).equals(good) && cv.calls() === 1);

  // b) both broken -> own encoder from pixels
  cv = mockCanvas(1024, 1024, [brokenUri]);
  target = path.join(outDir, "own.png");
  let t0 = Date.now();
  await G.writeCanvasAtomic(cv, "image/png", undefined, brokenUri, target);
  const own = fs.readFileSync(target);
  ok(`both encodes broken -> own encoder used (${Date.now() - t0} ms for 1024px), output valid`, G.checkBuffer(own) === null);
  fs.writeFileSync(path.join(outDir, "own_expected.rgba"), Buffer.from(cv.px.buffer));

  // c) 4096px own-encoder timing
  cv = mockCanvas(4096, 4096, [brokenUri]);
  target = path.join(outDir, "own4096.png");
  t0 = Date.now();
  await G.writeCanvasAtomic(cv, "image/png", undefined, brokenUri, target);
  ok(`4096px own encoder end-to-end ${Date.now() - t0} ms, valid`, G.checkBuffer(fs.readFileSync(target)) === null);

  // d) clean first encode -> written untouched, no retry
  cv = mockCanvas(64, 64, [brokenUri]);
  target = path.join(outDir, "first_ok.png");
  await G.writeCanvasAtomic(cv, "image/png", undefined, goodUri, target);
  ok("clean first encode written as-is (no extra encode)", fs.readFileSync(target).equals(good) && cv.calls() === 0);

  // e) 1 + 2 + 2 broken encodes in a), b), c)
  ok("stats counted", G.stats.toDataUrlBad === 5 && G.stats.retryOk === 1 && G.stats.ownEncoder === 2, JSON.stringify(G.stats));

  // f) cache check deletes a broken cached tile so GEOlayers re-renders it
  const cached = path.join(outDir, "cached_broken.png");
  fs.writeFileSync(cached, realBroken);
  ok("broken cached tile is removed and re-rendered", G.cachedFileIsValid(cached) === false && !fs.existsSync(cached));
  const cachedGood = path.join(outDir, "cached_good.png");
  fs.writeFileSync(cachedGood, good);
  ok("good cached tile is reused", G.cachedFileIsValid(cachedGood) === true);

  const log = fs.readFileSync(G.logPath(), "utf8");
  ok("log explains what happened", /BROKEN PNG from canvas.toDataURL/.test(log) && /own encoder used/.test(log) && /loaded v4/.test(log));
  console.log(`\n${pass} checks passed`);
})().catch((e) => { console.error("FAIL", e); process.exit(1); });
