import Link from "next/link";

import type { WaitingElsewhereItem } from "@/lib/api";
import { buttonClass } from "@/components/ui/Button";

// One Needs you row for something waiting on another page (GET /today
// `waiting_elsewhere`): what it is, and a button that opens that page,
// where it's answered.
export default function WaitingElsewhereRow({ item }: { item: WaitingElsewhereItem }) {
  return (
    <div className="flex items-center gap-3 rounded-2xl border border-line bg-surface-elevated px-4 py-3 sm:px-5">
      <div className="min-w-0 flex-1">
        <p className="text-[15px] font-semibold text-fg">{item.title}</p>
        {item.detail && <p className="truncate text-sm text-fg-muted">{item.detail}</p>}
      </div>
      <Link href={item.href} className={`${buttonClass("secondary", "md")} flex-shrink-0`}>
        {item.action}
      </Link>
    </div>
  );
}
