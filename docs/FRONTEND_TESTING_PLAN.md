# Frontend Testing Plan and Status

## Status: IMPLEMENTED (approved by the user; the original plan text below is kept for rationale)

| Item | Result |
|---|---|
| Installed (exact, `--save-exact`, dev only) | `vitest@3.2.7`, `@vitest/coverage-v8@3.2.7`, `jsdom@26.1.0`, `@testing-library/react@16.3.3`, `@testing-library/dom@10.4.2`, `@testing-library/user-event@14.6.10`, `@testing-library/jest-dom@6.10.0` |
| Files added | `vitest.config.ts`, `src/test/setup.ts`, `src/test/location.ts`, 13 co-located `*.test.ts(x)` files; `package.json` scripts `test`, `test:watch`, `test:coverage` |
| Application code changed | **None.** No `data-testid`, export or behaviour change was needed (all queries use roles/labels) |
| Tests | **203 passing in 13 files**, identical across 3 consecutive runs (about 45-65 s wall including coverage instrumentation on the Windows dev machine) |
| Coverage (v8, whole `src/`) | statements **100 %**, lines **100 %**, functions **100 %**, branches **95.62 %** |
| Enforced thresholds (global) | statements 70, lines 70, functions 70, branches 60 (`vitest.config.ts`); verified to FAIL the run when unmet (`ERROR: Coverage for statements ... does not meet global threshold`) |
| Typecheck / build | `tsc --noEmit` clean with tests included; `vite build` and `npm run build` pass; no test code in the production bundle |
| Mutation proof | 7 of 7 deliberate regressions in critical behaviour were detected by the suite (see below) |
| Run | `cd frontend && npm test` / `npm run test:coverage` (Node 22+ in CI: `jest-dom` 6.10 requires it) |

Coverage is a floor, not the goal. The suite is behavioural: real zustand stores, real routing, real axios interceptors (the axios *adapter* is the
mocked network boundary), `@microsoft/fetch-event-source` and `react-leaflet` mocked at module boundaries, no MSW.

### What the 203 tests cover (by priority, as planned in section 6)

- **P0** `store/auth` (login/register success and failure, token never stored inside the user object, logout clears credentials AND the chat store so the next
  person on the tab sees nothing, a second user starts clean, synchronous hydration at store creation incl. malformed JSON), `store/chat` (select/cache/failure clears
  only its own `activeId`, create, delete, rename, message helpers), `lib/sse` (headers, Authorization only with a token, non-2xx -> `SseHttpError(status)`, 401 purge + redirect
  unless already on /login or /register, callbacks, abort signal), `lib/api` (Bearer interceptor, 401 purge + redirect with no loop, other statuses keep the session, `apiError`).
- **P1** `ChatPanel` (optimistic messages, event handling for user_message/checkpoint/blocks/clarification/error/persist_error/persisted, malformed events ignored, the
  exact user-facing message for 429/401/404/413/422/500/503/network failure, retry after failure, Stop aborts the signal, auto-scroll follows only while the user is at the
  bottom, first-question titling, create-on-first-send, greeting by time of day), `App`/route protection (signed out never sees the workspace or triggers API calls; no
  sign-in flash on reload), `ConfirmDialog` (role/aria, initial focus on Cancel, Tab/Shift+Tab trap, Escape, backdrop, focus restoration), `ConversationSidebar`
  (accessible delete dialog instead of `confirm()`, active-chat delete, unloadable route redirects home, drawer), `ChatInput` (Enter/Shift+Enter, IME `isComposing`/keyCode 229, busy/Stop).
- **P2** `BlockRenderer` (unknown type -> JSON fallback invariant), `DataTableBlock` (sort text/numeric, `aria-sort`, safe links, `javascript:` cell stays text), `InsightCardsBlock`
  (label/value parsing, severity as text), `MarkdownBlock` (links `target=_blank rel=noreferrer`, raw HTML and `javascript:` links neutralised), `MessageBubble`, `CheckpointTrail`,
  `LeafletMapBlock` (react-leaflet mocked), `Login`/`Register` (server error text, 429 text, network fallback, pending state, error cleared on resubmit), `Chat` page shell (skip link, drawer, Escape).

### Mutation proof (the tests detect real regressions)

