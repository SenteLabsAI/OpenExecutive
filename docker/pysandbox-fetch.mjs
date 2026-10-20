// Build step for the Python job sandbox (workflows/python_job.py): download
// the compiled Pyodide libraries the jobs may import, plus everything they
// depend on, into the Pyodide folder, each checked against the sha256 in
// Pyodide's own pyodide-lock.json. At run time the sandbox has no network,
// so what is fetched here is all a job can use.
//
//   node pysandbox-fetch.mjs <pyodide dir> <cdn base url> <package> [...]
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";

const [dir, base, ...wanted] = process.argv.slice(2);
const lock = JSON.parse(readFileSync(`${dir}/pyodide-lock.json`, "utf8")).packages;
const need = new Set();
const add = (name) => {
  const key = name.toLowerCase();
  if (need.has(key)) return;
  if (!lock[key]) throw new Error(`${key} is not in pyodide-lock.json`);
  need.add(key);
  for (const dep of lock[key].depends || []) add(dep);
};
wanted.forEach(add);
for (const key of [...need].sort()) {
  const { file_name: file, sha256 } = lock[key];
  const res = await fetch(`${base}/${file}`);
  if (!res.ok) throw new Error(`${file}: HTTP ${res.status}`);
  const bytes = Buffer.from(await res.arrayBuffer());
  const got = createHash("sha256").update(bytes).digest("hex");
  if (got !== sha256) throw new Error(`${file}: sha256 ${got} != ${sha256}`);
  writeFileSync(`${dir}/${file}`, bytes);
  console.log(`fetched ${file}`);
}
