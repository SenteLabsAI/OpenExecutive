import HOST_LIST from "@/lib/host-list.json";
import { parseHostOrigins, type HostCall, type HostOrigins } from "@/lib/hostOrigins";

// Server-only: OE_HOST_ORIGINS and the host list, read once at startup (a
// restart makes an edit live, as for ALLOWED_EMAILS). Unset, there is no host
// origin and every page may be framed by this app alone.

export const HOST_ORIGINS: HostOrigins = parseHostOrigins(process.env.OE_HOST_ORIGINS);
if (HOST_ORIGINS.error) console.error(`[host origins] ${HOST_ORIGINS.error}; no host origin is trusted`);

export const HOST_CALLS: readonly HostCall[] = HOST_LIST.calls.map(([method, pattern]) => [method, pattern] as const);
