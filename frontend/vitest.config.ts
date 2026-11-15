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
        // Only what cannot be a unit under test: entry point, ambient types, shared test helpers.
        exclude: ["src/**/*.test.*", "src/test/**", "src/main.tsx", "src/vite-env.d.ts"],
        reporter: ["text-summary", "text", "lcov"],
        // Enforced minimums (global). They are floors, not goals: behaviour matters more than the number.
        thresholds: { statements: 70, lines: 70, functions: 70, branches: 60 },
      },
    },
  }),
);
