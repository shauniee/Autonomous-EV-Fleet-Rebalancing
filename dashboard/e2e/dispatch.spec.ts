import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  if (process.env.REAL_MAP === '1') return;
  // CI must never load public map tiles. Map controls/markers remain real Leaflet.
  await page.route('https://tile.openstreetmap.org/**', route => route.fulfill({
    contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#e4ede6"/><path d="M0 120H256M120 0V256" stroke="#d4dfd7"/></svg>',
  }));
});
test('select hotspot, recommend, explicitly approve, observe real relocation and arrival', async ({ page, request }) => {
  const browserErrors: string[] = [];
  page.on('pageerror', error => browserErrors.push(error.message));
  let loadedTiles = 0;
  page.on('response', response => { if (response.url().includes('tile.openstreetmap.org') && response.ok()) loadedTiles++; });
  await page.goto('/');
  await page.getByRole('button', { name: 'CBD CRITICAL' }).click();
  await page.getByRole('button', { name: 'Recommend EV', exact: true }).click();
  await expect(page.getByTestId('recommendation')).toBeVisible();
  const before = await (await request.get('/api/fleet/vehicles')).json();
  expect((await (await request.get('/api/dispatch/dispatches')).json()).dispatches).toHaveLength(0);
  await page.getByRole('button', { name: 'Review dispatch' }).click();
  await expect(page.getByRole('dialog')).toContainText('Predicted arrival SOC');
  await page.screenshot({ path: 'test-results/dashboard-confirmation.png', fullPage: true });
  await page.getByRole('button', { name: 'Approve 1 EV', exact: true }).click();
  const card = page.locator('[data-testid^="dispatch-"]').first();
  await expect(card).toBeVisible();
  await expect(card).toContainText('IN PROGRESS');
  const active = (await (await request.get('/api/dispatch/dispatches')).json()).dispatches[0];
  const original = before.vehicles.find((v: { vehicle_id: string }) => v.vehicle_id === active.vehicle_id);
  const samples: { latitude: number; longitude: number }[] = [];
  await expect.poll(async () => {
    const current = await (await request.get(`/api/fleet/vehicles/${active.vehicle_id}`)).json();
    samples.push(current.telemetry.position);
    return current.telemetry.soc_pct;
  }).toBeLessThan(original.telemetry.soc_pct);
  expect(samples.at(-1)).not.toEqual(original.telemetry.position);
  expect(samples.at(-1)).not.toEqual(active.command.target_position);
  await expect(card).toContainText('SUCCEEDED', { timeout: 60000 });
  await expect(page.getByTestId(`vehicle-${active.vehicle_id}`)).toContainText('cbd');
  await expect(page.getByTestId(`vehicle-${active.vehicle_id}`)).toContainText('AVAILABLE');
  const arrived = await (await request.get(`/api/fleet/vehicles/${active.vehicle_id}`)).json();
  expect(arrived.active_claim).toBeNull();
  expect(arrived.telemetry.position).not.toEqual(original.telemetry.position);
  expect(arrived.telemetry.soc_pct).toBeLessThan(original.telemetry.soc_pct);
  expect(active.acknowledged_at).not.toBeNull();
  await page.screenshot({ path: 'test-results/dashboard-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => {
    const map = document.querySelector('.map')!.getBoundingClientRect();
    return [...document.querySelectorAll('.map path.leaflet-interactive')].every(path => {
      const point = path.getBoundingClientRect();
      return point.left >= map.left && point.right <= map.right && point.top >= map.top && point.bottom <= map.bottom;
    });
  })).toBe(true);
  await page.screenshot({ path: 'test-results/dashboard-mobile.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(browserErrors).toEqual([]);
  if (process.env.REAL_MAP === '1') expect(loadedTiles).toBeGreaterThan(0);
});
test('tile and Demand failures preserve working Fleet and dispatch panels', async ({ page }) => {
  await page.route('https://tile.openstreetmap.org/**', route => route.abort());
  await page.route('**/api/demand/heat-map', route => route.fulfill({ status: 503, body: '{}' }));
  await page.goto('/');
  await expect(page.getByText('Map tiles unavailable.', { exact: false })).toBeVisible();
  await expect(page.getByText('Demand · unavailable', { exact: false })).toBeVisible();
  await expect(page.getByTestId('vehicle-ev-browser-01')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Relocations', exact: true })).toBeVisible();
});
