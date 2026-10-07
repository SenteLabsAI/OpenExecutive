// One Python job, run by workflows/python_job.py as
//   deno run --no-prompt --quiet --allow-read=<sandbox dir> python_job_worker.mjs
// Deno grants read access to the sandbox folder (Pyodide and the bundled
// libraries) and nothing else: no network, environment, writes or processes.
// Pyodide alone is not a sandbox (its Python can call into the JavaScript
// runtime), so those Deno limits are what keep a job away from the server.
//
// stdin:  {"code": str, "files": {name: base64}, "pyodide": dir, "wheels": [path, ...]}
// stdout: {"result", "error", "printed", "files": {name: base64}, "ms": {...}}
const MAX_PRINTED = 8000;
const t0 = performance.now();
const job = JSON.parse(
  new TextDecoder().decode(await new Response(Deno.stdin.readable).arrayBuffer()),
);
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
await py.loadPackage(job.wheels.map((p) => "file://" + p), { messageCallback: () => {} });
await py.loadPackagesFromImports(job.code, { messageCallback: () => {} });
const tPkgs = performance.now();
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
}));