Each mutation was applied to the application code alone, the suite run, then the file restored byte-for-byte (verified: no diff against HEAD):

| Mutation | Result |
|---|---|
| `logout()` no longer resets the chat store (cross-user leak) | **killed** (3 failing tests: auth store, sidebar) |
| SSE `onopen` 401 no longer redirects to /login | **killed** (1) |
| `selectConversation` failure no longer clears `activeId` | **killed** (2: chat store, sidebar) |
| Markdown links lose `rel="noreferrer"` | **killed** (1) |
| `ConfirmDialog` ignores Escape | **killed** (2) |
| axios request interceptor stops sending the Bearer token | **killed** (1) |
| `ChatPanel` ignores the `persist_error` event | **killed** (1) |

### Not covered / cannot reasonably be covered yet

| Area | Why | Where it IS covered |
|---|---|---|
| Real Leaflet map rendering, tiles, pin visuals, popups | jsdom has no layout engine; `react-leaflet` is mocked, so the 100 % on `LeafletMapBlock` proves our wiring (labels, counts, props), not the map | `e2e/scenario.mjs` renders the real map in a real browser |
| Layout, responsive behaviour, the drawer's CSS transform, `prefers-reduced-motion`, colour contrast, focus ring appearance | no CSS/layout in jsdom (`css: false`); tests assert class names/ARIA only (e.g. `invisible` on the closed drawer) | e2e browser run and the programmatic contrast scan (`DESIGN.md` section 9) |
| Real SSE over the network (chunking, reconnect, proxies, `fetch` streaming) | the library is mocked at its boundary and its callbacks driven; our handling of every event is tested, the transport is not | e2e journey against the real backend and agent |
| Real server error bodies | API error shapes are hand-written in the tests; they can drift from the backend | backend tests and the e2e journey |
| Framer-motion animation timing | not asserted (animations are decorative and `MotionConfig reducedMotion` is app-level) | none (by design) |
| Cross-browser behaviour (Firefox, Safari), mobile devices, screen readers | jsdom only; ARIA roles/names/focus are asserted but no assistive technology is run | not covered anywhere yet |
| `src/main.tsx`, `src/vite-env.d.ts` | entry point and ambient types: excluded from coverage by design | e2e |
| Branch coverage gaps (about 4 % of branches) | defensive branches such as `?? []` fallbacks in ChatPanel (88 % branch) and one rarely hit default in the table/dialog | n/a |
| HttpOnly-cookie session behaviour | not implemented yet; when `docs/COOKIE_AUTH_MIGRATION_PLAN.md` lands the auth store/api/sse tests above are the safety net and must be extended first | n/a |

### Dev-tooling advisories introduced by the new dependencies (production audit unchanged)

`npm audit --omit=dev` is unchanged (2 moderate, 0 high/critical: react-router, documented). The full audit gains **vitest (critical)** and **tinypool (critical)**
(`GHSA-82fw-gwwq-j7x9` `@vitest/mocker` path traversal / arbitrary file read; `GHSA-5gmw-xhrv-c9v3`, `GHSA-85c8-ppgw-ccpr` tinypool prototype pollution to RCE via
worker options) plus `@vitest/coverage-v8`/`test-exclude` (moderate). They affect the dev-time test runner executing untrusted *test code or options* and are not
shipped. The only npm-offered fix is Vitest 5.1.0, a semver-major that is not compatible with this repo's Vite 5.4 (Vitest 4+ needs Vite 6+), so it is deferred together
with the Vite upgrade. Revisit before deployment; CI gates on `npm audit --omit=dev --audit-level=high` (production deps only).

---

## Original plan (kept for rationale)

## 1. Current state (verified in the repository)

| Item | Finding |
|---|---|
| Test script / runner | **None.** `frontend/package.json` scripts: `dev`, `build` (`tsc -b && vite build`), `preview`, `lint` (`tsc --noEmit`) |
| Test dependencies | **None** (no vitest, jest, testing-library, jsdom, playwright, cypress) |
| Installed toolchain | React 18.3.x, Vite **5.4.21** (installed), TypeScript ^5.6.3, `"type": "module"`, strict TS, path alias `@/*` → `src/*` (both `tsconfig.json` and `vite.config.ts`) |
| Lockfile size | 316 packages in `package-lock.json` today |
| What *does* exist | `tsc` + production build gates, and the browser harness `e2e/scenario.mjs` (42 checks over the real stack with a fake LLM). It covers main journeys but cannot isolate store/component logic or run in milliseconds |

