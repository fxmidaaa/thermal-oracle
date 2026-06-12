import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Прокси решает CORS в dev: у бэкенда нет CORS-middleware, и он ему не нужен —
// фронт ходит на свой origin. THERMAL_API_TARGET переопределяется при запуске
// dev-сервера в докере (http://api:8000 внутри сети infra_default).
const target = process.env.THERMAL_API_TARGET ?? "http://127.0.0.1:8000";
const proxy = { "/api": { target, changeOrigin: true } };

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy, host: true },
  preview: { proxy, host: true },
});
