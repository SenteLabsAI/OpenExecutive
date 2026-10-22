import { HOST_ORIGINS } from "@/lib/hostConfig";
import SignedIn from "./SignedIn";

// Where the popup sign-in ends (components/embed/EmbedSignIn.tsx). The
// middleware lets only a signed-in session reach it. It tells the window that
// opened it (a page of this app in a host's frame, or a host origin's own
// page) and closes.
export default function SignInDonePage() {
  return <SignedIn hostOrigins={[...HOST_ORIGINS.origins]} />;
}
