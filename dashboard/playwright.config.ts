import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './e2e', fullyParallel: false, workers: 1, timeout: 90000,
  expect: { timeout: 10000 }, reporter: [['list'], ['html', { open: 'never' }], ['junit', { outputFile: '../docs/evidence/operator-dashboard-browser-tests.xml' }]],
  use: { baseURL: 'http://127.0.0.1:15173', browserName: 'chromium', trace: 'retain-on-failure' },
  webServer: [
    { command: `"${process.platform === 'win32' ? '.venv\\Scripts\\python.exe' : '.venv/bin/python'}" scripts/dashboard_test_server.py`, cwd: '..', url: 'http://127.0.0.1:18005', timeout: 90000, reuseExistingServer: false },
    { command: 'npm run dev -- --port 15173', url: 'http://127.0.0.1:15173', reuseExistingServer: false,
      env: { FLEET_URL: 'http://127.0.0.1:18001', DEMAND_URL: 'http://127.0.0.1:18002', REBALANCING_URL: 'http://127.0.0.1:18003', DISPATCH_URL: 'http://127.0.0.1:18004', FACILITIES_URL: 'http://127.0.0.1:18006' } },
  ],
});
