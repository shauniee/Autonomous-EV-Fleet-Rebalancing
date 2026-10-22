const { chromium } = require('@playwright/test');

const baseURL = process.argv[2] || 'http://127.0.0.1:8080';

async function json(response) {
  if (!response.ok()) {
    throw new Error(`${response.request().method()} ${response.url()} returned ${response.status()}`);
  }
  return response.json();
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const browserErrors = [];
    page.on('pageerror', error => browserErrors.push(error.message));
    // Keep deployment verification deterministic and independent of public tiles.
    await page.route('https://tile.openstreetmap.org/**', route => route.fulfill({
      contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"></svg>',
    }));

    await page.goto(baseURL, { waitUntil: 'domcontentloaded' });
    await page.getByRole('button', { name: 'CBD CRITICAL' }).click();

    const recommendationResponse = page.waitForResponse(response =>
      response.url().includes('/api/rebalancing/recommendations')
      && response.request().method() === 'POST');
    await page.getByRole('button', { name: 'Recommend EV', exact: true }).click();
    const recommendation = await json(await recommendationResponse);
    await page.getByTestId('recommendation').waitFor({ state: 'visible' });
    if (!recommendation.selected_vehicle_id) throw new Error('No EV was recommended');

    const vehicleURL = `${baseURL}/api/fleet/vehicles/${recommendation.selected_vehicle_id}`;
    const before = await json(await page.request.get(vehicleURL));

    await page.getByRole('button', { name: 'Review dispatch' }).click();
    await page.getByRole('dialog').waitFor({ state: 'visible' });
    const approvalResponse = page.waitForResponse(response =>
      response.url().includes('/api/dispatch/dispatches')
      && response.request().method() === 'POST');
    await page.getByRole('button', { name: 'Approve Dispatch', exact: true }).click();
    const approved = await json(await approvalResponse);

    const card = page.locator('[data-testid^="dispatch-"]').first();
    await card.waitFor({ state: 'visible' });
    const states = [];
    const positions = new Set();
    const deadline = Date.now() + 90000;
    let dispatch;
    let vehicle;

    while (Date.now() < deadline) {
      dispatch = await json(await page.request.get(
        `${baseURL}/api/dispatch/dispatches/${approved.dispatch_id}`));
      vehicle = await json(await page.request.get(vehicleURL));
      if (!states.includes(dispatch.state)) states.push(dispatch.state);
      if (vehicle.telemetry) positions.add(JSON.stringify(vehicle.telemetry.position));
      if (dispatch.state === 'SUCCEEDED' && vehicle.effective_availability === 'AVAILABLE') break;
      await page.waitForTimeout(300);
    }

    if (!dispatch || !vehicle || dispatch.state !== 'SUCCEEDED') {
      throw new Error(`Journey did not succeed: ${JSON.stringify(dispatch)}`);
    }

    await page.getByText('SUCCEEDED', { exact: true }).first().waitFor({
      state: 'visible', timeout: 10000,
    });
    const vehicleText = await page.getByTestId(
      `vehicle-${recommendation.selected_vehicle_id}`).innerText();
    const cardText = await card.innerText();
    const result = {
      target: 'cbd',
      selected_vehicle_id: recommendation.selected_vehicle_id,
      recommendation_id: recommendation.recommendation_id,
      dispatch_id: approved.dispatch_id,
      observed_dispatch_states: states,
      unique_positions: positions.size,
      start_soc_pct: before.telemetry.soc_pct,
      arrival_soc_pct: vehicle.telemetry.soc_pct,
      final_zone: vehicle.current_zone_id,
      final_availability: vehicle.effective_availability,
      active_claim: vehicle.active_claim,
      dispatch_claim_held: dispatch.claim_held,
      ui_dispatch_succeeded: cardText.includes('SUCCEEDED'),
      ui_vehicle_shows_cbd: vehicleText.toLowerCase().includes('cbd'),
      ui_vehicle_shows_available: vehicleText.includes('AVAILABLE'),
      browser_errors: browserErrors,
    };

    const accepted = states.includes('IN_PROGRESS')
      && positions.size >= 3
      && vehicle.telemetry.soc_pct < before.telemetry.soc_pct
      && vehicle.current_zone_id === 'cbd'
      && vehicle.active_claim === null
      && dispatch.claim_held === false
      && result.ui_dispatch_succeeded
      && result.ui_vehicle_shows_cbd
      && result.ui_vehicle_shows_available
      && browserErrors.length === 0;
    if (!accepted) throw new Error(`Acceptance assertion failed: ${JSON.stringify(result)}`);
    console.log(JSON.stringify(result, null, 2));
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});

