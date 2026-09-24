import { redirect } from "next/navigation";

// The review queue lives inside the Knowledge base now. Keep the old route so
// bookmarks and links still land on it.
export default function ReviewPage() {
  redirect("/knowledge?view=review");
}
