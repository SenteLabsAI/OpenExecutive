import { redirect } from "next/navigation";

import { AS_YOU_HREF } from "@/lib/reach";

// Handle it for me has no switch of its own now: what goes on its own from
// your inbox is Approvals' call, under As you on the one Delegate page.
export default function HandleItPage() {
  redirect(AS_YOU_HREF);
}