Consequence: three regressions found during the last milestone — logout leaking chat state (`store/auth.ts` → `useChat.setState`),
stale `activeId` after a failed `selectConversation`, and 401/429/5xx handling in `lib/sse.ts` + `ChatPanel.tsx` — are protected only by the slow
browser scenario.

## 2. Recommendation (minimal, compatible with React 18 + Vite 5.4)

**Vitest 3.2.x + jsdom + React Testing Library + user-event + jest-dom.** Vitest reuses `vite.config.ts` (so the `@` alias, JSX transform and
`import.meta.env` behave exactly as in the app), needs no Babel layer, and has a first-party coverage provider.

| Package | Version to pin | Why |
|---|---|---|
| `vitest` | **3.2.7** | Latest line whose `vite` dependency range includes Vite 5 (`^5.0.0 \|\| ^6.0.0 \|\| ^7.0.0-0`) |
| `@vitest/coverage-v8` | **3.2.7** (must equal vitest) | V8 coverage, no instrumentation step |
| `jsdom` | **26.1.0** | DOM environment; `engines.node >=18`; vitest peer is `jsdom: '*'` |
| `@testing-library/react` | **16.3.3** | Peer: react/react-dom `^18 \|\| ^19`, `@types/react(-dom)` `^18 \|\| ^19`, `@testing-library/dom ^10` — all satisfied |
| `@testing-library/dom` | **10.4.2** | Required peer of RTL 16 (not auto-installed by RTL 16) |
| `@testing-library/user-event` | **14.6.10** | Realistic keyboard/click sequences (Tab trap, Enter vs Shift+Enter) |
| `@testing-library/jest-dom` | **6.10.0** | DOM matchers; use the `/vitest` entry. **`engines.node >=22`** — CI must use Node ≥ 22 (local Node here is 24.22.1) |

Not recommended now: **Vitest 4.x/5.x** (4.x requires Vite `^6 \|\| ^7 \|\| ^8`; for 5.1.0 the registry shows no direct `vite` dependency and I could not
verify its Vite 5 support — treat as incompatible until the app moves off Vite 5), **jest-dom 7.0.1 / jsdom 31 / RTL-major bumps** (not needed; newer
engines floors).

### Peer-dependency compatibility check (against what the repo has)

| Requirement | Repo | OK? |
|---|---|---|
| vitest 3.2.7 → `vite ^5 \|\| ^6 \|\| ^7` | vite 5.4.21 | yes |
| vitest 3.2.7 → node `^18 \|\| ^20 \|\| >=22` | local 24.22.1 | yes |
| RTL 16.3.3 → react `^18 \|\| ^19` | react ^18.3.1 | yes |
| RTL 16.3.3 → `@types/react ^18 \|\| ^19` | ^18.3.12 | yes |
| jest-dom 6.10.0 → node `>=22` | local 24.22.1; CI to pin ≥22 | yes (CI constraint) |
| vitest peer `@types/node ^18 \|\| ^20 \|\| >=22` | not a direct dep today | **to confirm at install** (npm may auto-install or warn; add `@types/node` only if tsc needs it) |

### Mocking strategy: mock modules, not the network (MSW avoided)

- **Avoid MSW** (`msw` latest 3.0.6; ~service-worker/interceptor machinery, one more dependency tree, and SSE streaming handlers are awkward). The app's
  network surface is exactly two modules: `src/lib/api.ts` (axios instance) and `src/lib/sse.ts` (`fetchEventSource`).
- Component/store tests: `vi.mock("@/lib/api")` and `vi.mock("@/lib/sse")`. Tests of `api.ts` itself: use the **real axios instance with a stub adapter**
  (`api.defaults.adapter = async (cfg) => …`) so the request/response interceptors (Bearer header, 401 purge + redirect) are exercised for real.
- `sse.ts`: either `vi.mock("@microsoft/fetch-event-source")` and call the captured `onopen`/`onmessage`/`onerror`, or stub `globalThis.fetch`.
  *Unverified:* whether jsdom-environment tests see Node's `Response`/`ReadableStream` globals; decide in Stage 2 by a 5-line spike.
