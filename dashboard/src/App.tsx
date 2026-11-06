import { useCallback, useEffect, useRef, useState } from 'react';
import { applyTheme, type Theme } from './theme';
import type { Approval, BatchApproval, BatchDispatch, BatchRecommendation, ChargerAdvice, ChargingAdvice, TrafficSnapshot, Dispatch, DispatchList, Facility, FacilitiesSnapshot, Fleet, HeatMap, Recommendation } from './api/contracts';
import { api, ApiError, message } from './api/client';
import { usePolling } from './hooks/usePolling';
import { OperationalTabs } from './components/OperationalTabs';
import { FleetPulse } from './components/FleetPulse';
import { SingaporeMap } from './components/SingaporeMap';
import { Notifications, type ActiveNotification, type Notification, type Notify } from './components/Notifications';
import { ChargingOperations } from './components/ChargingOperations';
import { TrafficConditions } from './components/TrafficConditions';
import { TrafficJourneyCheck } from './components/TrafficJourneyCheck';
import { RentalOutlook } from './components/RentalOutlook';
import { FacilitiesPanel } from './components/FacilitiesPanel';
import { BatchPanel, BatchConfirmation, BatchResult } from './components/BatchPanel';
import { Badge, number, time } from './components/format';
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
  const fleet = usePolling<Fleet>('/api/fleet/dashboard/vehicles', 1000);
  const dispatches = usePolling<DispatchList>('/api/dispatch/dispatches');
  const facilities = usePolling<FacilitiesSnapshot>('/api/facilities/facilities', 10000);
  const chargingAdvice = usePolling<ChargingAdvice>('/api/rebalancing/charging-advice', 10000);
  const traffic = usePolling<TrafficSnapshot>('/api/rebalancing/traffic/conditions', 5000);
  const [selected, setSelected] = useState('');
  const [selectedDispatch, setSelectedDispatch] = useState('');
  const [operationalTab, setOperationalTab] = useState('dispatch');
  const [compactView, setCompactView] = useState<'dispatch' | 'map'>('dispatch');
  const [demandDetails, setDemandDetails] = useState(false);
  const [mapFocus, setMapFocus] = useState<{ dispatchId: string } | { facility: Facility } | null>(null);
  const trafficDispatchId = selectedDispatch || dispatches.data?.dispatches.filter(d =>
    d.command?.route && !['SUCCEEDED', 'REJECTED', 'FAILED'].includes(d.state)).at(-1)?.dispatch_id;
  const unresolvedRelocations = dispatches.data?.dispatches.filter(d => d.vehicle_id &&
    (d.state === 'UNKNOWN' || d.recovery?.status === 'BLOCKED' || d.recovery?.status === 'REQUESTED' || d.recovery?.status === 'RESTORING')) ?? [];
  function openRelocation(id: string) {
    setOperationalTab('relocations');
    requestAnimationFrame(() => {
      const card = document.getElementById(`relocation-${id}`);
      card?.focus({ preventScroll: true });
      card?.scrollIntoView({ block: 'nearest' });
    });
  }
  const mapPanel = useRef<HTMLElement>(null);
  function openMap(target: NonNullable<typeof mapFocus>) {
    setMapFocus(target);
    if ('dispatchId' in target) setSelectedDispatch(target.dispatchId);
    setOperationalTab('dispatch');
    setCompactView('map');
    requestAnimationFrame(() => {
      mapPanel.current?.focus({ preventScroll: true });
      mapPanel.current?.scrollIntoView?.({ block: 'nearest' });
    });
  }

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
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [activity, setActivity] = useState<Notification[]>([]);
  const activityId = useRef(0);
  const notify = useCallback<Notify>((tone, message) => {
    const item = { id: ++activityId.current, tone, message, createdAt: new Date().toISOString() };
    setActivity(current => [item, ...current].slice(0, 20));
  }, []);
  const hotspotZones = heat.data?.zones;
  const previousSeverity = useRef(new Map<string, HeatMap['zones'][number]['hotspot_severity']>());
  useEffect(() => {
    if (!hotspotZones) return;
    const currentSeverity = new Map<string, HeatMap['zones'][number]['hotspot_severity']>();
    for (const zone of hotspotZones) {
      const before = previousSeverity.current.get(zone.zone_id);
      const after = zone.hotspot_severity;
      currentSeverity.set(zone.zone_id, after);
      if (before !== undefined && before !== after) {
        notify(after === 'CRITICAL' || after === 'WARNING' ? 'warning' : 'info',
          `${zone.name} hotspot severity changed from ${before} to ${after}.`);
      }
    }
    previousSeverity.current = currentSeverity;
  }, [hotspotZones, notify]);
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
    if (matching && (matching.claim_held === true ||
        ['REJECTED', 'FAILED', 'SUCCEEDED'].includes(matching.state))) {
      setPending(null); setRecommendation(null); setConfirming(false);
      if (matching.state === 'REJECTED' || matching.state === 'FAILED') {
        notify('error', `Dispatch ${matching.dispatch_id}: ${matching.state} — ${matching.failure_reason || 'Approval unsuccessful'}.`);
      } else {
        notify('info', `Dispatch ${matching.dispatch_id}: ${matching.state}. Follow its backend state in Relocations.`);
      }
    }
  }, [dispatches.data, pending, setPending, notify]);
  const choose = useCallback((id: string) => {
    recommendationRequest.current?.abort(); setRecommending(false);
    setSelected(id); setDemandDetails(false); setSelectedFacility(''); setRecommendation(null); setBatch(null); setBatchResult(null); setConfirming(false); setBatchConfirming(false);
  }, []);
  const zones = heat.data?.zones ?? [];
  const vehicles = fleet.data?.vehicles ?? [];
  const twinVehicle = vehicles.find(v => v.vehicle_id === selectedVehicle) ||
    vehicles.find(v => v.vehicle_id === dispatches.data?.dispatches.find(d => d.dispatch_id === selectedDispatch)?.vehicle_id) || vehicles[0];
  const sites = facilities.data?.facilities ?? [];
  const liveFacilities = facilities.data?.data_source === 'LTA_DATAMALL';
  // An unknown provider must not fall back to a zone-only destination.
  const facilityWorkflow = facilities.data?.data_source !== 'MOCK' || quantity > 1 || purpose === 'CHARGE' || !!selectedVehicle;
  const criticalZones = zones.filter(item => item.hotspot_severity === 'CRITICAL').length;
  const feedIssues = [heat, fleet, dispatches, facilities].filter(resource => resource.error).length;
  const feedsReady = [heat, fleet, dispatches, facilities].every(resource => resource.data);
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
  const approvalBlocked = !fresh || !current || current.stale || !!pending || !!pendingBatch || submitting || remaining <= 0
    || (!!recommendation?.crds && (recommendation.crds.decision !== 'MOVE' || !recommendation.crds.approval_allowed));
  const batchBlocked = !fresh || !facilitiesFresh || !!pending || !!pendingBatch || submitting || batchRemaining <= 0
    || (!!batch?.crds && (batch.crds.decision !== 'MOVE' || !batch.crds.approval_allowed));
  async function recommend() {
    recommendationRequest.current?.abort();
    const controller = new AbortController(); recommendationRequest.current = controller;
    setRecommending(true); setRecommendation(null); setBatch(null); setBatchResult(null);
    try {
      if (!facilityWorkflow) {
        const result = await api<Recommendation>('/api/rebalancing/recommendations', {
          method: 'POST', timeoutMs: 135000, body: JSON.stringify({ target_zone_id: selected }), signal: controller.signal,
        });
        if (!controller.signal.aborted) setRecommendation(result);
      } else {
        const result = await api<BatchRecommendation>('/api/rebalancing/batch-recommendations', {
          method: 'POST', timeoutMs: 135000, body: JSON.stringify({ target_zone_id: selected, quantity,
            ...(purpose === 'CHARGE' ? { purpose } : {}),
            ...(selectedVehicle ? { vehicle_id: selectedVehicle } : {}),
            ...(purpose === 'CHARGE' && selectedFacility ? { facility_id: selectedFacility } : {}) }), signal: controller.signal,
        });
        if (!controller.signal.aborted) setBatch(result);
      }
      if (!controller.signal.aborted) setNow(Date.now());
    } catch (error) { if (!controller.signal.aborted) notify('error', message(error)); }
    finally { if (!controller.signal.aborted) setRecommending(false); }
  }
  async function approve(retry?: Approval) {
    if (submittingRef.current || (!retry && (!recommendation || approvalBlocked || Date.now() >= Date.parse(recommendation.expires_at)))) return;
    submittingRef.current = true; setSubmitting(true); setConfirming(false);
    const approval = retry ?? { request_id: crypto.randomUUID(), recommendation_id: recommendation!.recommendation_id };
    setPending(approval);
    try {
      const result = await api<Dispatch>('/api/dispatch/dispatches', { method: 'POST', timeoutMs: 135000, body: JSON.stringify(approval) });
      if (!mounted.current) return;
      if (result.command?.route) setSelectedDispatch(result.dispatch_id);
      if (result.state === 'UNKNOWN' ||
          (!['REJECTED', 'FAILED', 'SUCCEEDED'].includes(result.state) &&
           result.claim_held !== true)) {
        // HTTP 202 only confirms the coordinator received the request, not
        // that Fleet has acknowledged an atomic claim.
        notify('warning', `Dispatch ${result.dispatch_id}: approval not yet confirmed (${result.state}). Reconcile or retry the same request ID.`);
      } else {
        setPending(null); setRecommendation(null);
        if (result.state === 'REJECTED' || result.state === 'FAILED') {
          notify('error', `Dispatch ${result.dispatch_id}: ${result.state} — ${result.failure_reason || 'Approval unsuccessful'}.`);
        } else {
          notify('info', `Dispatch ${result.dispatch_id}: ${result.state}. Follow its live backend status in Relocations.`);
        }
      }
    } catch (error) {
      if (!mounted.current) return;
      if (error instanceof ApiError && error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
        setPending(null); setRecommendation(null); notify('error', `${message(error)}. Request a new recommendation before approving again.`);
      } else notify('warning', `Approval outcome uncertain: ${message(error)}. Reconcile status or retry the same approval.`);
    } finally {
      submittingRef.current = false;
      if (mounted.current) { setSubmitting(false); dispatches.refresh(); fleet.refresh(); heat.refresh(); }
    }
  }
  async function approveBatch(retry?: BatchApproval) {
    if (submittingRef.current || (!retry && (!batch || batchBlocked || Date.now() >= Date.parse(batch.expires_at)))) return;
    submittingRef.current = true; setSubmitting(true); setBatchConfirming(false);
    const approval = retry ?? { request_id: crypto.randomUUID(), batch_recommendation_id: batch!.batch_recommendation_id };
    setPendingBatch(approval);
    try {
      const result = await api<BatchDispatch>('/api/dispatch/batches', { method: 'POST', timeoutMs: 135000, body: JSON.stringify(approval) });
      if (!mounted.current) return;
      setBatchResult(result); setBatch(null);
      const dispatched = result.items.find(item => item.dispatch_id && !['REJECTED', 'FAILED'].includes(item.state));
      if (dispatched?.dispatch_id) setSelectedDispatch(dispatched.dispatch_id);
      if (result.status === 'UNKNOWN') {
        notify('warning', 'Some batch outcomes are uncertain. Reconcile or retry using the same batch request ID.');
      } else {
        setPendingBatch(null);
        reportBatchOutcome(result);
      }
    } catch (e) {
      if (!mounted.current) return;
      if (e instanceof ApiError && e.status >= 400 && e.status < 500 && ![408, 429].includes(e.status)) {
        setPendingBatch(null); setBatch(null);
        notify('error', `${message(e)}. Refresh the batch recommendation before approving again.`);
      } else notify('warning', `Batch approval uncertain: ${message(e)}. Retry the same batch ID.`);
    } finally {
      submittingRef.current = false;
      if (mounted.current) {
        setSubmitting(false); dispatches.refresh(); fleet.refresh(); heat.refresh(); facilities.refresh();
      }
    }
  }
  function reportBatchOutcome(result: BatchDispatch) {
    notify(result.status === 'REJECTED' ? 'error' : result.status === 'PARTIAL' ? 'warning' : 'info',
      `Batch outcome: ${result.status}. Check each child dispatch in Relocations.`);
  }
  const [reconcilingBatch, setReconcilingBatch] = useState(false);
  async function reconcileBatch(request: BatchApproval) {
    setReconcilingBatch(true);
    try {
      const result = await api<BatchDispatch>(`/api/dispatch/batches/${request.request_id}`);
      setBatchResult(result);
      if (result.status !== 'UNKNOWN') {
        setPendingBatch(null);
        setBatch(null);
        reportBatchOutcome(result);
      } else notify('warning', 'The batch approval is still unresolved.');
    } catch (error) {
      notify('error', message(error));
    } finally {
      setReconcilingBatch(false);
    }
  }

  const activeNotifications: ActiveNotification[] = [];
  if (pending) activeNotifications.push({
    id: 'pending', title: submitting ? 'Submitting approval…' : 'Unresolved approval',
    message: `Request ${pending.request_id}. New approvals are paused until this request is resolved.`,
    actions: <>
      <button disabled={submitting} onClick={() => void approve(pending)}>Retry same approval</button>
      <button disabled={submitting} onClick={dispatches.refresh}>Reconcile status</button>
    </>,
  });
  if (pendingBatch) activeNotifications.push({
    id: 'pending-batch', title: submitting ? 'Submitting batch…' : 'Unresolved batch approval',
    message: `Batch request ${pendingBatch.request_id}. New approvals are paused until this request is resolved.`,
    actions: <>
      <button disabled={submitting || reconcilingBatch} onClick={() => void approveBatch(pendingBatch)}>Retry same batch approval</button>
      <button disabled={submitting || reconcilingBatch} onClick={() => void reconcileBatch(pendingBatch)}>
        {reconcilingBatch ? 'Checking batch…' : 'Reconcile batch'}
      </button>
    </>,
  });
  const unavailable = [
    { name: 'Demand', error: heat.error }, { name: 'Fleet', error: fleet.error },
    { name: 'Dispatch', error: dispatches.error }, { name: 'Facilities', error: facilities.error },
    { name: 'Traffic', error: traffic.error }, { name: 'Charging advice', error: chargingAdvice.error },
  ].filter(feed => feed.error);
  if (unavailable.length) activeNotifications.push({
    id: 'feeds', title: 'Service feeds unavailable',
    message: `${unavailable.map(feed => `${feed.name}: ${feed.error}`).join(' · ')}. Any retained observations are last known.`,
  });
  for (const feed of facilities.data?.feeds ?? []) {
    if (feed.status === 'FRESH' || feed.status === 'PENDING') continue;
    activeNotifications.push({
      id: `facility-feed-${feed.name}`, title: `LTA ${feed.name} feed`,
      message: [feed.status, feed.error_code].filter(Boolean).join(' · '),
    });
  }
  if (heat.data?.status === 'PARTIAL') activeNotifications.push({
    id: 'partial-demand', title: 'Partial demand feed',
    message: `Fleet ${heat.data.fleet_status}. ${heat.data.issues.join(' · ')}`,
  });

  const result = recommendation ?? batch;
  const canReview = recommendation
    ? !approvalBlocked && recommendation.ranked_candidates.some(item => item.vehicle_id === recommendation.selected_vehicle_id)
    : !!batch && !batchBlocked && batch.recommended_count > 0;
  const resultRemaining = recommendation ? remaining : batchRemaining;
  const requestBlocked = !fresh || (facilityWorkflow && !facilitiesFresh);
  const inspectorBody = useRef<HTMLDivElement>(null);
  const inspectorTitle = useRef<HTMLHeadingElement>(null);
  const shouldFocusResult = useRef(false);
  useEffect(() => {
    if (inspectorBody.current) inspectorBody.current.scrollTop = 0;
  }, [selected, result, demandDetails]);
  useEffect(() => {
    if (result && shouldFocusResult.current && document.activeElement === document.body &&
        inspectorTitle.current?.getClientRects().length) inspectorTitle.current.focus({ preventScroll: true });
    if (result || !recommending) shouldFocusResult.current = false;
  }, [result, recommending]);
  function editRequest() {
    setRecommendation(null);
    setBatch(null);
    inspectorTitle.current?.focus();
  }
  return <div className="app">
    <header className="app-header"><div className="brand-lockup"><div className="brand-icon" aria-hidden="true"><svg viewBox="0 0 40 40" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M10 28L18 20L25 24L31 12"/><circle cx="10" cy="28" r="3.5" fill="currentColor" stroke="none"/><circle cx="18" cy="20" r="3.5" fill="currentColor" stroke="none"/><circle cx="31" cy="12" r="3.5" fill="currentColor" stroke="none"/></svg></div><div className="brand-copy"><h1>Fleet Dispatch</h1></div></div><div className="header-actions"><span className="simulation">SIMULATION</span><div className="theme-toggle" role="group" aria-label="Color theme">
          <button type="button" className="theme-option" aria-label="Light mode" aria-pressed={theme === 'light'} onClick={() => { applyTheme('light', true); setTheme('light'); }}>
            <span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></svg></span>Light
          </button>
          <button type="button" className="theme-option" aria-label="Dark mode" aria-pressed={theme === 'dark'} onClick={() => { applyTheme('dark', true); setTheme('dark'); }}>
            <span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M20.5 13.2A8.5 8.5 0 0 1 10.8 3.5a8.5 8.5 0 1 0 9.7 9.7Z"/></svg></span>Dark
          </button>
        </div></div>
      <div className="workspace-utilities">
        <details className="feed-health">
          <summary>{feedIssues ? `${feedIssues} ${feedIssues === 1 ? 'feed' : 'feeds'} unavailable` : !feedsReady ? 'Connecting feeds' : heat.data?.status === 'PARTIAL' ? 'Partial demand feed' : 'Service status'}<span className="feed-indicator" data-state={feedIssues || heat.data?.status === 'PARTIAL' ? 'warning' : feedsReady ? 'ready' : 'pending'} aria-hidden="true"/></summary>
          <div className="service-bar"><ServiceStatus name="Demand" resource={heat}/><ServiceStatus name="Fleet" resource={fleet}/><ServiceStatus name="Dispatch" resource={dispatches}/><ServiceStatus name={liveFacilities ? 'Facilities (LTA)' : 'Facilities (mock)'} resource={facilities}/><span className="refresh-note">Times in SGT</span></div>
        </details>
        <Notifications open={notificationsOpen} onOpenChange={setNotificationsOpen} active={activeNotifications}
          history={activity} onClear={() => setActivity([])}/>
        <button className="refresh-action" onClick={() => { heat.refresh(); fleet.refresh(); dispatches.refresh(); facilities.refresh(); chargingAdvice.refresh(); traffic.refresh(); }}>Refresh now</button>
      </div>
    </header>
    <main>
      <OperationalTabs activeTab={operationalTab} onTabChange={setOperationalTab} tabs={[
        { id: 'dispatch', label: 'Dispatch', content: <>
          <div className="workspace" data-compact-view={compactView}>
            <div className="compact-workspace-switch" role="group" aria-label="Dispatch workspace">
              <button aria-pressed={compactView === 'dispatch'} onClick={() => setCompactView('dispatch')}>Zones & dispatch</button>
              <button aria-pressed={compactView === 'map'} onClick={() => setCompactView('map')}>Map</button>
            </div>
            <section ref={mapPanel} tabIndex={-1} className="panel map-panel workspace-main" aria-labelledby="map-title">
              <div className="section-title map-title"><h2 id="map-title">Singapore fleet map</h2><span className="muted">{zones.length ? `${zones.length} operating zones` : 'Awaiting zone feed'}</span></div>
              <SingaporeMap zones={zones} vehicles={vehicles} facilities={sites} dispatches={dispatches.data?.dispatches} traffic={traffic.data}
                telemetryFeedHealthy={fleetFeedHealthy} selected={selected} onSelect={choose} selectedDispatch={selectedDispatch}
                onSelectDispatch={setSelectedDispatch} focusTarget={mapFocus}/>
              {zone && <div className="compact-map-selection">
                <strong>{zone.name}</strong><Badge value={zone.hotspot_severity}/>
                <button onClick={() => {
                  setDemandDetails(true);
                  setCompactView('dispatch');
                  requestAnimationFrame(() => inspectorTitle.current?.focus());
                }}>View demand</button>
              </div>}
              <div className="map-caption">
                <div className="legend" aria-label="Demand severity">{['NORMAL', 'WARNING', 'CRITICAL', 'UNKNOWN'].map(value => <Badge key={value} value={value}/>)}</div>
                <p className="snapshot">Demand {time(heat.data?.demand_snapshot_at)} · Fleet {time(heat.data?.fleet_snapshot_at)} SGT</p>
              </div>
            </section>
            <aside tabIndex={-1} className="panel target-panel" aria-labelledby="inspector-title">
              <div className="inspector-header">
                <h2 id="inspector-title" ref={inspectorTitle} tabIndex={-1}>{demandDetails ? 'Demand & supply' : result ? 'Recommendation' : 'Dispatch'}</h2>
                {demandDetails ? <button onClick={() => { setDemandDetails(false); inspectorTitle.current?.focus(); }}>Back to {result ? 'recommendation' : 'dispatch'}</button>
                  : result ? <button onClick={editRequest}>Edit request</button> : <span className="muted">{fresh && heat.data?.status === 'COMPLETE' && (criticalZones ? `${criticalZones} critical ${criticalZones === 1 ? 'zone' : 'zones'}` : 'No critical zones')}</span>}
              </div>
              <div className="inspector-body" ref={inspectorBody}>
                {!result && !demandDetails && <>
                  <div className="zone-list" role="group" aria-label="Select target zone">{zones.map(z =>
                    <button key={z.zone_id} aria-pressed={selected === z.zone_id} onClick={() => choose(z.zone_id)}>
                      <strong>{z.name}</strong><Badge value={z.hotspot_severity}/>
                      <small>{z.current_shortage == null ? 'Shortage unknown' : z.current_shortage ? `${z.current_shortage} short now` : 'No current shortage'}</small>
                    </button>)}</div>
                  {!zones.length && <p className="empty">Waiting for zone data from Demand Service.</p>}
                  {!!zones.length && !zone && <p className="empty inspector-empty">Choose a zone to see its shortage and request a relocation.</p>}
                </>}
                {zone && (demandDetails ? <ZoneDetails zone={zone}/> : <section className="selected-demand" aria-label="Selected zone demand">
                  <div className="section-title"><h3>{zone.name}</h3>{result ? <Badge value={zone.hotspot_severity}/> :
                    <button className="text-button" onClick={() => { setDemandDetails(true); inspectorTitle.current?.focus(); }}>Demand details</button>}</div>
                  {result ? <p className="muted">Shortage now {number(zone.current_shortage)} · Projected {number(zone.projected_shortage)}</p> : <>
                    <dl className="demand-figures">
                      <div><dt>Rental demand</dt><dd>{number(zone.rental_demand)}</dd></div>
                      <div><dt>Available EVs</dt><dd>{number(zone.available_evs)}</dd></div>
                      <div><dt>Current shortage</dt><dd>{number(zone.current_shortage)}</dd></div>
                      <div><dt>Projected shortage</dt><dd>{number(zone.projected_shortage)}</dd></div>
                    </dl>
                    <p className="muted demand-inbound">Relocating in: {number(zone.inbound_relocating_evs)} · Pending: {number(zone.inbound_pending_evs)} · Unconfirmed: {number(zone.inbound_unconfirmed_evs)}</p>
                  </>}
                </section>)}
                {zone && !result && !demandDetails && <section className="relocation-request" aria-label="Relocation request">
                  <div className="request-fields">
                    <div className="request-field"><label htmlFor="relocation-purpose">Destination purpose</label>
                      <div className="select-wrap"><select className="form-select" id="relocation-purpose" value={purpose} disabled={recommending || submitting} onChange={event => {
                        setPurpose(event.target.value as 'REBALANCE' | 'CHARGE'); setSelectedFacility('');
                        setBatchResult(null);
                      }}><option value="REBALANCE">{facilityWorkflow ? 'Automatic' : 'Rebalance'}</option><option value="CHARGE">Charging only</option></select></div>
                    </div>
                    <div className="request-field request-count"><label htmlFor="ev-count">EV count</label><input id="ev-count" aria-label="Number of EVs needed" type="number" min={1} max={20} value={quantity} disabled={recommending || submitting || !!selectedVehicle} onChange={event => {
                      setQuantity(Math.max(1, Math.min(20, Math.trunc(Number(event.target.value) || 1))));
                      setBatchResult(null);
                    }}/></div>
                    <div className="request-field request-vehicle"><label htmlFor="relocation-vehicle">Vehicle</label>
                      <div className="select-wrap"><select className="form-select" id="relocation-vehicle" value={selectedVehicle} disabled={recommending || submitting} onChange={event => {
                        setSelectedVehicle(event.target.value); setSelectedFacility(''); if (event.target.value) setQuantity(1);
                        setBatchResult(null);
                      }}><option value="">Any eligible</option>{vehicles.map(vehicle => <option key={vehicle.vehicle_id} value={vehicle.vehicle_id} disabled={vehicle.stale || vehicle.effective_availability !== 'AVAILABLE'}>{vehicle.vehicle_id} · {vehicle.telemetry?.soc_pct.toFixed(1) ?? '—'}% · {vehicle.effective_availability}</option>)}</select></div>
                    </div>
                  </div>
                  {purpose === 'CHARGE' ? <p className="muted">Simulated charging after arrival, up to 80%.</p> : facilityWorkflow && <p className="muted">Automatic chooses charging below 35% predicted arrival SOC, or parking otherwise.</p>}
                  {selectedFacility && <p className="muted">Charging advisory destination: <strong>{selectedFacility}</strong> (exact facility required). <button disabled={recommending || submitting} onClick={() => setSelectedFacility('')}>Clear advised charger</button></p>}
                </section>}
                {!demandDetails && recommendation && <RecommendationPanel key={recommendation.recommendation_id} recommendation={recommendation}/>}
                {!demandDetails && batch && <BatchPanel key={batch.batch_recommendation_id} batch={batch}/>}
                {!demandDetails && result?.crds?.decision === 'DEFER' && unresolvedRelocations.length > 0 &&
                  <section className="assessment-followup" aria-label="Relocations to review">
                    <h3>Unresolved relocations</h3>
                    {unresolvedRelocations.map(d => <button key={d.dispatch_id} onClick={() => openRelocation(d.dispatch_id)}>Review {d.vehicle_id ?? d.dispatch_id} relocation / recovery</button>)}
                  </section>}
              </div>
              {((zone && !demandDetails) || pending || pendingBatch) && <div className="inspector-actions">
                {(pending || pendingBatch) && <button className="approval-notification-link" onClick={() => {
                  setNotificationsOpen(true);
                  requestAnimationFrame(() => document.getElementById('notifications-title')?.focus());
                }}>{submitting ? 'Approval in progress' : 'Resolve pending approval'}</button>}
                {zone && !demandDetails && (result ? <>
                  {resultRemaining > 0 ? <span className="muted">Expires in {resultRemaining}s</span> : <span className="warning">Expired · edit request to start again</span>}
                  {resultRemaining > 0 && (!fresh || (batch && !facilitiesFresh) || (recommendation && current?.stale)) &&
                    <span className="warning">Approval requires fresh observations.</span>}
                  <button className="primary" disabled={!canReview} onClick={() => recommendation ? setConfirming(true) : setBatchConfirming(true)}>Review dispatch</button>
                </> : <>
                  {requestBlocked && <p className="warning">{!fresh ? 'Fresh demand and fleet data are required.' : 'Fresh facility availability is required.'}</p>}
                  <button className="primary" disabled={recommending || !!pending || !!pendingBatch || submitting || requestBlocked} onClick={() => {
                    shouldFocusResult.current = true;
                    void recommend();
                  }}>{recommending ? 'Finding suitable EVs…' : quantity === 1 ? 'Recommend EV' : `Recommend ${quantity} EVs`}</button>
                  <span className="muted">Dispatch requires your approval.</span>
                </>)}
              </div>}
            </aside>
          </div>
          {batchResult && <BatchResult result={batchResult} onRelocation={openRelocation}/>}
        </> },
        { id: 'fleet', label: 'Fleet', content: <>
          <FleetPulse fleet={fleet} heat={heat} dispatches={dispatches} now={now}/>
          <FleetPanel vehicles={vehicles} initialVehicle={twinVehicle?.vehicle_id}/>
        </> },
        { id: 'relocations', label: 'Relocations',
          summary: dispatches.updatedAt && !dispatches.error && now - Date.parse(dispatches.updatedAt) < 10000
            ? `${dispatches.data?.dispatches.filter(d => !['SUCCEEDED', 'REJECTED', 'FAILED'].includes(d.state)).length ?? 0} unfinished`
            : 'unavailable',
          content: <RelocationPanel notify={notify} dispatches={dispatches.data?.dispatches ?? []} vehicles={vehicles} onTrack={id => {
            const target = dispatches.data?.dispatches.find(dispatch => dispatch.dispatch_id === id)?.command?.target_zone_id;
            if (target && target !== selected) choose(target);
            openMap({ dispatchId: id });
          }}/> },
        { id: 'charging', label: 'Charging',
          summary: fleetFeedHealthy
            ? `EVs ≤30% SOC: ${vehicles.filter(v => !v.stale && v.connectivity === 'ONLINE' && v.telemetry && v.telemetry.soc_pct <= 30).length}`
            : 'unavailable',
          content: <ChargingOperations notify={notify} vehicles={vehicles} onUpdated={() => fleet.refresh()}
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
              setOperationalTab('dispatch');
              setCompactView('dispatch');
              requestAnimationFrame(() => inspectorTitle.current?.focus());
            }}/> },
        { id: 'facilities', label: 'Facilities', content: <FacilitiesPanel zones={zones} facilities={sites} selectedZone={selected} error={facilities.error} snapshot={facilities.data}
          onShowOnMap={facility => openMap({ facility })}/> },
        { id: 'traffic', label: 'Traffic', content: <>
          {trafficDispatchId && dispatches.data?.dispatches.some(d => d.dispatch_id === trafficDispatchId && d.command?.route && !['SUCCEEDED', 'REJECTED', 'FAILED'].includes(d.state)) &&
            <div className="traffic-journey-view"><TrafficJourneyCheck key={trafficDispatchId} dispatchId={trafficDispatchId} notify={notify}/>
              <button onClick={() => openMap({ dispatchId: trafficDispatchId })}>View assessed route on map</button>
            </div>}
          <TrafficConditions snapshot={traffic.data} error={traffic.error}/>
        </> },
        { id: 'rentals', label: 'Rentals', content: <RentalOutlook vehicles={vehicles} zones={heat.data?.zones ?? []}/> },
      ]}/>
    </main>{confirming && recommendation && <DispatchConfirmation recommendation={recommendation} vehicle={current || undefined} expired={!!approvalBlocked} onClose={() => setConfirming(false)} onApprove={() => void approve()}/>}
    {batchConfirming && batch && <BatchConfirmation batch={batch} expired={batchBlocked} onClose={() => setBatchConfirming(false)} onApprove={() => void approveBatch()}/>}
  </div>;
}
