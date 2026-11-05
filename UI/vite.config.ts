import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

const defaultApiTarget = 'http://127.0.0.1:8010';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_');
  let target = defaultApiTarget;
  if (env.VITE_API_BASE_URL && !env.VITE_API_BASE_URL.startsWith('/')) {
    try {
      target = new URL(env.VITE_API_BASE_URL).origin;
    } catch {
      throw new Error('VITE_API_BASE_URL must be a backend URL or a relative API path.');
    }
  }
  return {
    plugins: [react()],
    server: {
      proxy: {
        '/api': {
          target,
          changeOrigin: true,
          rewrite: path => path.replace(/^\/api/, ''),
        },
      },
    },
  };
});
