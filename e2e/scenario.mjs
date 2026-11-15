// Full user-journey integration scenario, driven through a real browser.
//
//   React (Vite) -> FastAPI -> PostgreSQL -> research agent (LangGraph) -> tools -> Redis
//   with a FAKE LLM and FAKE internet (see e2e/README.md). Nothing here is a unit test:
//   every hop except the LLM and third-party websites is the real component.
//
// The browser talks to ONE origin (the Vite dev server proxies /api): same-origin cookie topology. Sessions are
// HttpOnly cookies; nothing is stored in web storage. A small attacker server on a different SITE
// (localhost vs 127.0.0.1) exercises real cross-site CSRF behaviour.
//
// Usage: node e2e/scenario.mjs   (services started by e2e/run.py or by hand)
import http from "node:http";
import { launch } from "./cdp.mjs";

const APP = process.env.E2E_APP || "http://127.0.0.1:15173";
const ATTACKER_PORT = 15999;
const ATTACKER = `http://localhost:${ATTACKER_PORT}`; // a different SITE from 127.0.0.1
const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok: !!ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok || !detail ? "" : "  -> " + detail}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const b = await launch({ width: 1280, height: 900, port: 9444 });
const ev = (e) => b.eval(e);
async function waitFor(expr, timeout = 30000, step = 250) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    const v = await ev(expr).catch(() => null);
    if (v) return v;
    await sleep(step);
  }
  return null;
}
// Navigate and wait until the page has actually rendered `sel` (cold Vite can take a while).
async function go(url, sel) {
  await b.goto(url, 300);
  return waitFor(`!!document.querySelector(${JSON.stringify(sel)})`, 90000);
}
const email = `e2e-${Date.now()}@example.com`;
const password = "e2e-password-123";
const webStorage = `JSON.stringify([...Object.keys(localStorage), ...Object.keys(sessionStorage)])`;
// Same-origin API call from the page, exactly as the app does it: cookies are automatic, unsafe methods carry the CSRF header.
// opts.legacy = behave like the OLD frontend (no X-Auth-Mode header); opts.noCsrf = omit the CSRF header.
async function api(path, opts = {}) {
  const m = opts.method || "GET";
  const body = opts.body ? JSON.stringify(JSON.stringify(opts.body)) : "undefined";
  return ev(`(async()=>{const m=${JSON.stringify(m)};const c=(document.cookie.split('; ').find(x=>x.startsWith('lumen_csrf='))||'').slice(11);
    const h={'Content-Type':'application/json'};if(!${!!opts.legacy})h['X-Auth-Mode']='cookie';if(m!=='GET'&&c&&!${!!opts.noCsrf})h['X-CSRF-Token']=decodeURIComponent(c);
    const r=await fetch('/api'+${JSON.stringify(path)},{method:m,headers:h,body:${body}});let j=null;try{j=await r.json()}catch(e){};return JSON.stringify({status:r.status,json:j})})()`).then((s) => JSON.parse(s));
}
async function jar() {
  const all = await b.cookies();
  const by = (n) => all.find((c) => c.name === n);
  return { all, at: by("lumen_at"), rt: by("lumen_rt"), csrf: by("lumen_csrf") };
}

// A page on a different SITE, trying the classic attacks against the signed-in victim.
let attackerHits = 0;
const attacker = http.createServer((req, res) => {
  attackerHits++;
  res.setHeader("Content-Type", "text/html");
  if (req.url.startsWith("/csrf-form")) {
    res.end(`<form id=f method=POST action="${APP}/api/conversations" enctype="text/plain"><input name='{"title":"PWNED-BY-ATTACKER","x":"' value='"}'></form><script>f.submit()</script>`);
  } else if (req.url.startsWith("/login-csrf")) {
    res.end(`<form id=f method=POST action="${APP}/api/auth/login" enctype="text/plain"><input name='{"email":"attacker@evil.example","password":"x","pad":"' value='"}'></form><script>f.submit()</script>`);
  } else if (req.url.startsWith("/fetch-attack")) {
    res.end(`<script>window.__r={};
      fetch("${APP}/api/auth/me",{credentials:"include"}).then(r=>r.json()).then(j=>window.__r.read=JSON.stringify(j)).catch(e=>window.__r.read="blocked: "+e.message);
      fetch("${APP}/api/conversations",{method:"POST",credentials:"include",mode:"no-cors",headers:{"Content-Type":"text/plain"},body:'{"title":"PWNED-FETCH"}'}).then(()=>window.__r.post="sent").catch(e=>window.__r.post="blocked: "+e.message);
    </script>ok`);
  } else res.end("attacker");
});
await new Promise((r) => attacker.listen(ATTACKER_PORT, "127.0.0.1", r));
async function ask(text) {
  await b.click("textarea");
  await b.type(text);
  await b.key("Enter", "Enter", { vk: 13, text: "\r" });
}
const lastArticle = `[...document.querySelectorAll('article')].pop()`;

