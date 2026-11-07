/** One value of a page's query parameter. Next hands a page an array when a
 *  name repeats (`?callbackUrl=/a&callbackUrl=/b`), which a link scanner that
 *  decodes an address once too often produces; the first value wins. */
export function firstParam(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}
