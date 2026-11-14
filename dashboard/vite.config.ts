import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), ''), ...process.env };
  return {
    server: {
      strictPort: true,
      // Dev only: forwards every route; the internal-route 403s live in nginx.conf and k8s/configmaps.yaml.
      proxy: Object.fromEntries(['fleet', 'demand', 'rebalancing', 'dispatch', 'facilities'].map((service, i) => [
        `/api/${service}`, {
          target: env[`${service.toUpperCase()}_URL`] || `http://127.0.0.1:${8001 + i}`,
          rewrite: (path: string) => path.replace(`/api/${service}`, '/api/v1'),
        },
      ])),
    },
  };
});
