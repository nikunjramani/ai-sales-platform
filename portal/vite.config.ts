/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The portal is served under /portal/ by the API (SALES_PORTAL_DIST_DIR) or by the Vite dev
// server, which forwards /api to the local API so cookies stay same-origin.
export default defineConfig({
  base: "/portal/",
  plugins: [react()],
  // "@/features/..." means "src/features/..." (same mapping as tsconfig.json paths).
  resolve: { alias: [{ find: /^@\//, replacement: "/src/" }] },
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["tests/**/*.test.{ts,tsx}"],
    setupFiles: ["./tests/setup.ts"],
  },
});
