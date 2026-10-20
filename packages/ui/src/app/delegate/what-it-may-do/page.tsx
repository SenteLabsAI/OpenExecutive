import { redirect } from "next/navigation";

// What it may do is now What it does for you, beside each Delegate tab.
// Old links land on the first tab.
export default function WhatItMayDoPage() {
  redirect("/delegate/act-as-me");
}
