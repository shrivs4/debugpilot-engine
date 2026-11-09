/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The browser talks to /api on the same origin; Vite forwards it to FastAPI.
// In Docker the target is http://api:8000, locally http://localhost:8000.
const apiTarget = process.env.API_PROXY_TARGET ?? "http://localhost:8000";
const proxy = {
  "/api": {
    target: apiTarget,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, ""),
  },
};

export default defineConfig({
  plugins: [react()],
  server: { host: "0.0.0.0", port: 5173, proxy },
  preview: { host: "0.0.0.0", port: 5173, proxy },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