- `react-leaflet`/`leaflet`: **mock** (`vi.mock("react-leaflet", …)` rendering simple divs). jsdom has no layout engine, so real maps cannot be tested
  here; the real-browser e2e remains the map's test.
- `framer-motion`: leave real (the app uses `MotionConfig reducedMotion="user"`); if animation timers cause flakiness, mock `AnimatePresence`/`motion`.

## 3. Evidence — exact commands and results (read-only)

```
npm view vitest version                          -> 5.1.0 (latest)
npm view vitest@3 version                        -> 3.2.7 ;  vitest@3.2.7 dependencies.vite -> '^5.0.0 || ^6.0.0 || ^7.0.0-0'
npm view vitest@2 version                        -> 2.1.9 ;  vitest@2.1.9 dependencies.vite -> '^5.0.0'
npm view vitest@4.0.0 dependencies.vite          -> '^6.0.0 || ^7.0.0'      (vitest@4.1.12 -> '^6.0.0 || ^7.0.0 || ^8.0.0')
npm view vitest@3.2.7 engines                    -> node ^18 || ^20 || >=22
npm view @vitest/coverage-v8@3.2.7 peerDependencies -> vitest 3.2.7, @vitest/browser 3.2.7
npm view @testing-library/react version          -> 16.3.3 ; peerDependencies -> react ^18||^19, react-dom ^18||^19, @types/react ^18||^19, @types/react-dom ^18||^19, @testing-library/dom ^10
npm view @testing-library/dom version            -> 10.4.2
npm view @testing-library/user-event version     -> 14.6.10 (peer @testing-library/dom >=7.21.4)
npm view @testing-library/jest-dom@^6 version    -> 6.10.0 ; engines -> node >=22
npm view jsdom@^26 version                       -> 26.1.0 ; engines -> node >=18
npm view jsdom version                           -> 31.0.1 (engines ^22.22.2 || ^24.15.0 || >=26)   [not recommended]
npm view msw version                             -> 3.0.6                                              [not recommended]
```

Vitest 2.1.9 would also work with Vite 5 (`vite ^5`), but 3.2.7 is the newer maintained line that still accepts Vite 5; either is acceptable.

### Dependency-count impact (measured in a TEMP copy; `frontend/` untouched)

Copied `package.json` + `package-lock.json` to the scratchpad and ran
`npm install --package-lock-only --ignore-scripts -D vitest@3.2.7 @vitest/coverage-v8@3.2.7 jsdom@26.1.0 @testing-library/react@16.3.3 @testing-library/dom@10.4.2 @testing-library/user-event@14.6.10 @testing-library/jest-dom@6.10.0`.

| | Packages in lockfile |
|---|---|
| Before | 316 |
| After | 467 |
| **Added (all devDependencies, none ship to users)** | **151** |

The resulting lockfile was **not** audited for advisories; run `npm audit` on the real result at approval time. `git status frontend` was clean afterwards.

## 4. Alternatives considered

| Option | For | Against (for this repo) |
|---|---|---|
| **Vitest + RTL + jsdom (recommended)** | Reuses Vite config/alias; fast; TS-native; coverage built in; no Babel | jsdom ≠ real browser (no layout, focus quirks); +151 dev packages |
| Jest + RTL | Ubiquitous | Needs a TS/ESM transform and an alias/`import.meta.env` shim duplicating Vite's config; a second pipeline to maintain |
| `happy-dom` instead of jsdom | Faster | Less complete DOM/focus/events — risky for `ConfirmDialog` focus-trap and keyboard tests |
| Playwright component testing | Real browser, real Leaflet | Experimental CT mode, browser downloads in CI, heavier, slower; overlaps with the existing e2e harness |
| Cypress component testing | Real browser | Largest footprint; second runner; same overlap |
| Status quo (e2e only) | Zero new deps | Slow feedback; store/lib edge cases (401 purge, stale activeId) hard to isolate; failures indicate "something in the journey" not the unit |

## 5. Files that WOULD be added (proposed text — not created)

