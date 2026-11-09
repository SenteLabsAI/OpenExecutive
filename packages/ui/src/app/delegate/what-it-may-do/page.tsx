import { redirect } from "next/navigation";

// What it may do is now What it does for you, beside the Delegate page.
export default function WhatItMayDoPage() {
  redirect("/delegate");
}
