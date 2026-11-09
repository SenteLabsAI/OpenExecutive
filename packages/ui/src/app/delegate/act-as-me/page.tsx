import { redirect } from "next/navigation";

import { AS_YOU_HREF } from "@/lib/reach";

// Act as me is the As you block of the one Delegate page now.
export default function ActAsMePage() {
  redirect(AS_YOU_HREF);
}
