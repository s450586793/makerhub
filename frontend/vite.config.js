import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules/three")) {
            return undefined;
          }
          if (id.includes("examples/jsm/loaders")) {
            return "three-loaders";
          }
          if (id.includes("examples/jsm/controls")) {
            return "three-controls";
          }
          return "three-core";
        },
      },
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
  },
});
