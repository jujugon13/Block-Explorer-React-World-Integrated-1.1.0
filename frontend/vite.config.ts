import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const target = process.env.VITE_BACKEND_URL || "http://127.0.0.1:8080";
const paths = ["/api", "/auth", "/departments", "/roles", "/collections", "/permissions", "/admin", "/mcp"];

export default defineConfig({
  plugins: [react()],
  server: {
    host: "localhost",
    port: 5173,
    strictPort: true,
    proxy: {
      ...Object.fromEntries(paths.map((path) => [path, { target, changeOrigin: true }])),
      "/ws": { target, changeOrigin: true, ws: true },
    },
  },
  preview: { host: "localhost", port: 5173, strictPort: true },
});
