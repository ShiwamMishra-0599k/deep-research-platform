import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  // RTL only auto-cleans when vitest `globals` is on; we keep globals off and clean explicitly.
  cleanup();
  localStorage.clear();
  vi.useRealTimers();
});

// The only jsdom gap this app touches (verified: matchMedia/ResizeObserver are not used by app code):
// ChatPanel auto-scrolls with Element.scrollTo, which jsdom does not implement.
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollTo() {};
}
