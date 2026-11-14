#!/usr/bin/env node
// Builds the browser extension for your own Chrome Web Store listing (or to
// load unpacked) with your name, icons and version stamped in.
//
//   node build.mjs [--name "Acme Executive"] [--version 1.2.3]
//                  [--icons path/to/icons] [--out dist] [--zip]
//
// --icons is a folder holding icon16.png, icon32.png, icon48.png and
// icon128.png. The same options can come from EXTENSION_NAME,
// EXTENSION_VERSION and EXTENSION_ICONS. --zip also writes <out>.zip, ready
// to upload. Nothing outside --out is changed.
import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { deflateRawSync } from "node:zlib";

const HERE = dirname(fileURLToPath(import.meta.url));
const FILES = ["manifest.json", "site-rules.js", "shared.js", "content.js", "background.js", "popup.html", "popup.js"];
const ICONS = ["icon16.png", "icon32.png", "icon48.png", "icon128.png"];

/** The options from the command line, then the environment. */
export function options(argv, env) {
  const out = { name: env.EXTENSION_NAME || "", version: env.EXTENSION_VERSION || "", icons: env.EXTENSION_ICONS || "", out: "dist", zip: false };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--zip") out.zip = true;
    else if (["--name", "--version", "--icons", "--out"].includes(flag) && i + 1 < argv.length) out[flag.slice(2)] = argv[++i];
    else throw new Error(`Unknown option: ${flag}`);
  }
  if (out.version && !/^\d{1,5}(\.\d{1,5}){0,3}$/.test(out.version)) {
    throw new Error("--version is one to four numbers with dots, like 1.2.3");
  }
  if (out.name.length > 45) throw new Error("--name is at most 45 characters (the store's limit)");
  return out;
}

/** The manifest with the name and version stamped in. */
export function stamp(manifest, { name, version }) {
  const m = structuredClone(manifest);
  if (name) {
    m.name = name;
    m.short_name = name.slice(0, 12);
    m.action = { ...m.action, default_title: name };
  }
  if (version) m.version = version;
  return m;
}

const CRC = new Uint32Array(256).map((_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});

function crc32(buf) {
  let c = 0xffffffff;
  for (const b of buf) c = CRC[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** A zip of `entries` ([name, bytes]), deflated. */
export function zip(entries) {
  const parts = [];
  const central = [];
  let offset = 0;
  for (const [name, data] of entries) {
    const nameBuf = Buffer.from(name, "utf8");
    const packed = deflateRawSync(data);
    const crc = crc32(data);
    const head = Buffer.alloc(30);
    head.writeUInt32LE(0x04034b50, 0);
    head.writeUInt16LE(20, 4);
    head.writeUInt16LE(0x0800, 6);
    head.writeUInt16LE(8, 8);
    head.writeUInt32LE(crc, 14);
    head.writeUInt32LE(packed.length, 18);
    head.writeUInt32LE(data.length, 22);
    head.writeUInt16LE(nameBuf.length, 26);
    parts.push(head, nameBuf, packed);
    const dir = Buffer.alloc(46);
    dir.writeUInt32LE(0x02014b50, 0);
    dir.writeUInt16LE(20, 4);
    dir.writeUInt16LE(20, 6);
    dir.writeUInt16LE(0x0800, 8);
    dir.writeUInt16LE(8, 10);
    dir.writeUInt32LE(crc, 16);
    dir.writeUInt32LE(packed.length, 20);
    dir.writeUInt32LE(data.length, 24);
    dir.writeUInt16LE(nameBuf.length, 28);
    dir.writeUInt32LE(offset, 42);
    central.push(dir, nameBuf);
    offset += head.length + nameBuf.length + packed.length;
  }
  const dirBuf = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(dirBuf.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...parts, dirBuf, end]);
}

export function build(opts) {
  const out = resolve(opts.out);
  if (out === HERE || HERE.startsWith(out + "/")) throw new Error("--out must be a new folder, not the extension's own");
  const iconDir = opts.icons ? resolve(opts.icons) : join(HERE, "icons");
  for (const icon of ICONS) {
    if (!existsSync(join(iconDir, icon))) throw new Error(`Missing ${icon} in ${iconDir}`);
  }
  rmSync(out, { recursive: true, force: true });
  mkdirSync(join(out, "icons"), { recursive: true });
  const entries = [];
  for (const file of FILES) {
    let data = readFileSync(join(HERE, file));
    if (file === "manifest.json") data = Buffer.from(JSON.stringify(stamp(JSON.parse(data.toString("utf8")), opts), null, 2) + "\n");
    writeFileSync(join(out, file), data);
    entries.push([file, data]);
  }
  for (const icon of ICONS) {
    copyFileSync(join(iconDir, icon), join(out, "icons", icon));
    entries.push([`icons/${icon}`, readFileSync(join(iconDir, icon))]);
  }
  if (opts.zip) writeFileSync(`${out}.zip`, zip(entries));
  return { out, zip: opts.zip ? `${out}.zip` : null };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const done = build(options(process.argv.slice(2), process.env));
    console.log(`Built ${basename(done.out)}${done.zip ? ` and ${basename(done.zip)}` : ""}.`);
  } catch (err) {
    console.error(err instanceof Error ? err.message : String(err));
    process.exit(1);
  }
}
