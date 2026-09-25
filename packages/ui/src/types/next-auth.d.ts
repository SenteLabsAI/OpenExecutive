// One-person mode (see lib/localOwner.ts): the `jwt` callback in auth.ts marks
// the token at sign-in and `session` copies the mark here. Such a session has
// no email by design.
import "next-auth";

declare module "next-auth" {
  interface Session {
    localOwner?: boolean;
  }
}
