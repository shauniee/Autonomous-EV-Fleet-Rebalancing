import { useCallback, useEffect, useRef, useState } from 'react';
import { applyTheme, type Theme } from './theme';
import type { Approval, BatchApproval, BatchDispatch, BatchRecommendation, ChargerAdvice, ChargingAdvice, TrafficSnapshot, Dispatch, DispatchList, FacilitiesSnapshot, Fleet, HeatMap, Recommendation } from './api/contracts';
import { api, ApiError, message } from './api/client';
import { usePolling } from './hooks/usePolling';
import { OperationalTabs } from './components/OperationalTabs';
import { SingaporeMap } from './components/SingaporeMap';
import { HotspotToasts } from './components/HotspotToasts';
import { ChargingOperations } from './components/ChargingOperations';
import { TrafficConditions } from './components/TrafficConditions';
import { TrafficJourneyCheck } from './components/TrafficJourneyCheck';
import { TwinHistoryPanel } from './components/TwinHistoryPanel';
import { RentalOutlook } from './components/RentalOutlook';
import { FacilitiesPanel } from './components/FacilitiesPanel';
import { BatchPanel, BatchConfirmation, BatchResult } from './components/BatchPanel';
import { Badge, time } from './components/format';
import { DispatchConfirmation, FleetPanel, RecommendationPanel, RelocationPanel, ServiceStatus, ZoneDetails } from './components/Panels';

