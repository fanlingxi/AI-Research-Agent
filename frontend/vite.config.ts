import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      // The local API workspace uses 8010. Developers can still override this
      // for another environment with VITE_API_PROXY_TARGET.
      "/api": process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8010",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: true,
  },
});
