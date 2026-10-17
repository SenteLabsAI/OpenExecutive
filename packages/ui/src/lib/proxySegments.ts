// How the backend proxy (app/api/backend/[...path]) forwards each path
// segment. Kept apart from the route so the rule can be tested.

/** The segment to send upstream. Next.js hands the segments decoded, so an id
 *  the caller sent with `%2F` (a Google Chat chat, `google_chat:spaces/…`)
 *  arrives holding a slash and, joined back, would become several segments
 *  that route nowhere. It goes on as one opaque segment instead: `~` and its
 *  base64url, which the API reads back (`api/path_ids.py`). */
export function upstreamSegment(segment: string): string {
  if (segment.includes("/")) {
    return "~" + Buffer.from(segment, "utf8").toString("base64url");
  }
  return segment;
}
