import { useCallback, useEffect, useRef, useState } from 'react';
import type { Approval, Dispatch, DispatchList, FacilitiesSnapshot, Fleet, HeatMap, Recommendation } from './api/contracts';
import { api, ApiError, message } from './api/client';
import { usePolling } from './hooks/usePolling';
import { SingaporeMap } from './components/SingaporeMap';
import { FacilitiesPanel } from './components/FacilitiesPanel';
import { Badge, time } from './components/format';
import { DispatchConfirmation, FleetPanel, RecommendationPanel, RelocationPanel, ServiceStatus, ZoneDetails } from './components/Panels';

const pendingKey = 'fleet.pending-approval';
function readPending(): Approval | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(pendingKey) || 'null');
    return value && typeof value.request_id === 'string' && typeof value.recommendation_id === 'string' ? value : null;
  } catch { return null; }
}
export default function App() {
  const heat = usePolling<HeatMap>('/api/demand/heat-map');
  const fleet = usePolling<Fleet>('/api/fleet/vehicles');
  const dispatches = usePolling<DispatchList>('/api/dispatch/dispatches');
  const facilities = usePolling<FacilitiesSnapshot>('/api/facilities/facilities', 10000);
  const [selected, setSelected] = useState('');
  const [recommendation, setRecommendation] = useState<Recommendation | null>(null);
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
  useEffect(() => {
    if (pending && dispatches.data?.dispatches.some(d => d.request_id === pending.request_id)) {
      setPending(null); setRecommendation(null); setConfirming(false);
      setNotice('Approval reconciled. Follow its backend status in Relocations.'); setError('');
    }
  }, [dispatches.data, pending, setPending]);
  const choose = useCallback((id: string) => {
    recommendationRequest.current?.abort(); setRecommending(false);
    setSelected(id); setRecommendation(null); setConfirming(false); setError('');
  }, []);
  const zones = heat.data?.zones ?? [];
  const vehicles = fleet.data?.vehicles ?? [];
  const sites = facilities.data?.facilities ?? [];
  const zone = zones.find(z => z.zone_id === selected);
  const current = recommendation && vehicles.find(v => v.vehicle_id === recommendation.selected_vehicle_id);
  const fresh = !!heat.data && !heat.error && !!fleet.data && !fleet.error && heat.data.fleet_status === 'FRESH'
    && !!heat.updatedAt && now - Date.parse(heat.updatedAt) < 10000 && !!fleet.updatedAt && now - Date.parse(fleet.updatedAt) < 10000;
  const remaining = recommendation ? Math.max(0, Math.ceil((Date.parse(recommendation.expires_at) - now) / 1000)) : 0;
  const approvalBlocked = !fresh || !current || current.stale || !!pending || submitting || remaining <= 0;
  async function recommend() {
    recommendationRequest.current?.abort();
    const controller = new AbortController(); recommendationRequest.current = controller;
    setRecommending(true); setRecommendation(null); setError(''); setNotice('');
    try {
      const result = await api<Recommendation>('/api/rebalancing/recommendations', {
        method: 'POST', body: JSON.stringify({ target_zone_id: selected }), signal: controller.signal,
      });
      if (!controller.signal.aborted) { setRecommendation(result); setNow(Date.now()); }
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
      setPending(null); setRecommendation(null);
      setNotice(`Dispatch ${result.dispatch_id}: approval recorded. Follow its live backend status below.`);
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
  return <div className="app">
    <header><div className="brand-icon" aria-hidden="true">↗</div><div><div className="eyebrow">Operations / Singapore</div><h1>Fleet Dispatch</h1></div><span className="simulation">SIMULATION</span></header>
    <div className="service-bar"><ServiceStatus name="Demand" resource={heat}/><ServiceStatus name="Fleet" resource={fleet}/><ServiceStatus name="Dispatch" resource={dispatches}/><ServiceStatus name="Facilities (mock)" resource={facilities}/><span className="refresh-note">Auto-refresh · 2 seconds · SGT</span></div>
    <main>
      <div className="page-intro"><div><h2>A clear view. An informed move.</h2><p>Monitor demand, review a recommendation, then approve a relocation.</p></div><button onClick={() => { heat.refresh(); fleet.refresh(); dispatches.refresh(); facilities.refresh(); }}>Refresh now</button></div>
      {(heat.error || fleet.error || dispatches.error) && <p className="warning" role="status">Some services are unavailable. Retained observations are last known, not live. Healthy panels continue updating.</p>}
      {heat.data?.status === 'PARTIAL' && <p className="warning" role="status">Demand response is partial · Fleet {heat.data.fleet_status}. {heat.data.issues.join(' · ')}</p>}
      {error && <p className="warning" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}
      {pending && <div className="warning" role="status"><strong>{submitting ? 'Submitting approval…' : 'Unresolved approval'}</strong><p>Request {pending.request_id}. New approvals are paused until this request is resolved.</p><button disabled={submitting} onClick={() => void approve(pending)}>Retry same approval</button> <button onClick={dispatches.refresh}>Reconcile status</button></div>}
      <div className="workspace"><section className="panel map-panel"><div className="section-title"><h2>Singapore demand</h2><span className="muted">6 operating zones</span></div>
        <SingaporeMap zones={zones} vehicles={vehicles} facilities={sites} selected={selected} onSelect={choose}/>
        <div className="legend">Hotspot severity {['NORMAL', 'WARNING', 'CRITICAL', 'UNKNOWN'].map(value => <Badge key={value} value={value}/>)}<span>● EV location</span><span>● Mock parking (teal)</span><span>● Mock charging (amber)</span></div>
        <div className="zone-list" aria-label="Select target zone">{zones.map(z => <button key={z.zone_id} aria-pressed={selected === z.zone_id} onClick={() => choose(z.zone_id)}><strong>{z.name}</strong><Badge value={z.hotspot_severity}/></button>)}</div>
        {!zones.length && <p className="empty">Waiting for zone data from Demand Service.</p>}
        <p className="snapshot">Demand snapshot {time(heat.data?.demand_snapshot_at)} · Fleet snapshot {time(heat.data?.fleet_snapshot_at)} · Approximate simulation coordinates</p>
      </section>
      <aside className="panel target-panel">{zone ? <><div className="eyebrow">Selected destination</div><ZoneDetails zone={zone}/><hr/><button className="primary" disabled={recommending || !!pending || submitting || !fresh} onClick={() => void recommend()}>{recommending ? 'Finding a suitable EV…' : 'Recommend EV'}</button><p className="muted">A recommendation does not move an EV.</p></> : <div className="empty"><div className="empty-icon">↗</div><h2>Where is an EV needed?</h2><p>Select a zone on the map or from the list to review its demand and request a recommendation.</p></div>}
        {zone && !fresh && <p className="warning">Fresh demand and fleet data are required to request or approve a recommendation.</p>}
        {recommendation && <RecommendationPanel recommendation={recommendation} remaining={remaining} disabled={!!approvalBlocked} onReview={() => setConfirming(true)}/>}
      </aside></div>
      <FacilitiesPanel facilities={sites} selectedZone={selected} error={facilities.error}/>
      <FleetPanel vehicles={vehicles}/><RelocationPanel dispatches={dispatches.data?.dispatches ?? []} vehicles={vehicles}/>
      <footer>Human-approved relocation · Backend-owned decisions · In-memory simulation</footer>
    </main>{confirming && recommendation && <DispatchConfirmation recommendation={recommendation} vehicle={current || undefined} expired={!!approvalBlocked} onClose={() => setConfirming(false)} onApprove={() => void approve()}/>}
  </div>;
}
