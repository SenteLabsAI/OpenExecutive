// SSO sign-in: any OpenID Connect identity provider (Keycloak, Okta, Auth0,
// Authentik, Microsoft Entra ID, …), offered next to Google — or instead of
// it — when the operator sets AUTH_OIDC_ISSUER / _ID / _SECRET.
//
// It only adds a way in. Who may sign in is decided exactly as for Google:
// the verified email the provider returns goes through ALLOWED_EMAILS + the
// People roster (lib/allowlist.ts), the per-request re-check and the audit
// log, unchanged.
//
// The one rule that matters here is the verified email. Access is granted by
// email address alone, so an address the provider has not verified must not
// get in: at many providers a user can type any address into their own
// profile, including the owner's. `emailVerified` below fails closed, and the
// only way past it is an explicit operator opt-in.
//
// No imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/oidc.test.mjs).

/** The Auth.js provider id. Also fixes the callback path:
 *  /api/auth/callback/oidc. */
export const OIDC_PROVIDER_ID = "oidc";

/** What the sign-in button says when AUTH_OIDC_NAME is not set:
 *  "Sign in with SSO". */
export const OIDC_DEFAULT_NAME = "SSO";

export interface OidcEnv {
  /** AUTH_OIDC_ISSUER — e.g. https://sso.example.com/realms/company. */
  issuer: string | undefined;
  /** AUTH_OIDC_ID — the client id registered at the provider. */
  clientId: string | undefined;
  /** AUTH_OIDC_SECRET — that client's secret. */
  clientSecret: string | undefined;
  /** AUTH_OIDC_NAME — the provider's name on the button ("Sign in with Okta"). */
  name: string | undefined;
  /** AUTH_OIDC_TRUST_UNVERIFIED_EMAIL — the opt-in described above. */
  trustUnverifiedEmail: string | undefined;
}

export interface OidcConfig {
  issuer: string;
  clientId: string;
  clientSecret: string;
  name: string;
  trustUnverifiedEmail: boolean;
}

/** The provider settings, or null unless issuer, client id and secret are
 *  all set. A half-filled block offers no SSO button (Setup status says what
 *  is missing) rather than a button that fails at the provider. */
export function oidcConfig(env: OidcEnv): OidcConfig | null {
  const issuer = env.issuer?.trim() ?? "";
  const clientId = env.clientId?.trim() ?? "";
  const clientSecret = env.clientSecret?.trim() ?? "";
  if (!issuer || !clientId || !clientSecret) return null;
  return {
    issuer,
    clientId,
    clientSecret,
    name: env.name?.trim() || OIDC_DEFAULT_NAME,
    trustUnverifiedEmail: env.trustUnverifiedEmail?.trim().toLowerCase() === "true",
  };
}

/** Where the provider publishes its settings. Built here rather than by
 *  Auth.js, which appends to the issuer as written and so asks for
 *  `…//.well-known/…` when the issuer ends in a slash (Auth0's does). The
 *  issuer itself is kept exactly as written: it must equal the `iss` the
 *  provider puts in its tokens. */
export function discoveryUrl(issuer: string): string {
  return `${issuer.replace(/\/+$/, "")}/.well-known/openid-configuration`;
}

/** Microsoft Entra ID's issuers. */
function isEntraIssuer(issuer: string): boolean {
  try {
    return new URL(issuer).hostname.toLowerCase() === "login.microsoftonline.com";
  } catch {
    return false;
  }
}

function claimIsTrue(value: unknown): boolean {
  // Some providers send the boolean as a string; anything else fails closed.
  return value === true || value === "true";
}

/**
 * May this sign-in's email be trusted as the person's own? True when the
 * provider says it is verified:
 *   - the standard `email_verified` claim (Keycloak, Okta, Auth0, Authentik…);
 *   - on Microsoft Entra ID, which never sends that claim, its own
 *     `xms_edov` ("email domain owner verified") optional claim — read only
 *     from an Entra issuer, where Microsoft alone sets it.
 * Otherwise only when the operator opted in with
 * AUTH_OIDC_TRUST_UNVERIFIED_EMAIL=true. A missing or odd value is false.
 */
export function emailVerified(
  profile: Record<string, unknown> | null | undefined,
  config: Pick<OidcConfig, "issuer" | "trustUnverifiedEmail">,
): boolean {
  if (config.trustUnverifiedEmail) return true;
  if (!profile) return false;
  if (claimIsTrue(profile.email_verified)) return true;
  return isEntraIssuer(config.issuer) && claimIsTrue(profile.xms_edov);
}
