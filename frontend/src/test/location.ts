import { vi } from "vitest";

/**
 * Replace the global `location` with a plain recorder so redirects (`location.href = "/login"`) can be asserted.
 * jsdom cannot navigate, so assigning `location.href` would otherwise just log "not implemented".
 */
export function stubLocation(pathname = "/") {
  const loc = { pathname, href: "http://localhost" + pathname, assigned: [] as string[] };
  const proxy = {
    get pathname() { return loc.pathname; },
    get href() { return loc.href; },
    set href(v: string) { loc.assigned.push(v); loc.href = v; },
  };
  vi.stubGlobal("location", proxy);
  return loc;
}
