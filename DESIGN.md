# Lumen — Design System

A calm, readable research workspace. Warm ivory surfaces, a forest-charcoal frame, and one muted-teal accent. No gradients, glows, glass or decorative animation.

**Source of truth:** `frontend/src/styles/tokens.css`. `frontend/tailwind.config.ts` only *references* those variables (the Tailwind palette is replaced, not extended, so off-palette colours cannot be used). Change a value in `tokens.css` and every component follows.

## 1. The 60 / 30 / 10 distribution

| Share | Role | Colour | Where it appears |
|---|---|---|---|
| ~60% | Dominant | Warm Ivory `#F5F4EF` (+ `#FCFBF8` surface, `#ECEAE3` sunken) | App background, conversation, cards, tables, inputs |
| ~30% | Secondary | Deep Forest Charcoal `#26332F` (+ raised `#31403B`) | Sidebar, auth brand panel, logo mark |
| ~10% | Accent | Muted Teal `#568477` family | Primary buttons, active/selected state, links, focus rings, map pins, progress ticks |

Semantic success / warning / danger colours exist but are used sparingly (insight-card severity, error messages, the destructive button).

### Why the teal has four values
The proposed `#568477` fails WCAG AA for text and button labels (white on it is 4.23:1; it is 3.84:1 as link text on ivory; 3.11:1 on charcoal). It stays the **brand** colour for non-text uses (icons, rings, borders, pins, bars) and gains darker/lighter siblings for text:

| Token | Hex | Use | Contrast |
|---|---|---|---|
| `accent` | `#568477` | Icons, borders, pins, bars (non-text) | 3.84:1 on canvas (≥3:1 non-text) |
| `accent-strong` | `#4A786A` | Button fill, links on ivory | white on it 4.85:1; link on canvas 4.56:1 |
| `accent-deep` | `#3F6B5E` | Hover, accent text on tint | 5.84:1 on surface |
| `accent-tint` | `#E4ECE7` | Subtle selected background | — |
| `accent-on-dark` | `#8DBBAA` | Accent on charcoal (focus ring, active marker, logo ring) | 6.15:1 on nav |

## 2. Colour tokens

| Token (`--c-*`) | Hex | Notes |
|---|---|---|
| `canvas` | `#F5F4EF` | Page background |
| `surface` | `#FCFBF8` | Cards, inputs, tables |
| `sunken` | `#ECEAE3` | Hover, table header, user message, code |
| `nav` | `#26332F` | Sidebar |
| `nav-raised` | `#31403B` | Sidebar hover / selected |
| `nav-line` | `#3E4E48` | Sidebar dividers (decorative) |
| `nav-text` | `#F5F4EF` | 11.94:1 on nav |
| `nav-muted` | `#A9B5AE` | 6.20:1 on nav |
| `ink` | `#26332F` | Primary text, 11.94:1 on canvas |
| `muted` | `#5F6962` | Secondary text, 5.18:1 on canvas (the proposed `#737C75` was 3.92:1) |
| `placeholder` | `#69736C` | 4.75:1 on surface |
| `line` | `#DDE2DB` | Decorative borders and dividers |
| `line-strong` | `#7F8A82` | Borders of interactive controls, 3.46:1 on surface (≥3:1) |
| `success` / `-tint` | `#276B43` / `#E3EFE6` | |
| `warning` / `-tint` | `#8A5210` / `#F5EBDA` | |
| `danger` / `-tint` | `#B3392F` / `#F6E4E1` | |
| `danger-on-dark` | `#F19A90` | Destructive hover on charcoal |

Two literals live outside `tokens.css` because they cannot use CSS variables: the `theme-color` meta tag and `public/favicon.svg`. Both use `#F5F4EF` / `#26332F` / `#8DBBAA`. `white` is reserved for text on accent/danger fills.

## 3. Typography

One family: **Inter** (400 / 500 / 600 only), system fallback `ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto`. Monospace is the system stack and appears only for code.

| Token | Size / line | Use |
|---|---|---|
| `text-xs` | 12 / 16 | Metadata, section labels, timestamps |
| `text-sm` | 14 / 20 | UI text, labels, table cells, buttons (app default) |
| `text-base` | 16 / 26 | Reading text: answers, user messages, input |
| `text-lg` | 18 / 28 | Section headings, dialog titles |
| `text-xl` | 24 / 32 | Page headings |

## 4. Spacing, radius, shadow, layout

