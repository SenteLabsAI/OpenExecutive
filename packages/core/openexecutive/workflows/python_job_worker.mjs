// One Python job, run by workflows/python_job.py as
//   deno run --no-prompt --quiet --allow-read=<sandbox dir> python_job_worker.mjs
// Deno grants read access to the sandbox folder (Pyodide and the bundled
// libraries) and nothing else: no network, environment, writes or processes.
// Pyodide alone is not a sandbox (its Python can call into the JavaScript
// runtime), so those Deno limits are what keep a job away from the server.
//
// stdin:  {"code": str, "files": {name: base64}, "inputs": {...}, "pyodide": dir,
//          "wheels": [path, ...], "max_files", "max_file_bytes", "max_total_bytes"}
// stdout: one line of JSON: {"result", "error", "printed", "files": {name: base64},
//          "skipped", "ms": {...}, "rss_mb"}
const MAX_PRINTED = 8000;
const t0 = performance.now();
// The job is one line of JSON. stdin then stays open until the API has
// measured this process (peak memory, CPU) and stops it.
const stdin = Deno.stdin.readable.getReader();
const chunks = [];
for (;;) {
  const { value, done } = await stdin.read();
  if (done) break;
  const end = value.indexOf(10);
  if (end >= 0) {
    chunks.push(value.subarray(0, end));
    break;
  }
  chunks.push(value);
}
const job = JSON.parse(new TextDecoder().decode(await new Blob(chunks).arrayBuffer()));
const { loadPyodide } = await import("file://" + job.pyodide + "/pyodide.mjs");
let printed = "";
const keep = (line) => {
  if (printed.length < MAX_PRINTED) printed += line + "\n";
};
// indexURL explicitly: Pyodide otherwise guesses its folder from a stack
// trace, which Deno's source maps point elsewhere.
const py = await loadPyodide({ indexURL: job.pyodide + "/", stdout: keep, stderr: keep });
const tLoad = performance.now();
// The bundled pure-Python libraries, then any compiled ones the code imports
// (numpy, pandas, matplotlib, pillow, lxml), all from the local folder.
// Pyodide doesn't know the bundled libraries' own dependencies, so NEEDS
// names the Pyodide packages each one imports.
const NEEDS = {
  docx: ["lxml", "typing-extensions"],
  pptx: ["lxml", "pillow", "typing-extensions"],
  fpdf: ["pillow", "fonttools"],
};
const quiet = { messageCallback: () => {} };
await py.loadPackage(job.wheels.map((p) => "file://" + p), quiet);
const imports = py.pyodide_py.code.find_imports(job.code).toJs();
const needed = [...new Set(imports.flatMap((name) => NEEDS[name] || []))];
if (needed.length) await py.loadPackage(needed, quiet);
await py.loadPackagesFromImports(job.code, quiet);
const tPkgs = performance.now();
// A saved tool's per-run values, read by the code as the dict `inputs`.
py.globals.set("inputs", py.toPy(job.inputs || {}));
py.FS.mkdirTree("/in");
py.FS.mkdirTree("/out");
for (const [name, b64] of Object.entries(job.files || {})) {
  py.FS.writeFile(`/in/${name}`, Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)));
}
let result = null;
let error = null;
try {
  const value = await py.runPythonAsync(job.code);
  result = value === undefined || value === null ? null : String(value);
} catch (e) {
  error = String(e.message).split("\n").filter(Boolean).slice(-6).join("\n");
}
// Result files within the caps the API set; anything past them is named in
// `skipped` and not read, so a job can't flood the API with output.
const files = {};
const skipped = [];
let total = 0;
for (const name of py.FS.readdir("/out")) {
  if (name === "." || name === "..") continue;
  const stat = py.FS.stat(`/out/${name}`);
  if (!py.FS.isFile(stat.mode)) continue;
  if (Object.keys(files).length >= job.max_files || stat.size > job.max_file_bytes
      || total + stat.size > job.max_total_bytes) {
    skipped.push(name);
    continue;
  }
  total += stat.size;
  const bytes = py.FS.readFile(`/out/${name}`);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  files[name] = btoa(bin);
}
const t1 = performance.now();
console.log(JSON.stringify({
  result,
  error,
  printed: printed.slice(0, MAX_PRINTED),
  files,
  skipped,
  ms: { load: Math.round(tLoad - t0), packages: Math.round(tPkgs - tLoad), job: Math.round(t1 - tPkgs) },
  // WebAssembly memory never shrinks, so this is close to the job's peak.
  rss_mb: Math.round(Deno.memoryUsage().rss / 2 ** 20),
}));
// Wait for the API to stop this process (it reads the line above, measures,
// then kills it), or exit once it closes stdin.
await stdin.read().catch(() => {});
Deno.exit(0);