`frontend/vitest.config.ts`
```ts
import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

export default mergeConfig(
  viteConfig, // keeps the "@" alias and the React plugin
  defineConfig({
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      css: false,
      restoreMocks: true,
      coverage: {
        provider: "v8",
        include: ["src/**/*.{ts,tsx}"],
        exclude: ["src/**/*.test.*", "src/test/**", "src/main.tsx", "src/vite-env.d.ts"],
        reporter: ["text-summary", "lcov"],
      },
    },
  }),
);
```

`frontend/src/test/setup.ts`
```ts
import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {            // RTL only auto-cleans when vitest `globals` is true; we keep globals off
  cleanup();
  localStorage.clear();
  vi.useRealTimers();
});

// jsdom gaps this app touches (confirm which are really missing when implementing):
Element.prototype.scrollTo ??= () => {};            // ChatPanel auto-scroll
window.matchMedia ??= ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, onchange: null, dispatchEvent: () => false })) as any;
```

`frontend/package.json` (additions)
```json
"scripts": { "test": "vitest run", "test:watch": "vitest", "test:coverage": "vitest run --coverage" }
```

`frontend/tsconfig.json`: no change needed if tests import `describe/it/expect/vi` explicitly from `"vitest"` (recommended; no `globals`),
and the setup file's `import "@testing-library/jest-dom/vitest"` supplies matcher types (it lives under `src/`, so `tsc -b` sees it).
`vitest.config.ts` sits outside `include: ["src"]`, like `vite.config.ts` today.

Test location: co-located `src/**/<name>.test.ts(x)`; shared helpers in `src/test/`.

## 6. Coverage priorities (each tied to a real defect or invariant in THIS repo)

| P | Unit under test | Cases | Why |
|---|---|---|---|
| P0 | `store/auth.ts` `logout()` | clears `auth_token`/`auth_user`; resets `useChat` to `{conversations:[], activeId:null, messages:{}}`; second user starts clean | Cross-user leak on a shared tab (found in review, F1) |
| P0 | `store/auth.ts` init/`hydrate` | reads stored user synchronously; malformed JSON → `null` | Reload used to bounce to /login |
| P0 | `store/chat.ts` `selectConversation` | on API failure returns `false` and clears `activeId` only if it is still that id; success caches messages; cached id skips network | Stale-`activeId`-after-delete bug (F2) |
| P0 | `lib/sse.ts` `postSse` | `onopen` non-ok → throws `SseHttpError(status)`; 401 removes both storage keys and redirects unless already on /login or /register; adds `Authorization` only when a token exists | Expired session mid-stream (F3) |
| P0 | `lib/api.ts` | request interceptor adds Bearer; response interceptor 401 purge + redirect; `apiError` handles string, `[{msg}]`, missing | Auth error text bug fixed earlier |
| P1 | `ChatPanel` | maps `SseHttpError` 429/401/404/413/422/5xx and unknown to the exact messages; `persist_error` appends the notice; `clarification`/`error` events render; stop button aborts | Specific-message fix (F3) |
| P1 | `App` / `Protected` | no user → `/login`; user → children; hydration does not flash login | Protected-route behaviour |
| P1 | `ConfirmDialog` | initial focus on Cancel; Tab/Shift+Tab trapped; Esc cancels; backdrop click cancels; focus restored to trigger | A11y requirement in `DESIGN.md` §8 |
| P1 | `ConversationSidebar` | delete opens dialog, not `confirm()`; `select(...).then(ok=>!ok && nav("/"))` redirects on false | Delete-active-chat flow |
| P1 | `ChatInput` | Enter sends, Shift+Enter newline, IME composition (`isComposing`, keyCode 229) ignored; disabled when empty/busy; Stop shown when busy | Existing IME guard |
| P2 | `BlockRenderer` | each known `template_id` routes; unknown → `FallbackBlock` (collapsible JSON); empty/null → nothing | Documented invariant (README "adding a block type") |
| P2 | `DataTableBlock` | sort asc/desc (numeric vs text), `aria-sort`, URL cells render as `https://` links with `rel`, empty → "No rows" | |
| P2 | `InsightCardsBlock` | `"A: 1 · B: 2"` → label/value rows; plain segments; severity label+icon (not colour only) | Body-parsing logic |
| P2 | `MarkdownBlock` | table wrapped in scroll region; links get `target=_blank rel=noreferrer`; raw HTML/`javascript:` link not executed | XSS-surface regression guard |
| P2 | `CheckpointTrail`, `MessageBubble` | sources capped at 8 with `(opens in a new tab)`, follow-ups callback, `aria-busy` while streaming | |
| — | Map rendering, layout, contrast, drawer | **Out of scope for jsdom** — remains covered by `e2e/scenario.mjs` and the programmatic contrast scan | jsdom has no layout engine |

