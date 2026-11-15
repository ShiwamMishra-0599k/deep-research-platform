// Minimal Chrome DevTools driver for headless Edge/Chrome (integration tests only; no dependencies).
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const EDGE = process.env.EDGE_PATH || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
export const OUT = process.env.E2E_OUT || join(dirname(fileURLToPath(import.meta.url)), "out");
mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function launch({ width = 1440, height = 900, mobile = false, reducedMotion = false, port = 9333 } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "edge-"));
  const proc = spawn(EDGE, [`--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--headless=new", "--disable-gpu", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
  let targets;
  for (let i = 0; i < 40; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); if (targets.find((t) => t.type === "page")) break; } catch {} await sleep(250); }
  const page = targets.find((t) => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  let id = 0; const pending = new Map(); const logs = [];
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
    if (d.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(d.params.type)) logs.push(d.params.type + ": " + d.params.args.map((a) => a.value ?? a.description).join(" "));
    if (d.method === "Runtime.exceptionThrown") logs.push("exception: " + d.params.exceptionDetails.text + " " + (d.params.exceptionDetails.exception?.description ?? ""));
  };
  const send = (method, params = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
  await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable");
  const setSize = (w, h, m = false) => send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: m });
  await setSize(width, height, mobile);
  if (reducedMotion) await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
  const api = {
    send, logs, setSize,
    async goto(url, wait = 1500) { await send("Page.navigate", { url }); await sleep(wait); },
    async eval(expr) { const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }); return r.result?.result?.value ?? r.result?.exceptionDetails?.text; },
    async shot(name, full = false) {
      let params = { format: "png" };
      if (full) {
        const h = await api.eval("document.documentElement.scrollHeight");
        params = { format: "png", captureBeyondViewport: true, clip: { x: 0, y: 0, width: (await api.eval("innerWidth")), height: h, scale: 1 } };
      }
      const r = await send("Page.captureScreenshot", params);
      writeFileSync(`${OUT}/${name}.png`, Buffer.from(r.result.data, "base64")); return name;
    },
    async click(sel) { const p = await api.eval(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});if(!e)return null;e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()`); if (!p) return false; await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: p[0], y: p[1] }); await send("Input.dispatchMouseEvent", { type: "mousePressed", x: p[0], y: p[1], button: "left", clickCount: 1 }); await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: p[0], y: p[1], button: "left", clickCount: 1 }); return true; },
    async hover(sel) { const p = await api.eval(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});if(!e)return null;const r=e.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()`); if (p) await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: p[0], y: p[1] }); return !!p; },
    async key(key, code = key, extra = {}) { await send("Input.dispatchKeyEvent", { type: "keyDown", key, code, windowsVirtualKeyCode: extra.vk ?? 0, ...extra }); await send("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode: extra.vk ?? 0 }); },
    async type(text) { await send("Input.insertText", { text }); },
    sleep,
    /** Every cookie in the browser jar, INCLUDING HttpOnly ones (what page script can NOT see). */
    async cookies() { const r = await send("Network.getAllCookies"); return r.result.cookies; },
    async clearCookies() { await send("Network.clearBrowserCookies"); },
    async setCookie(c) { return send("Network.setCookie", c); },
    async seedAuth() { await api.eval(`localStorage.setItem('auth_token','tok');localStorage.setItem('auth_user',JSON.stringify({id:'u1',email:'demo@example.com',name:'Demo User'}))`); },
    close() { try { ws.close(); } catch {} proc.kill(); },
  };
  return api;
}
