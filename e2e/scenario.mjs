// Full user-journey integration scenario, driven through a real browser.
//
//   React (Vite) -> FastAPI -> PostgreSQL -> research agent (LangGraph) -> tools -> Redis
//   with a FAKE LLM and FAKE internet (see e2e/README.md). Nothing here is a unit test:
//   every hop except the LLM and third-party websites is the real component.
//
// Usage: node e2e/scenario.mjs   (services started by e2e/run.sh or by hand)
import { launch } from "./cdp.mjs";

const APP = process.env.E2E_APP || "http://localhost:15173";
const API = process.env.E2E_API || "http://127.0.0.1:18080/api";
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
const token = () => ev(`localStorage.getItem('auth_token')`);
async function api(path, opts = {}) {
  return ev(`(async()=>{const r=await fetch(${JSON.stringify(API + path)},{method:${JSON.stringify(opts.method || "GET")},headers:{'Content-Type':'application/json',Authorization:'Bearer '+localStorage.getItem('auth_token')},body:${opts.body ? JSON.stringify(JSON.stringify(opts.body)) : "undefined"}});let j=null;try{j=await r.json()}catch(e){};return JSON.stringify({status:r.status,json:j})})()`).then((s) => JSON.parse(s));
}
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
  check("JWT stored, no PII in token", await ev(`(()=>{const t=localStorage.getItem('auth_token');if(!t)return false;const c=JSON.parse(atob(t.split('.')[1].replace(/-/g,'+').replace(/_/g,'/')));return !('email' in c)&&!('name' in c)&&!!c.sub})()`));
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

  // ── 8. Expired / invalid session while using the app ──────────────────────
  await ev(`localStorage.setItem('auth_token','not.a.valid.jwt')`);
  check("invalid token rejected by API (401)", (await api(`/conversations`)).status === 401);
  await ask("hello after expiry");
  check("expired session during a query -> back to sign-in", await waitFor(`location.pathname === '/login'`, 15000));
  check("stale credentials cleared", await waitFor(`localStorage.getItem('auth_token') === null`, 5000));

  // ── 9. Login again; data persists; logout ─────────────────────────────────
  await waitFor(`!!document.querySelector('#email')`, 15000);   // the redirect was a full page load
  await b.click("#email"); await b.type(email);
  await b.click("#password"); await b.type(password);
  await b.key("Enter", "Enter", { vk: 13, text: "\r" });
  check("login with the registered account", await waitFor(`location.pathname === '/' && !!document.querySelector('textarea')`, 15000));
  check("conversations survive logout/login", await waitFor(`document.querySelector('nav[aria-label="Conversations"]')?.innerText.includes('Renamed by test')`, 10000));
  await b.click(`button[aria-label="Sign out"]`);
  check("logout -> sign-in page", await waitFor(`location.pathname === '/login'`, 8000));
  check("logout clears stored credentials", await waitFor(`localStorage.getItem('auth_token') === null && localStorage.getItem('auth_user') === null`, 5000));

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

const consoleErrors = b.logs.filter((l) => !/React Router Future Flag|favicon|DevTools|Download the React DevTools|SSE error SseHttpError: HTTP 401|AxiosError: Request failed with status code 401/.test(l));
check("no unexpected browser console errors", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));
b.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
