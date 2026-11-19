import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// npm run build writes dist/, which the console's Python server serves (and git keeps, so it runs without Node)
export default defineConfig({ plugins: [react(), tailwindcss()] });
