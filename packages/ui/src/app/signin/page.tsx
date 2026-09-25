import { AuthError } from "next-auth";
import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { LOCAL_OWNER_MODE, auth, sessionStillAllowed, signIn } from "@/auth";
import { LOCAL_OWNER_PROVIDER_ID } from "@/lib/localOwner";

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

  // If already signed in, bounce straight to the destination — but only with a
  // session the middleware still accepts. A revoked one would be sent straight
  // back here, forever.
  const session = await auth();
  if (session && (await sessionStillAllowed(session, (await headers()).get("host")))) {
    redirect(safeDest);
  }

  const errorMessage = error ? describeError(error) : null;
  const googleConfigured = Boolean(process.env.AUTH_GOOGLE_ID?.trim());

  return (
    <main className="min-h-screen flex items-center justify-center px-6">
      <div className="w-full max-w-sm rounded-2xl border border-line bg-surface/60 p-8 shadow-xl">
        <h1 className="text-xl font-semibold tracking-tight text-fg">Open Executive</h1>
        <p className="mt-2 text-sm text-fg-muted">
          {LOCAL_OWNER_MODE
            ? "This copy runs on your computer, and only you can reach it — so there’s no sign-in."
            : "Sign in to continue."}
        </p>

        {errorMessage && (
          <p className="mt-4 rounded-md border border-red-900/50 bg-red-950/40 px-3 py-2 text-sm text-red-200">
            {errorMessage}
          </p>
        )}

        {LOCAL_OWNER_MODE ? (
          <>
            <form
              action={async () => {
                "use server";
                try {
                  await signIn(LOCAL_OWNER_PROVIDER_ID, { redirectTo: safeDest });
                } catch (err) {
                  // A refused sign-in throws; success throws Next's redirect, which must pass through.
                  if (err instanceof AuthError) {
                    redirect(
                      `/signin?error=${encodeURIComponent(err.type)}&callbackUrl=${encodeURIComponent(safeDest)}`,
                    );
                  }
                  throw err;
                }
              }}
              className="mt-6"
            >
              <button
                type="submit"
                className="w-full rounded-md bg-white px-4 py-2 text-sm font-medium text-zinc-900 hover:bg-zinc-100 transition"
              >
                Open
              </button>
            </form>
            <p className="mt-4 text-xs text-fg-subtle">
              To invite your team, or to run it on a server, set up Google sign-in (see docs/auth.md).
            </p>
          </>
        ) : (
          <>
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
            {!googleConfigured && (
              <p className="mt-4 text-xs text-fg-subtle">
                Google sign-in isn’t set up here yet (see docs/auth.md). On your own computer,
                start Open Executive with <code>make dev</code> to use it without signing in.
              </p>
            )}
          </>
        )}
      </div>
    </main>
  );
}

function describeError(code: string): string {
  switch (code) {
    case "AccessDenied":
      return "Your Google account is not on the allow-list for this workspace. Ask an admin to add you.";
    case "CredentialsSignin":
      return "Open only works in a browser on the computer running Open Executive, at http://localhost:3000.";
    case "Configuration":
      return "Authentication is misconfigured. Contact the administrator.";
    default:
      return "Sign-in failed. Try again, or contact the administrator if this keeps happening.";
  }
}