## 7. CI integration (implemented locally; the parent wires it into `.github/workflows/ci.yml`)

```yaml
  frontend:
    name: Frontend (TypeScript, build, tests + coverage, audit)
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    defaults:
      run:
        working-directory: frontend
    steps:
      - uses: actions/checkout@<pinned-sha>
        with: { persist-credentials: false }
      - uses: actions/setup-node@<pinned-sha>
        with: { node-version: "22", cache: npm, cache-dependency-path: frontend/package-lock.json }   # jest-dom 6.10 needs Node >= 22
      - run: npm ci
      - run: npx tsc --noEmit
      - run: npx vite build
      - name: Unit tests with enforced coverage (statements/lines/functions 70, branches 60)
        run: npm run test:coverage
      - name: Dependency audit (production deps, fail on high/critical)
        run: npm audit --omit=dev --audit-level=high
```

Thresholds are enforced by `vitest.config.ts` (global: statements 70, lines 70, functions 70, branches 60) and fail the job when unmet. Measured today: 100 / 100 / 100 / 95.62.
Do not chase 100 %: when adding a feature, add the behavioural test first and let the number follow.

## 8. Risks and costs

- **jsdom fidelity:** no layout, partial focus/scroll APIs → some a11y behaviours (focus order across the drawer, sticky headers) cannot be asserted; do not treat a green unit run as a UI sign-off.
- **Mock drift:** `vi.mock("@/lib/api")` can hide interceptor bugs; mitigated by testing `api.ts` with the real axios instance + stub adapter.
- **Framework churn:** the Vitest/Vite/jsdom/jest-dom majors move together and floors rise (Node ≥ 22 already); Vite 5 is the binding constraint — upgrading Vite later is a prerequisite for Vitest ≥ 4.
- **Maintenance:** +151 dev packages to audit/update (Dependabot/CI audit should cover them); roughly a handful of test files per feature area to keep in step with the code.
- **Flakiness:** timers/animation (framer-motion), `scrollTo`, `ResizeObserver` — handled in `setup.ts`; keep tests free of real timers/network.
- **Not a replacement for e2e:** the real-stack browser harness stays.

## 9. Staged implementation (each stage ends green: `tsc`, `vite build`, and the stage's tests)

| Stage | Change | Exit criteria (repo green) |
|---|---|---|
| 0 | User approval (§10) | decisions recorded |
| 1 | Add the 7 dev deps, `vitest.config.ts`, `src/test/setup.ts`, `test` scripts, **one** smoke test (e.g. `BlockRenderer` fallback); CI job runs it | `npm ci && npx tsc --noEmit && npm test && npx vite build` all pass; `npm audit` reviewed |
| 2 | P0 tests: `auth`, `chat`, `sse`, `api` (+ the fetch-vs-mock spike) | all pass; mutating each guarded line (e.g. remove the `useChat.setState` in `logout`) makes a test fail — verify by deliberate regression |
| 3 | P1 tests: `ChatPanel`, `ConfirmDialog`, `ConversationSidebar`, `ChatInput`, `App` | all pass |
| 4 | P2 block/leaf components; enable coverage thresholds measured from the real numbers | gate passes with the chosen margin |
| 5 | Update `TEST_PLAN.md` (new row, counts), `AGENTS.md` commands, `README.md` | docs match reality |

No stage leaves a half-migrated state: Stage 1 is additive (tests are not part of `vite build`), and every stage can be reverted by removing the added files/deps.

## 10. Decisions (resolved)

1. Add the 7 devDependencies: **approved and done** (exact versions above).
2. CI coverage gate: **approved** with global thresholds 70/70/70 and branches 60 (not path-restricted, as the measured suite far exceeds them).
3. Test location: **co-located** `src/**/*.test.ts(x)`, shared helpers in `src/test/`.
4. CI Node >= 22: **approved** (snippet in section 7).
5. DOM-only behaviour (maps, layout) stays covered by the browser e2e: see "Not covered" above.
