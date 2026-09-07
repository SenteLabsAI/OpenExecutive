import { signIn, auth, checkEmailAllowed, DEV_BYPASS_EMAIL } from "@/auth";
import { redirect } from "next/navigation";
import DevAutoSignIn from "@/components/DevAutoSignIn";

type SearchParams = Promise<{ callbackUrl?: string; error?: string }>;

// Only same-origin paths allowed — a leading `/` followed by anything other
// than another `/` (which would be protocol-relative, e.g. `//evil.com`).
function safeCallbackUrl(raw: string | undefined): string {
  if (!raw) return "/";
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) return "/";
  return raw;
}

export default async function SignInPage({ searchParams }: { searchParams: SearchParams }) {
  const { callbackUrl, error } = await searchParams;
  const safeDest = safeCallbackUrl(callbackUrl);

  // If already signed in, bounce straight to the destination — unless the
  // session's email has since left the allowlist, in which case the
  // middleware would send it straight back here. Show the denial instead.
  const session = await auth();
  let denied = false;
  if (session?.user?.email) {
    const { allowed, source } = await checkEmailAllowed(session.user.email.toLowerCase());
    if (allowed || source === "env_after_fetch_error") redirect(safeDest);
    denied = true;
  }

  const errorMessage = error ? describeError(error) : denied ? describeError("AccessDenied") : null;

  return (
    <main className="min-h-screen flex items-center justify-center px-6">
      <div className="w-full max-w-sm rounded-2xl border border-line bg-surface/60 p-8 shadow-xl">
        <h1 className="text-xl font-semibold tracking-tight text-fg">Open Executive</h1>
        <p className="mt-2 text-sm text-fg-muted">Sign in to continue.</p>

        {errorMessage && (
          <p className="mt-4 rounded-md border border-red-900/50 bg-red-950/40 px-3 py-2 text-sm text-red-200">
            {errorMessage}
          </p>
        )}

        {DEV_BYPASS_EMAIL && !errorMessage && (
          <DevAutoSignIn email={DEV_BYPASS_EMAIL} redirectTo={safeDest} />
        )}

        <form
          action={async () => {
            "use server";
            await signIn("google", { redirectTo: safeDest });
          }}
          className="mt-6"
        >
          <button
            type="submit"
            className="w-full rounded-md bg-white px-4 py-2 text-sm font-medium text-zinc-900 hover:bg-zinc-100 transition"
          >
            Sign in with Google
          </button>
        </form>
      </div>
    </main>
  );
}

function describeError(code: string): string {
  switch (code) {
    case "AccessDenied":
      return "Your Google account is not on the allow-list for this workspace. Ask an admin to add you.";
    case "Configuration":
      return "Authentication is misconfigured. Contact the administrator.";
    default:
      return "Sign-in failed. Try again, or contact the administrator if this keeps happening.";
  }
}
