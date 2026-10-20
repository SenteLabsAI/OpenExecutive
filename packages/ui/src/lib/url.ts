// Hostname of a URL for display ("Open in app (docs.google.com)"); the raw
// string when it doesn't parse.
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}
