/** Whether a key event belongs to an IME composition still in progress
 *  (Korean, Japanese, Chinese input). An Enter that confirms the composed
 *  text must not also submit: the box would be cleared and sent, then the
 *  IME would commit the last character back into the empty box, which goes
 *  out again as its own message. Safari reports some of these keydowns
 *  with `isComposing` false but `keyCode` 229, so both are checked. */
export function isImeComposing(e: {
  nativeEvent?: { isComposing?: boolean };
  keyCode?: number;
}): boolean {
  return e.nativeEvent?.isComposing === true || e.keyCode === 229;
}
