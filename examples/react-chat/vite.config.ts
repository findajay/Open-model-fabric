import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: { "/api": process.env.OMF_API_URL ?? "http://127.0.0.1:8080" },
  },
});