- **Spacing:** Tailwind's 4px scale. Rhythm: 8px within a component, 12–16px between components, 20px between answer blocks, 32px between messages.
- **Radius (two steps):** `control` 6px (buttons, inputs, chips, menu items) and `card` 8px (cards, tables, maps, dialogs, message surfaces). `full` is only for the user avatar and a 4px bullet.
- **Shadow:** `sm` `0 1px 2px rgb(38 51 47 / .06)` (map controls, badge) and `md` `0 4px 16px rgb(38 51 47 / .10), 0 1px 3px rgb(38 51 47 / .06)` (dialog, popups). Borders carry most separation; cards have no shadow.
- **Layout:** sidebar 280px (`w-sidebar`), reading column 46rem (`max-w-content`), header 56px (`h-header`).

## 5. Breakpoints

| Name | Min width | Behaviour |
|---|---|---|
| (base) | 0 | Single column; sidebar is an off-canvas drawer opened from the header menu button |
| `sm` | 640px | Two-column suggestions and insight cards; larger gutters; map taller |
| `md` | 768px | Three-column insight cards |
| `lg` | 1024px | Sidebar is persistent; auth brand panel appears |
| `xl` | 1280px | Wider auth brand panel |

## 6. Components

All defined in `frontend/src/index.css` (`@layer components`) and used consistently.

- **Buttons:** `btn-primary` (teal fill, white text), `btn-secondary` (surface + strong border), `btn-ghost`, `btn-danger` (used only in the delete dialog), `btn-icon` (36px square). Height 40px (32px for follow-up chips). Disabled = 50% opacity, no pointer events.
- **Inputs:** `.field` + `.label`; 40px high, strong border, teal border + 1px ring on focus. Errors use `.field-error` with `role="alert"`.
- **Cards:** `.card` = surface + 1px `line` border + 8px radius. No nested card-in-card.
- **Navigation:** charcoal sidebar; the active conversation has a raised background and a 2px `accent-on-dark` left marker, plus `aria-current="page"`. Delete appears on hover/focus (always visible on touch).
- **Dialog:** `ConfirmDialog` (`role="alertdialog"`, `aria-modal`). Focus starts on Cancel, Tab is trapped, Esc and backdrop click cancel, focus returns to the trigger. Replaces the browser `confirm()`.
- **Data table:** sticky sunken header, real `<button>` sort controls with `aria-sort`, no zebra striping, tabular numerals, scrolls inside its own focusable region.
- **Insight cards:** neutral card; severity shown as icon + label + colour (never colour alone); "Label: value" bodies render as aligned rows.
- **Map:** light OpenStreetMap tiles (no CSS filter), flat teal pin, 8px radius, `isolate` so Leaflet's internal z-index values never cover the drawer or dialogs.
- **Markdown:** 16px / 26px, headings 24 / 18 / 16, comfortable list and blockquote spacing; wide tables scroll within a bordered region.
- **Progress:** a quiet bordered list, one muted line per step, a single spinner on the current step, elapsed time as metadata. No shimmer, glow or badges.

## 7. Motion

- Framer Motion is used only for a 150ms opacity fade on new messages and progress steps. No hover lifts, no staggered entrances, no animated backgrounds.
- `MotionConfig reducedMotion="user"` plus a global `prefers-reduced-motion` rule collapse all animation and transition durations.

## 8. Accessibility

- Visible focus on every interactive element: 2px teal outline (light teal on charcoal), 2px offset. The chat input shows focus via a ring on its wrapper.
- Semantic structure: one `h1` per screen, `main`, labelled `nav` / `aside` / `section` regions, `role="log"` for the conversation, `aria-live="polite"` for research progress, `aria-busy` while an answer streams.
- Skip link ("Skip to conversation"); mobile drawer closes with Esc and with its backdrop.
- Every icon-only button has an `aria-label`; decorative icons are `aria-hidden`. Form labels are tied to inputs with `htmlFor`.
- External links announce "(opens in a new tab)" to screen readers.

## 9. Verification (final implementation)

- **Contrast:** an automated scan computed the real foreground/background ratio of every visible text node on login, register, welcome, chat (top / middle / end) and the delete dialog. **0 failures** against WCAG AA (4.5:1, or 3:1 for large text); lowest measured ratio 4.56:1.
- **Responsive:** no horizontal page overflow at 320, 360, 390, 768, 1024 and 1280px on login, register, welcome and a conversation containing every block type.
- **Interaction states tested:** hover, keyboard focus, delete dialog (focus trap, Esc, focus return), mobile drawer (open, Esc to close), streaming progress, stop button, clarification, error, sign-in error, empty conversation, unknown-block fallback, reduced motion.
- **Build:** `tsc --noEmit` clean; `vite build` succeeds.
- **Not covered:** dark mode (the product is light-only by design), real-backend end-to-end, and testing on physical devices or Safari/Firefox (verified in Chromium-based Edge).
