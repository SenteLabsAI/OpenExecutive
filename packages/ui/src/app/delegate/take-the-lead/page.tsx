import { redirect } from "next/navigation";

import { AS_ME_HREF } from "@/lib/reach";

// Take the lead is the As me block of the one Delegate page now.
export default function TakeTheLeadPage() {
  redirect(AS_ME_HREF);
}
