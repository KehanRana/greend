import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { loadEnv } from 'vite';
export default defineConfig({
  base:'/dashboard/', plugins:[react()],
  server:{proxy:{'/api':{target:loadEnv('development','.','GREENROUTE').GREENROUTE_API_ORIGIN || 'http://127.0.0.1:8000',changeOrigin:false}}},
  test:{environment:'jsdom',setupFiles:['./src/test-setup.ts'],include:['src/**/*.test.tsx','src/**/*.test.ts']}
});
