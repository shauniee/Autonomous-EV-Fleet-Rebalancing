import { defineConfig } from 'vitest/config';

export default defineConfig({
  resolve: {
    // Tests also exercise the sibling simulation-console UI. Keep a single
    // React instance when source files live outside the dashboard package.
    dedupe: ['react', 'react-dom'],
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['tests/setup.ts'],
    include: ['tests/**/*.test.ts*'],
    // Windows/Docker Desktop laptops can time out starting several forked
    // jsdom workers simultaneously. Use one isolated worker thread so test
    // startup stays bounded without changing any test assertions.
    pool: 'threads',
    fileParallelism: false,
  },
});
