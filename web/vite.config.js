import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built straight into the Python package so `uvx gmail-unsub --ui` serves the
// UI with no separate build step for the user.
export default defineConfig({
  plugins: [react()],
  base: "/assets/",
  build: {
    outDir: "../gmail_unsub/server/static",
    emptyOutDir: true,
    assetsDir: "",
  },
});
