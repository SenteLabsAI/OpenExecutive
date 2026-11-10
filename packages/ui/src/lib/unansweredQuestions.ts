/** The parts of a listed question its "who asked" line is built from. */
export interface AskedFields {
  asked_by: string;
  place: string;
  times: number;
}

/** "Sam Okafor asked in chat · 2d ago · asked 3 times". `when` is the last
 * time it was asked, already formatted (empty leaves it out). */
export function askedLine(q: AskedFields, when: string): string {
  const parts = [`${q.asked_by} asked in ${q.place}`];
  if (when) parts.push(when);
  if (q.times > 1) parts.push(`asked ${q.times} times`);
  return parts.join(" · ");
}