try {
  // ── 1. Registration ───────────────────────────────────────────────────────
  await go(`${APP}/register`, "#name");
  await b.click("#name"); await b.type("E2E Tester");
  await b.click("#email"); await b.type(email);
  await b.click("#password"); await b.type(password);
  await b.key("Enter", "Enter", { vk: 13, text: "\r" });
  check("register -> lands on workspace", await waitFor(`location.pathname === '/' && !!document.querySelector('textarea')`));
  check("sidebar shows the new user", await ev(`document.body.innerText.includes('E2E Tester')`));
  let j = await jar();
  check("session cookies issued: access, refresh, csrf", j.at && j.rt && j.csrf, j.all.map((c) => c.name).join(","));
  check("access cookie: HttpOnly, SameSite=Lax, Path=/, host-only", j.at?.httpOnly && j.at?.sameSite === "Lax" && j.at?.path === "/" && !j.at?.domain?.startsWith("."), JSON.stringify(j.at));
  check("refresh cookie: HttpOnly, SameSite=Strict, Path=/api/auth only", j.rt?.httpOnly && j.rt?.sameSite === "Strict" && j.rt?.path === "/api/auth", JSON.stringify(j.rt));
  check("CSRF cookie is script-readable on purpose (not HttpOnly)", j.csrf && !j.csrf.httpOnly);
  const visible = await ev(`document.cookie`);
  check("page script CANNOT read the access or refresh cookie (only the CSRF cookie is visible)", !/lumen_at|lumen_rt/.test(visible) && /lumen_csrf/.test(visible), visible);
  check("NO token or identity in localStorage/sessionStorage", (await ev(webStorage)) === "[]", await ev(webStorage));
  check("access cookie lifetime is ~15 minutes", j.at && Math.abs(j.at.expires - Date.now() / 1000 - 900) < 120, String(j.at && j.at.expires - Date.now() / 1000));
  await b.shot("e2e-01-welcome");

  // ── 2. Research request: live checkpoints, blocks, citations ─────────────
  await ask("search EV charging stations in Lucknow and Bitcoin price in INR");
  const sawTrail = await waitFor(`!!document.querySelector('[aria-label="Research progress"] li')`, 20000, 100);
  check("live research progress (checkpoints) displayed while working", sawTrail);
  const trailText = await ev(`document.querySelector('[aria-label="Research progress"]')?.innerText || ''`);
  await b.shot("e2e-02-streaming");
  const done = await waitFor(`!!document.querySelector('article .markdown-body') && !document.querySelector('article[aria-busy="true"]')`, 60000);
  check("final answer rendered (markdown)", done);
  await sleep(1500);
  check("map block rendered (Leaflet)", await ev(`!!document.querySelector('article .leaflet-container')`));
  check("table block rendered with place rows", await ev(`document.querySelectorAll('article [aria-label="Results table"] tbody tr').length >= 1`));
  check("insight card shows Bitcoin price", await ev(`[...document.querySelectorAll('article li.card')].some(e=>/Bitcoin/i.test(e.innerText))`));
  check("citations (sources) displayed", await ev(`document.querySelectorAll('article [aria-label="Sources"] a').length >= 1`));
  check("source links are http(s) and open safely", await ev(`[...document.querySelectorAll('article [aria-label="Sources"] a')].every(a=>/^https?:/.test(a.href)&&a.rel.includes('noreferrer')&&a.target==='_blank')`));
  await b.shot("e2e-03-answer");

  const url1 = await ev("location.pathname");
  const conv1 = url1.split("/c/")[1];
  check("conversation created and routed (/c/:id)", !!conv1);

  // ── 3. Persistence: API + reload ──────────────────────────────────────────
  const m1 = await api(`/conversations/${conv1}/messages`);
  check("messages persisted in PostgreSQL (user + assistant)", m1.status === 200 && m1.json.length === 2 && m1.json[0].role === "user" && m1.json[1].role === "assistant", JSON.stringify(m1).slice(0, 200));
  const stored = m1.json?.[1];
  check("assistant message stored with blocks + sources", stored && ["markdown", "leaflet-map", "data-table", "insight-cards"].every((t) => stored.blocks.some((x) => x.template_id === t)) && stored.sources.length >= 1,
    stored ? stored.blocks.map((x) => x.template_id).join(",") : "none");
  await go(`${APP}/c/${conv1}`, "textarea");
  check("reload restores conversation with blocks", await waitFor(`!!document.querySelector('article .leaflet-container') && document.querySelectorAll('article').length===1`, 15000));
  check("title derived from first question", await ev(`document.querySelector('nav[aria-label="Conversations"]').innerText.includes('search EV charging stations')`));

  // ── 3b. The 15-minute ACCESS cookie expires while the refresh session is still alive ──
  const before = await jar();
  await b.send("Network.deleteCookies", { name: "lumen_at", url: APP }); // what the browser does at Max-Age
  check("precondition: access cookie gone, refresh cookie present", !(await jar()).at && !!(await jar()).rt);
  await go(`${APP}/c/${conv1}`, "textarea");
  check("REGRESSION: reload after the access cookie expired stays signed in (silent refresh)", (await waitFor(`!!document.querySelector('article')`, 15000)) && (await ev("location.pathname")) === `/c/${conv1}`);
  const after = await jar();
  check("new access cookie issued and the refresh token ROTATED", after.at && after.rt && after.rt.value !== before.rt.value);
  check("API works with the rotated cookies", (await api(`/conversations/${conv1}/messages`)).status === 200);

  // ── 4. Second conversation, switching ─────────────────────────────────────
  await b.click("nav ~ div button.btn-primary, aside button.btn-primary");
  await sleep(800);
  check("new chat shows empty welcome", await ev(`!!document.querySelector('#welcome-title')`));
  await ask("latest RBI news");
  check("news table rendered", await waitFor(`!!document.querySelector('article [aria-label="Results table"]')`, 60000));
  await waitFor(`!document.querySelector('article[aria-busy="true"]')`, 30000);
  const convList = await api(`/conversations`);
  check("two conversations listed (newest first)", convList.json.length === 2 && convList.json[0].title.includes("RBI"), JSON.stringify(convList.json.map((c) => c.title)));
  await b.click(`nav[aria-label="Conversations"] li:nth-child(2) button`);
  check("switching conversations loads the other transcript", await waitFor(`!!document.querySelector('article .leaflet-container')`, 15000));

  // ── 5. Rename (API; the UI auto-titles) ───────────────────────────────────
  const rn = await api(`/conversations/${conv1}`, { method: "PATCH", body: { title: "Renamed by test" } });
  check("rename conversation", rn.status === 204);
  check("renamed title persisted", (await api(`/conversations/${conv1}`)).json.title === "Renamed by test");
  const noCsrf = await api(`/conversations/${conv1}`, { method: "PATCH", body: { title: "no csrf" }, noCsrf: true });
  check("state-changing request WITHOUT the CSRF header is refused (403) and changes nothing", noCsrf.status === 403 && (await api(`/conversations/${conv1}`)).json.title === "Renamed by test", JSON.stringify(noCsrf));

  // ── 6. Clarification and error paths ──────────────────────────────────────
  await b.click(`nav[aria-label="Conversations"] li:nth-child(1) button`);
  await sleep(800);
  await ask("please clarify this");
  check("clarifying question shown", await waitFor(`/Which city/.test(document.body.innerText)`, 40000));
  await waitFor(`!document.querySelector('article[aria-busy="true"]')`, 30000);
  await ask("llm-fail please");
  check("LLM provider failure -> friendly error, no internals", await waitFor(`/research service hit an error/i.test(document.body.innerText)`, 60000));
  const bodyText = await ev(`document.body.innerText`);
  check("no stack trace / key / provider detail leaked to the page", !/Traceback|OPENROUTER|sk-|fake provider outage|500/.test(bodyText));
  await waitFor(`!document.querySelector('article[aria-busy="true"]')`, 30000);
  await sleep(500);
  const convNow = (await ev("location.pathname")).split("/c/")[1];
  const mErr = await api(`/conversations/${convNow}/messages`);
  check("error outcome persisted so reload shows it", mErr.json.some((m) => m.role === "assistant" && /research service hit an error/i.test(m.content || "")));

  // ── 7. Delete the ACTIVE conversation, then keep working ────────────────
  await b.hover(`nav[aria-label="Conversations"] li:nth-child(1)`);
  await b.click(`nav[aria-label="Conversations"] li:nth-child(1) button[aria-label^="Delete conversation"]`);
  check("delete asks for confirmation (accessible dialog)", await waitFor(`!!document.querySelector('[role=alertdialog]')`, 5000));
  await b.click(`[role=alertdialog] .btn-danger`);
  await sleep(1500);
  check("conversation deleted (gone from API)", (await api(`/conversations/${convNow}`)).status === 404);
  check("after deleting the active conversation the app shows the welcome screen", await waitFor(`!!document.querySelector('#welcome-title')`, 5000));
  await ask("search news after delete");
  check("a new question after deleting the active chat works (no stale 404)", await waitFor(`!!document.querySelector('article .markdown-body') && !document.querySelector('article[aria-busy="true"]')`, 60000)
        && !/no longer exists/i.test(await ev(`document.body.innerText`)));

  // ── 8. Real cross-site attacks from a different SITE ──────────────────────
  const victimConvs = (await api(`/conversations`)).json.length;
  const victimMe = (await api(`/auth/me`)).json.email;
  await b.goto(`${ATTACKER}/csrf-form`, 2500);
  await b.goto(`${ATTACKER}/login-csrf`, 2500);
  await b.goto(`${ATTACKER}/fetch-attack`, 2500);
  const attackerSaw = await ev(`JSON.stringify(window.__r || {})`);
  check("attacker pages really ran from a different site", attackerHits >= 3 && (await ev("location.origin")) === ATTACKER, `${attackerHits} hits`);
  check("attacker's credentialed read of /auth/me is BLOCKED (no CORS grant)", /blocked/i.test(attackerSaw) && !attackerSaw.includes(email), attackerSaw);
  await go(`${APP}/`, "textarea");
  const convsAfter = (await api(`/conversations`)).json;
  check("CSRF attacks created NOTHING (no PWNED conversation)", convsAfter.length === victimConvs && !convsAfter.some((c) => /PWNED/.test(c.title)), JSON.stringify(convsAfter.map((c) => c.title)));
  check("login-CSRF failed: still signed in as the victim, not the attacker", (await api(`/auth/me`)).json.email === victimMe && victimMe === email, victimMe);

  // ── 8b. Server-side revocation while the app is open ──────────────────────
  await api(`/auth/logout-all`, { method: "POST" });   // "sign out everywhere"
  await ask("hello after revocation");
  check("revoked session during a query -> back to sign-in", await waitFor(`location.pathname === '/login'`, 20000));
  check("no session cookies left after the server ended the session", await waitFor(`true`, 1) && !(await jar()).at && !(await jar()).rt, JSON.stringify((await jar()).all.map((c) => c.name)));
  check("web storage still empty", (await ev(webStorage)) === "[]");

  // ── 9. Login again; data persists; logout ─────────────────────────────────
  await waitFor(`!!document.querySelector('#email')`, 15000);   // the redirect was a full page load
  await b.click("#email"); await b.type(email);
  await b.click("#password"); await b.type(password);
  await b.key("Enter", "Enter", { vk: 13, text: "\r" });
  check("login with the registered account", await waitFor(`location.pathname === '/' && !!document.querySelector('textarea')`, 15000));
  check("conversations survive logout/login", await waitFor(`document.querySelector('nav[aria-label="Conversations"]')?.innerText.includes('Renamed by test')`, 10000));
  const stolen = (await jar()).at;
  await b.click(`button[aria-label="Sign out"]`);
  check("logout -> sign-in page", await waitFor(`location.pathname === '/login'`, 8000));
  // The UI navigates immediately and the logout request completes just after, so poll briefly for the server's Set-Cookie deletions.
  let gone = await jar();
  for (let i = 0; i < 20 && (gone.at || gone.rt || gone.csrf); i++) { await sleep(250); gone = await jar(); }
  check("logout cleared all three session cookies and web storage is empty", !gone.at && !gone.rt && !gone.csrf && (await ev(webStorage)) === "[]", gone.all.filter((c) => c.name.startsWith("lumen")).map((c) => c.name).join(","));
  await b.setCookie({ name: "lumen_at", value: stolen.value, url: APP, httpOnly: true, sameSite: "Lax", path: "/" });
  const replay = await api(`/auth/me`);
  check("REPLAY: an access cookie copied before logout is rejected afterwards (server-side revocation)", replay.status === 401, JSON.stringify(replay));
  await b.clearCookies();

  // ── 9b. A pre-migration (localStorage) session is upgraded, not signed out ──
  await go(`${APP}/login`, "#email");
  const legacyLogin = await api(`/auth/login`, { method: "POST", body: { email, password }, legacy: true }); // like the OLD frontend
  check("old-style login (no X-Auth-Mode) still receives a body token in dual mode", legacyLogin.status === 200 && !!legacyLogin.json.token);
  await b.clearCookies();
  await ev(`localStorage.setItem('auth_token', ${JSON.stringify(legacyLogin.json?.token || "x")}); localStorage.setItem('auth_user', '{"email":"stale"}')`);
  await go(`${APP}/`, "textarea");
  check("legacy session UPGRADED to cookies on first load (no forced re-login)", (await ev("location.pathname")) === "/" && !!(await jar()).rt);
  check("legacy localStorage credential and identity PURGED", (await ev(webStorage)) === "[]", await ev(webStorage));
  await b.click(`button[aria-label="Sign out"]`);
  await waitFor(`location.pathname === '/login'`, 8000);

  // ── 10. A different person signs in on the same browser tab (no reload) ───
  const user1Marker = "Renamed by test";
  await b.click(`a[href="/register"]`);                      // client-side navigation: app memory is NOT reset
  await waitFor(`!!document.querySelector('#name')`, 10000);
  await b.click("#name"); await b.type("Second Person");
  await b.click("#email"); await b.type(`e2e2-${Date.now()}@example.com`);
  await b.click("#password"); await b.type("second-password-123");
  await b.key("Enter", "Enter", { vk: 13, text: "\r" });
  check("second user registers in the same tab", await waitFor(`location.pathname === '/' && !!document.querySelector('textarea')`, 15000));
  await sleep(1200);
  const page2 = await ev(`document.body.innerText`);
  check("second user does NOT see the first user's conversations", !page2.includes(user1Marker) && !/search EV charging/i.test(page2));
  check("second user starts on a clean welcome screen", await ev(`!!document.querySelector('#welcome-title') && document.querySelectorAll('article').length === 0`));
  await b.click(`button[aria-label="Sign out"]`);
  await waitFor(`location.pathname === '/login'`, 8000);

  // ── 11. Protected routes and bad credentials ──────────────────────────────
  await go(`${APP}/`, "#email");
  check("protected route redirects to login when signed out", (await ev("location.pathname")) === "/login");
  await b.click("#email"); await b.type(email);
  await b.click("#password"); await b.type("wrong-password-zzz");
  await b.key("Enter", "Enter", { vk: 13, text: "\r" });
  check("wrong password shows the server's message", await waitFor(`/Invalid credentials/.test(document.body.innerText)`, 8000));
} catch (e) {
  check("scenario completed without an unexpected exception", false, String(e?.stack || e));
}

attacker.close();
const consoleErrors = b.logs.filter((l) => !/React Router Future Flag|favicon|DevTools|Download the React DevTools|SSE error SseHttpError: HTTP 401|AxiosError: Request failed with status code 40[139]/.test(l));
check("no unexpected browser console errors", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));
b.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
