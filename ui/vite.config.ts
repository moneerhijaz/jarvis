import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The UI calls the backend with relative /api paths. In dev, Vite proxies them
// to jarvisd (so there's no CORS and SSE streams cleanly). In production the
// backend serves the built UI from the same origin, so /api also just works.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true,
      },
    },
  },
  build: { outDir: "dist" },
});