const pendingKey = 'fleet.pending-approval';
const batchPendingKey = 'fleet.pending-batch';
function readPending(): Approval | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(pendingKey) || 'null');
    return value && typeof value.request_id === 'string' && typeof value.recommendation_id === 'string' ? value : null;
  } catch { return null; }
}
function readPendingBatch(): BatchApproval | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(batchPendingKey) || 'null');
    return value && typeof value.request_id === 'string' && typeof value.batch_recommendation_id === 'string' ? value : null;
  } catch { return null; }
}
export default function App() {
  const [theme, setTheme] = useState<Theme>(() => document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
  const heat = usePolling<HeatMap>('/api/demand/heat-map');
  const fleet = usePolling<Fleet>('/api/fleet/vehicles', 1000);
  const dispatches = usePolling<DispatchList>('/api/dispatch/dispatches');
  const facilities = usePolling<FacilitiesSnapshot>('/api/facilities/facilities', 10000);
  const chargingAdvice = usePolling<ChargingAdvice>('/api/rebalancing/charging-advice', 10000);
  const traffic = usePolling<TrafficSnapshot>('/api/rebalancing/traffic/conditions', 5000);
  const [selected, setSelected] = useState('');
  const [selectedDispatch, setSelectedDispatch] = useState('');
  const mapPanel = useRef<HTMLElement>(null);
  const inspector = useRef<HTMLElement>(null);
  const [recommendation, setRecommendation] = useState<Recommendation | null>(null);
  const [quantity, setQuantity] = useState(1);
  const [purpose, setPurpose] = useState<'REBALANCE' | 'CHARGE'>('REBALANCE');
  const [selectedVehicle, setSelectedVehicle] = useState('');
  const [selectedFacility, setSelectedFacility] = useState('');
  const [batch, setBatch] = useState<BatchRecommendation | null>(null);
  const [batchConfirming, setBatchConfirming] = useState(false);
  const [batchResult, setBatchResult] = useState<BatchDispatch | null>(null);
  const [pendingBatch, setPendingBatchState] = useState<BatchApproval | null>(readPendingBatch);
  const [recommending, setRecommending] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [pending, setPendingState] = useState<Approval | null>(readPending);
  const [submitting, setSubmitting] = useState(false);
  const [now, setNow] = useState(Date.now());
  const recommendationRequest = useRef<AbortController | null>(null);
  const submittingRef = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    const timer = setInterval(() => setNow(Date.now()), 500);
    return () => { mounted.current = false; clearInterval(timer); recommendationRequest.current?.abort(); };
  }, []);
  const setPending = useCallback((approval: Approval | null) => {
    setPendingState(approval);
    try { if (approval) sessionStorage.setItem(pendingKey, JSON.stringify(approval)); else sessionStorage.removeItem(pendingKey); }
    catch { /* Memory still protects retries when browser storage is disabled. */ }
  }, []);
  const setPendingBatch = useCallback((value: BatchApproval | null) => {
    setPendingBatchState(value);
    try { if (value) sessionStorage.setItem(batchPendingKey, JSON.stringify(value)); else sessionStorage.removeItem(batchPendingKey); }
    catch { /* In-memory retry still works when browser storage is blocked. */ }
  }, []);
  useEffect(() => {
    const matching = pending && dispatches.data?.dispatches.find(d => d.request_id === pending.request_id);
    if (matching && matching.state !== 'UNKNOWN') {
      setPending(null); setRecommendation(null); setConfirming(false);
      if (matching.state === 'REJECTED' || matching.state === 'FAILED') {
        setNotice('');
        setError(`Dispatch ${matching.dispatch_id}: ${matching.state} — ${matching.failure_reason || 'Approval unsuccessful'}.`);
      } else {
        setNotice(`Dispatch ${matching.dispatch_id}: ${matching.state}. Follow its backend state in Relocations.`);
        setError('');
      }
    }
  }, [dispatches.data, pending, setPending]);
  const choose = useCallback((id: string) => {
    recommendationRequest.current?.abort(); setRecommending(false);
    setSelected(id); setSelectedFacility(''); setRecommendation(null); setBatch(null); setBatchResult(null); setConfirming(false); setBatchConfirming(false); setError('');
  }, []);
  const zones = heat.data?.zones ?? [];
  const vehicles = fleet.data?.vehicles ?? [];
  const twinVehicle = vehicles.find(v => v.vehicle_id === selectedVehicle) ||
    vehicles.find(v => v.vehicle_id === dispatches.data?.dispatches.find(d => d.dispatch_id === selectedDispatch)?.vehicle_id) || vehicles[0];
  const sites = facilities.data?.facilities ?? [];
  const liveFacilities = facilities.data?.data_source === 'LTA_DATAMALL';
  // An unknown provider must not fall back to a zone-only destination.
  const facilityWorkflow = facilities.data?.data_source !== 'MOCK' || quantity > 1 || purpose === 'CHARGE' || !!selectedVehicle;
  const zone = zones.find(z => z.zone_id === selected);
  const current = recommendation && vehicles.find(v => v.vehicle_id === recommendation.selected_vehicle_id);
  const fleetFeedHealthy = !!fleet.updatedAt && !fleet.error && now - Date.parse(fleet.updatedAt) < 4500;
  const fresh = !!heat.data && !heat.error && !!fleet.data && !fleet.error && heat.data.fleet_status === 'FRESH'
    && !!heat.updatedAt && now - Date.parse(heat.updatedAt) < 10000 && !!fleet.updatedAt && now - Date.parse(fleet.updatedAt) < 10000;
  const remaining = recommendation ? Math.max(0, Math.ceil((Date.parse(recommendation.expires_at) - now) / 1000)) : 0;
  const batchRemaining = batch ? Math.max(0, Math.ceil((Date.parse(batch.expires_at) - now) / 1000)) : 0;
  const facilitiesFresh = !!facilities.data && !facilities.error && !!facilities.updatedAt
    && now - Date.parse(facilities.updatedAt) < 30000;
  const chargingAdviceFresh = !!chargingAdvice.data && !chargingAdvice.error && !!chargingAdvice.updatedAt
    && now - Date.parse(chargingAdvice.updatedAt) < 30000 && now - Date.parse(chargingAdvice.data.generated_at) < 30000;
  const approvalBlocked = !fresh || !current || current.stale || !!pending || !!pendingBatch || submitting || remaining <= 0;
  const batchBlocked = !fresh || !facilitiesFresh || !!pending || !!pendingBatch || submitting || batchRemaining <= 0;
  async function recommend() {
    recommendationRequest.current?.abort();
    const controller = new AbortController(); recommendationRequest.current = controller;
    setRecommending(true); setRecommendation(null); setBatch(null); setBatchResult(null); setError(''); setNotice('');
    try {
      if (!facilityWorkflow) {
        const result = await api<Recommendation>('/api/rebalancing/recommendations', {
          method: 'POST', body: JSON.stringify({ target_zone_id: selected }), signal: controller.signal,
        });
        if (!controller.signal.aborted) setRecommendation(result);
      } else {
        const result = await api<BatchRecommendation>('/api/rebalancing/batch-recommendations', {
          method: 'POST', body: JSON.stringify({ target_zone_id: selected, quantity,
            ...(purpose === 'CHARGE' ? { purpose } : {}),
            ...(selectedVehicle ? { vehicle_id: selectedVehicle } : {}),
            ...(purpose === 'CHARGE' && selectedFacility ? { facility_id: selectedFacility } : {}) }), signal: controller.signal,
        });
        if (!controller.signal.aborted) setBatch(result);
      }
      if (!controller.signal.aborted) setNow(Date.now());
    } catch (error) { if (!controller.signal.aborted) setError(message(error)); }
    finally { if (!controller.signal.aborted) setRecommending(false); }
  }
  async function approve(retry?: Approval) {
    if (submittingRef.current || (!retry && (!recommendation || approvalBlocked || Date.now() >= Date.parse(recommendation.expires_at)))) return;
    submittingRef.current = true; setSubmitting(true); setConfirming(false); setError('');
    const approval = retry ?? { request_id: crypto.randomUUID(), recommendation_id: recommendation!.recommendation_id };
    setPending(approval);
    try {
      const result = await api<Dispatch>('/api/dispatch/dispatches', { method: 'POST', body: JSON.stringify(approval) });
      if (!mounted.current) return;
      if (result.command?.route) setSelectedDispatch(result.dispatch_id);
      if (result.state === 'UNKNOWN') {
        // HTTP 202 only confirms the coordinator received the request, not
        // that Fleet has acknowledged an atomic claim.
        setError(`Dispatch ${result.dispatch_id}: outcome UNKNOWN. Reconcile or retry the same request ID.`);
      } else {
        setPending(null); setRecommendation(null);
        if (result.state === 'REJECTED' || result.state === 'FAILED') {
          setError(`Dispatch ${result.dispatch_id}: ${result.state} — ${result.failure_reason || 'Approval unsuccessful'}.`);
        } else {
          setNotice(`Dispatch ${result.dispatch_id}: ${result.state}. Follow its live backend status below.`);
        }
      }
    } catch (error) {
      if (!mounted.current) return;
      if (error instanceof ApiError && error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
        setPending(null); setRecommendation(null); setError(`${message(error)}. Request a new recommendation before approving again.`);
      } else setError(`Approval outcome uncertain: ${message(error)}. Reconcile below or retry the same approval.`);
    } finally {
      submittingRef.current = false;
      if (mounted.current) { setSubmitting(false); dispatches.refresh(); fleet.refresh(); heat.refresh(); }
    }
  }
  async function approveBatch(retry?: BatchApproval) {
    if (submittingRef.current || (!retry && (!batch || batchBlocked || Date.now() >= Date.parse(batch.expires_at)))) return;
    submittingRef.current = true; setSubmitting(true); setBatchConfirming(false); setError('');
    const approval = retry ?? { request_id: crypto.randomUUID(), batch_recommendation_id: batch!.batch_recommendation_id };
    setPendingBatch(approval);
    try {
      const result = await api<BatchDispatch>('/api/dispatch/batches', { method: 'POST', body: JSON.stringify(approval) });
      if (!mounted.current) return;
      setBatchResult(result); setBatch(null);
      const dispatched = result.items.find(item => item.dispatch_id && !['REJECTED', 'FAILED'].includes(item.state));
      if (dispatched?.dispatch_id) setSelectedDispatch(dispatched.dispatch_id);
      if (result.status === 'UNKNOWN') {
        setError('Some batch outcomes are uncertain. Reconcile or retry using the same batch request ID.');
      } else {
        setPendingBatch(null);
        setNotice(`Batch outcome: ${result.status}. Check each child dispatch in Relocations.`);
      }
    } catch (e) {
      if (!mounted.current) return;
      if (e instanceof ApiError && e.status >= 400 && e.status < 500 && ![408, 429].includes(e.status)) {
        setPendingBatch(null); setBatch(null);
        setError(`${message(e)}. Refresh the batch recommendation before approving again.`);
      } else setError(`Batch approval uncertain: ${message(e)}. Retry the same batch ID.`);
    } finally {
      submittingRef.current = false;
      if (mounted.current) {
        setSubmitting(false); dispatches.refresh(); fleet.refresh(); heat.refresh(); facilities.refresh();
      }
    }
  }
  return <div className="app">
    <HotspotToasts zones={heat.data?.zones}/>
    <header><div className="brand-icon" aria-hidden="true">↗</div><div><div className="eyebrow">Operations / Singapore</div><h1>Fleet Dispatch</h1></div><div className="header-actions"><span className="simulation">SIMULATION</span><div className="theme-toggle" role="group" aria-label="Color theme">
          <button type="button" className="theme-option" aria-label="Light mode" aria-pressed={theme === 'light'} onClick={() => { applyTheme('light', true); setTheme('light'); }}>
            <span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></svg></span>Light
          </button>
          <button type="button" className="theme-option" aria-label="Dark mode" aria-pressed={theme === 'dark'} onClick={() => { applyTheme('dark', true); setTheme('dark'); }}>
            <span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M20.5 13.2A8.5 8.5 0 0 1 10.8 3.5a8.5 8.5 0 1 0 9.7 9.7Z"/></svg></span>Dark
          </button>
        </div></div></header>
    <div className="service-bar"><ServiceStatus name="Demand" resource={heat}/><ServiceStatus name="Fleet" resource={fleet}/><ServiceStatus name="Dispatch" resource={dispatches}/><ServiceStatus name={liveFacilities ? 'Facilities (LTA)' : 'Facilities (mock)'} resource={facilities}/><span className="refresh-note">Auto-refresh · 2 seconds · SGT</span></div>
    <main>
      <div className="page-intro"><div><h2>A clear view. An informed move.</h2><p>Monitor demand, review a recommendation, then approve a relocation.</p></div><button onClick={() => { heat.refresh(); fleet.refresh(); dispatches.refresh(); facilities.refresh(); }}>Refresh now</button></div>
      {(heat.error || fleet.error || dispatches.error) && <p className="warning" role="status">Some services are unavailable. Retained observations are last known, not live. Healthy panels continue updating.</p>}
      {heat.data?.status === 'PARTIAL' && <p className="warning" role="status">Demand response is partial · Fleet {heat.data.fleet_status}. {heat.data.issues.join(' · ')}</p>}
      {error && <p className="warning" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}
      {pendingBatch && <div className="warning" role="status"><strong>{submitting ? 'Submitting batch…' : 'Unresolved batch approval'}</strong><p>Batch request {pendingBatch.request_id}. Do not create a different request until reconciled.</p><button disabled={submitting} onClick={() => void approveBatch(pendingBatch)}>Retry same batch approval</button><button onClick={async () => {
        try { const result = await api<BatchDispatch>(`/api/dispatch/batches/${pendingBatch.request_id}`); setBatchResult(result); if (result.status !== 'UNKNOWN') setPendingBatch(null); }
        catch (e) { setError(message(e)); }
      }}>Reconcile batch</button></div>}
      {pending && <div className="warning" role="status"><strong>{submitting ? 'Submitting approval…' : 'Unresolved approval'}</strong><p>Request {pending.request_id}. New approvals are paused until this request is resolved.</p><button disabled={submitting} onClick={() => void approve(pending)}>Retry same approval</button> <button onClick={dispatches.refresh}>Reconcile status</button></div>}
      <div className="workspace"><section ref={mapPanel} tabIndex={-1} aria-labelledby="map-title" className="panel map-panel workspace-main"><div className="section-title"><h2 id="map-title">Singapore demand & EV routes</h2><span className="muted">6 operating zones</span></div>
        <SingaporeMap zones={zones} vehicles={vehicles} facilities={sites} dispatches={dispatches.data?.dispatches} traffic={traffic.data} telemetryFeedHealthy={fleetFeedHealthy} selected={selected} onSelect={choose} selectedDispatch={selectedDispatch} onSelectDispatch={setSelectedDispatch}/>
        <div className="legend">Hotspot severity {['NORMAL', 'WARNING', 'CRITICAL', 'UNKNOWN'].map(value => <Badge key={value} value={value}/>)}</div>
        <p className="snapshot">Demand snapshot {time(heat.data?.demand_snapshot_at)} · Fleet snapshot {time(heat.data?.fleet_snapshot_at)} · EV markers follow reported telemetry</p>
      </section>
      <aside ref={inspector} tabIndex={-1} className="panel target-panel" aria-labelledby="inspector-title">
        <div className="section-title"><h2 id="inspector-title">Dispatch inspector</h2></div>
        <div className="zone-list" aria-label="Select target zone">{zones.map(z => <button key={z.zone_id} aria-pressed={selected === z.zone_id} onClick={() => choose(z.zone_id)}><strong>{z.name}</strong><Badge value={z.hotspot_severity}/></button>)}</div>
        {!zones.length && <p className="empty">Waiting for zone data from Demand Service.</p>}
        {zone ? <details className="destination-details"><summary>{zone.name} · Demand & supply details</summary><ZoneDetails zone={zone}/></details> : <p className="empty">Select a zone on the map or from the list to review its demand and request a recommendation.</p>}
      {zone && <section className="relocation-request" aria-labelledby="relocation-request-title"><div className="section-title"><h3 id="relocation-request-title">Relocation request</h3><span className="muted">1–20 EVs</span></div>
          <div className="request-fields"><div className="request-field">
          <label htmlFor="relocation-purpose">Destination purpose</label>
          <div className="select-wrap"><select className="form-select" id="relocation-purpose" aria-describedby="relocation-purpose-help" value={purpose} disabled={recommending || submitting} onChange={event => {
            setPurpose(event.target.value as 'REBALANCE' | 'CHARGE'); setSelectedFacility('');
            setRecommendation(null); setBatch(null); setBatchResult(null); setConfirming(false); setBatchConfirming(false);
          }}><option value="REBALANCE">{facilityWorkflow ? 'Automatic — parking or charging' : 'Parking / demand rebalancing'}</option><option value="CHARGE">Charging only — replenish to 80%</option></select></div>
          <details><summary>Destination guidance</summary><p id="relocation-purpose-help" className="muted">{purpose === 'CHARGE'
            ? 'Request a charging destination explicitly. Ground-team cable connection is assumed; simulated charging starts after arrival and stops at 80%.'
            : facilityWorkflow ? 'Choose a facility automatically for each EV: charging if predicted arrival battery is below 35%, otherwise parking. The EV must still have enough battery to reach it safely.'
            : 'Find a destination zone to meet demand.'}</p></details>
          </div><div className="request-field">
          <label htmlFor="relocation-vehicle">Vehicle</label>
          <div className="select-wrap"><select className="form-select" id="relocation-vehicle" aria-describedby="relocation-vehicle-help" value={selectedVehicle} disabled={recommending || submitting} onChange={event => {
            setSelectedVehicle(event.target.value); setSelectedFacility(''); if (event.target.value) setQuantity(1);
            setRecommendation(null); setBatch(null); setBatchResult(null); setConfirming(false); setBatchConfirming(false);
          }}><option value="">Any eligible EV</option>{vehicles.map(vehicle => <option key={vehicle.vehicle_id} value={vehicle.vehicle_id} disabled={vehicle.stale || vehicle.effective_availability !== 'AVAILABLE'}>{vehicle.vehicle_id} · {vehicle.telemetry?.soc_pct.toFixed(1) ?? '—'}% · {vehicle.effective_availability}</option>)}</select></div>
          <p id="relocation-vehicle-help" className="muted">{selectedVehicle ? 'One relocation for the selected EV.' : 'Let the system select, or choose an EV for a single relocation.'}</p>
          {selectedFacility && <p className="muted">Charging advisory destination: <strong>{selectedFacility}</strong> (exact facility required). <button disabled={recommending || submitting} onClick={() => { setSelectedFacility(''); setRecommendation(null); setBatch(null); }}>Clear advised charger</button></p>}
          </div></div>
          <div className="quantity-picker"><label htmlFor="ev-count">Number of EVs needed</label><input id="ev-count" type="number" min={1} max={20} value={quantity} disabled={recommending || submitting || !!selectedVehicle} onChange={event => {
            const value = Math.max(1, Math.min(20, Math.trunc(Number(event.target.value) || 1)));
            setQuantity(value); setRecommendation(null); setBatch(null); setBatchResult(null); setConfirming(false); setBatchConfirming(false);
          }}/></div>
      </section>}
      {zone && <>
        <button className="primary" disabled={recommending || !!pending || !!pendingBatch || submitting || !fresh || (facilityWorkflow && !facilitiesFresh)} onClick={() => void recommend()}>{recommending ? 'Finding suitable EVs…' : quantity === 1 ? 'Recommend EV' : `Recommend ${quantity} EVs`}</button>
        <p className="muted">A recommendation does not move an EV. The dispatcher must review and approve it.</p></>}
        {zone && !fresh && <p className="warning">Fresh demand and fleet data are required to request or approve a recommendation.</p>}
        {recommendation && <RecommendationPanel recommendation={recommendation} remaining={remaining} disabled={!!approvalBlocked} onReview={() => setConfirming(true)}/>}
        {batch && <BatchPanel batch={batch} remaining={batchRemaining} disabled={batchBlocked} onReview={() => setBatchConfirming(true)}/>}
      </aside></div>
      {batchResult && <BatchResult result={batchResult}/>}
      <OperationalTabs tabs={[
        { id: 'fleet', label: 'Fleet', content: <>
          <FleetPanel vehicles={vehicles}/>
          {twinVehicle && <details className="twin-history"><summary>EV digital twin diagnostics</summary>
            <TwinHistoryPanel key={twinVehicle.vehicle_id} vehicle={twinVehicle}/>
          </details>}
        </> },
        { id: 'relocations', label: 'Relocations',
          summary: dispatches.updatedAt && !dispatches.error && now - Date.parse(dispatches.updatedAt) < 10000
            ? `${dispatches.data?.dispatches.filter(d => !['SUCCEEDED', 'REJECTED', 'FAILED'].includes(d.state)).length ?? 0} unfinished`
            : 'unavailable',
          content: <RelocationPanel dispatches={dispatches.data?.dispatches ?? []} vehicles={vehicles} onTrack={id => {
            const target = dispatches.data?.dispatches.find(dispatch => dispatch.dispatch_id === id)?.command?.target_zone_id;
            if (target && target !== selected) choose(target);
            setSelectedDispatch(id);
            mapPanel.current?.focus({ preventScroll: true });
            mapPanel.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }}/> },
        { id: 'charging', label: 'Charging',
          summary: fleetFeedHealthy
            ? `EVs ≤30% SOC: ${vehicles.filter(v => !v.stale && v.connectivity === 'ONLINE' && v.telemetry && v.telemetry.soc_pct <= 30).length}`
            : 'unavailable',
          content: <ChargingOperations vehicles={vehicles} onUpdated={() => fleet.refresh()}
            advice={chargingAdvice.data} adviceError={chargingAdvice.error} adviceFresh={chargingAdviceFresh}
            canPrepare={!!fresh && facilitiesFresh && !pending && !pendingBatch && !submitting && !recommending}
            onPrepare={(item: ChargerAdvice) => {
              if (!item.charger || !chargingAdviceFresh || !fresh || !facilitiesFresh
                  || pending || pendingBatch || submitting || recommending) return;
              // Prefill only; the operator must explicitly request and review the
              // live backend batch recommendation, then approve the dispatch.
              choose(item.charger.zone_id);
              setSelectedVehicle(item.vehicle_id);
              setSelectedFacility(item.charger.facility_id);
              setPurpose('CHARGE');
              setQuantity(1);
              inspector.current?.focus({ preventScroll: true });
              inspector.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
            }}/> },
        { id: 'facilities', label: 'Facilities', content: <FacilitiesPanel key={selected} facilities={sites} selectedZone={selected} error={facilities.error} snapshot={facilities.data}/> },
        { id: 'traffic', label: 'Traffic', content: <>
          {selectedDispatch && dispatches.data?.dispatches.some(d => d.dispatch_id === selectedDispatch && d.command?.route && !['SUCCEEDED', 'REJECTED', 'FAILED'].includes(d.state)) &&
            <TrafficJourneyCheck dispatchId={selectedDispatch}/>}
          <TrafficConditions snapshot={traffic.data} error={traffic.error}/>
        </> },
        { id: 'rentals', label: 'Rentals', content: <RentalOutlook vehicles={vehicles} zones={heat.data?.zones ?? []}/> },
      ]}/>
      <footer>Human-approved relocation · Backend-owned decisions · In-memory simulation</footer>
    </main>{confirming && recommendation && <DispatchConfirmation recommendation={recommendation} vehicle={current || undefined} expired={!!approvalBlocked} onClose={() => setConfirming(false)} onApprove={() => void approve()}/>}
    {batchConfirming && batch && <BatchConfirmation batch={batch} expired={batchBlocked} onClose={() => setBatchConfirming(false)} onApprove={() => void approveBatch()}/>}
  </div>;
}
