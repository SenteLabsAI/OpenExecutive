// Whether an image in the Executive's reply may load inline. A reply can quote
// mail or a web page, so markdown such as ![](https://elsewhere.example/x.png?d=…)
// would otherwise have the browser send that site whatever the URL carries, with
// no click. Only an image served by this app itself (a root-relative path)
// loads; any other is shown as a link the reader can choose to open.
export function loadsInline(src: unknown): boolean {
  if (typeof src !== "string") return false;
  // "//host/x" and "/\host/x" are protocol-relative: another site.
  return /^\/(?![/\\])/.test(src.trim());
}
