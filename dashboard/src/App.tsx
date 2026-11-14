import { useCallback, useEffect, useRef, useState } from 'react';
import { applyTheme, type Theme } from './theme';
import type { Approval, BatchApprovalLookup, BatchDispatch, BatchRecommendation, RecommendationDraft, RecommendationRequest, RecommendationRequestPage, ChargerAdvice, ChargingAdvice, TrafficSnapshot, DispatchApprovalResult, DispatchList, Facility, FacilitiesSnapshot, Fleet, HeatMap, Recommendation } from './api/contracts';
import { api, ApiError } from './api/client';
import { recommendationError } from './components/crdsMessages';
import { approvalErrorMessage, approvalStatus, batchInProgress, batchProgressLabel, dispatchApprovalMessage } from './components/approvalMessages';
import { usePendingApprovals } from './hooks/usePendingApprovals';
import { usePolling } from './hooks/usePolling';
import { OperationalTabs } from './components/OperationalTabs';
import { FleetPulse } from './components/FleetPulse';
import { SingaporeMap, type MapFocusTarget } from './components/SingaporeMap';
import { Notifications, type ActiveNotification, type Notification, type Notify } from './components/Notifications';
import { ChargingOperations } from './components/ChargingOperations';
import { TrafficConditions } from './components/TrafficConditions';
import { TrafficJourneyCheck } from './components/TrafficJourneyCheck';
import { RentalOutlook } from './components/RentalOutlook';
import { FacilitiesPanel } from './components/FacilitiesPanel';
import { BatchPanel, BatchConfirmation, BatchResult } from './components/BatchPanel';
import { Badge, number, time } from './components/format';
import { DispatchConfirmation, FleetPanel, RecommendationPanel, ServiceStatus, ZoneDemandStatus, ZoneDetails, ZoneMetrics } from './components/Panels';
import {
  RecommendationRequests, recommendationPageSize, recommendationRequestsPath,
  requestApprovalStatus, type QueuedBatchApproval,
} from './components/RecommendationRequests';
import { useBulkRecommendationApproval } from './components/BulkRecommendationApproval';
import { RecommendationVehiclePicker } from './components/RecommendationVehiclePicker';
import { RelocationPanel } from './components/RelocationPanel';
import { DemandThresholds, defaultDemandThresholds, demandLevel, demandLevels, readDemandThresholds } from './components/DemandThresholds';

type PlanningMode = NonNullable<RecommendationDraft['planning_mode']>;

const pendingKey = 'fleet.pending-approval';
const batchPendingKey = 'fleet.pending-batch';
type PendingBatchApproval = QueuedBatchApproval & { accepted?: boolean };
interface RoutingConfiguration {
  provider: 'SIMULATED' | 'ONEMAP' | 'OFFLINE_DEMO';
  configured: boolean;
  reason_code: string | null;
}
export default function App() {
  const [theme, setTheme] = useState<Theme>(() => document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
  const heat = usePolling<HeatMap>('/api/demand/heat-map');
  const [now, setNow] = useState(Date.now());
  const heatUnavailable = !!heat.error || !heat.updatedAt || now - Date.parse(heat.updatedAt) >= 10000;
  const [customThresholds, setCustomThresholds] = useState(readDemandThresholds);
  const defaultThresholds = defaultDemandThresholds(heat.data?.policy);
  const thresholds = customThresholds ?? defaultThresholds;
  const { moderate, critical } = thresholds;
  const rentalDemandLevel = useCallback((zone: HeatMap['zones'][number]) =>
    heatUnavailable ? 'Unknown' : demandLevel(zone, { moderate, critical }), [heatUnavailable, moderate, critical]);
  const fleet = usePolling<Fleet>('/api/fleet/dashboard/vehicles', 1000);
  const dispatches = usePolling<DispatchList>('/api/dispatch/dispatches');
  // "Configured" deliberately does not claim that OneMap accepted the token.
  const routing = usePolling<RoutingConfiguration>('/api/dispatch/routing/status', 30000);
  const facilities = usePolling<FacilitiesSnapshot>('/api/facilities/facilities', 10000);
  const chargingAdvice = usePolling<ChargingAdvice>('/api/rebalancing/charging-advice', 10000);
  const traffic = usePolling<TrafficSnapshot>('/api/rebalancing/traffic/conditions', 5000);
  const [selected, setSelected] = useState('');
  const [selectedDispatch, setSelectedDispatch] = useState('');
  const [operationalTab, setOperationalTab] = useState('dispatch');
  const [requestOffset, setRequestOffset] = useState(0);
  const [requestFocus, setRequestFocus] = useState<{ id: string; revision: number }>();
  const requestPage = usePolling<RecommendationRequestPage>(
    `${recommendationRequestsPath}?offset=${requestOffset}&limit=${recommendationPageSize}`,
    operationalTab === 'recommendations' ? 2000 : 4000);
  const queueEnabled = requestPage.data?.enabled === true;
  const bulkApproval = useBulkRecommendationApproval(queueEnabled, operationalTab === 'recommendations');
  const focusedRequest = usePolling<RecommendationRequest>(
    operationalTab === 'recommendations' && requestFocus &&
      !requestPage.data?.requests.some(row => row.request_id === requestFocus.id)
      ? `${recommendationRequestsPath}/${requestFocus.id}` : null, 4000);
  const requestRows = requestPage.data?.requests ?? [];
  const visibleRequests = focusedRequest.data ? [focusedRequest.data, ...requestRows] : requestRows;
  const [queuedReceipt, setQueuedReceipt] = useState<RecommendationRequest>();
  const [sendingRequest, setSendingRequest] = useState(false);
  const sendingRequestRef = useRef(false);
  const [drafts, updateDraft] = usePendingApprovals<RecommendationDraft>('fleet.pending-recommendation', 'target_zone_id');
  function openRequest(id: string) {
    setRequestOffset(0);
    setRequestFocus(previous => ({ id, revision: (previous?.revision ?? 0) + 1 }));
    setOperationalTab('recommendations');
    requestPage.refresh();
  }
  const [unparkedOnly, setUnparkedOnly] = useState(false);
  const [relocationFocus, setRelocationFocus] = useState<{ id: string; revision: number }>();
  const [compactView, setCompactView] = useState<'dispatch' | 'map'>('dispatch');
  const [demandDetails, setDemandDetails] = useState(false);
  const [mapFocus, setMapFocus] = useState<MapFocusTarget | null>(null);
  const trafficDispatchId = selectedDispatch || dispatches.data?.dispatches.filter(d =>
    d.command?.route && !['SUCCEEDED', 'REJECTED', 'FAILED'].includes(d.state)).at(-1)?.dispatch_id;
  const unresolvedRelocations = dispatches.data?.dispatches.filter(d => d.vehicle_id &&
    (d.state === 'UNKNOWN' || d.recovery?.status === 'BLOCKED' || d.recovery?.status === 'REQUESTED' || d.recovery?.status === 'RESTORING')) ?? [];
  function openRelocation(id: string) {
    setOperationalTab('relocations');
    setRelocationFocus(previous => ({ id, revision: (previous?.revision ?? 0) + 1 }));
  }
  function openPlanningRelocation(id: string) {
    openRelocation(id);
    if (!dispatches.data?.dispatches.some(d => d.dispatch_id === id)) {
      notify('warning', 'The relocation record is unavailable.');
    }
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
  const [planningMode, setPlanningMode] = useState<PlanningMode>('OPTIMIZE_COVERAGE');
  const [selectedVehicle, setSelectedVehicle] = useState('');
  const [selectedVehicles, setSelectedVehicles] = useState<string[] | null>(null);
  const [selectedFacility, setSelectedFacility] = useState('');
  const [batch, setBatch] = useState<BatchRecommendation | null>(null);
  const [batchConfirming, setBatchConfirming] = useState(false);
  const [batchResults, setBatchResults] = useState<Record<string, BatchDispatch>>({});
  const [batchProgressErrors, setBatchProgressErrors] = useState<Record<string, boolean>>({});
  const [pendingBatches, updateBatch] = usePendingApprovals<PendingBatchApproval>(batchPendingKey, 'batch_recommendation_id');
  const clearFinishedBatches = useCallback(() => {
    setBatchResults(previous => Object.fromEntries(Object.entries(previous).filter(([, result]) => batchInProgress(result))));
  }, []);
  const approvalIds = visibleRequests.flatMap(row => row.approval_request_id ? [row.approval_request_id] : []).sort();
  const queueApprovals = usePolling<BatchApprovalLookup>(
    approvalIds.length
      ? `/api/dispatch/batches?${new URLSearchParams(approvalIds.map(id => ['request_id', id]))}` : null,
    operationalTab === 'recommendations' ? 2000 : 5000);
  const [recommending, setRecommending] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [activity, setActivity] = useState<Notification[]>([]);
  const activityId = useRef(0);
  const notify = useCallback<Notify>((tone, message) => {
    const item = { id: ++activityId.current, tone, message, createdAt: new Date().toISOString() };
    setActivity(current => [item, ...current].slice(0, 20));
  }, []);
  const demandZones = heat.data?.zones;
  const previousDemandZones = useRef(new Map<string, HeatMap['zones'][number]>());
  useEffect(() => {
    if (!demandZones || heat.error) return;
    const currentZones = new Map(demandZones.map(zone => [zone.zone_id, zone]));
    for (const zone of demandZones) {
      const previous = previousDemandZones.current.get(zone.zone_id);
      if (!previous) continue;
      const before = rentalDemandLevel(previous);
      const after = rentalDemandLevel(zone);
      if (before !== after) {
        notify(after === 'Critical' || after === 'Moderate' ? 'warning' : 'info',
          `${zone.name} rental demand changed from ${before} to ${after}.`);
      }
    }
    previousDemandZones.current = currentZones;
  }, [demandZones, heat.error, rentalDemandLevel, notify]);
  const [confirming, setConfirming] = useState(false);
  const [pendingApprovals, updateApproval] = usePendingApprovals<Approval>(pendingKey, 'recommendation_id');
  const hasPending = pendingApprovals.length + pendingBatches.length > 0;
  const [submitting, setSubmitting] = useState(false);
  const recommendationRequest = useRef<AbortController | null>(null);
  const submittingRef = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    const timer = setInterval(() => setNow(Date.now()), 500);
    return () => { mounted.current = false; clearInterval(timer); recommendationRequest.current?.abort(); };
  }, []);
  useEffect(() => {
    for (const pending of pendingApprovals) {
      const matching = dispatches.data?.dispatches.find(d => d.request_id === pending.request_id);
      if (matching && approvalStatus(matching) !== 'UNKNOWN') {
        updateApproval(pending, false);
        notify(approvalStatus(matching) === 'REJECTED' ? 'error' : 'info', dispatchApprovalMessage(matching));
      }
    }
  }, [dispatches.data, pendingApprovals, updateApproval, notify]);
  const choose = useCallback((id: string) => {
    recommendationRequest.current?.abort(); setRecommending(false);
    setSelectedVehicles(null);
    setSelected(id); setDemandDetails(false); setSelectedFacility(''); setRecommendation(null); setBatch(null); clearFinishedBatches(); setConfirming(false); setBatchConfirming(false);
  }, []);
  const zones = heat.data?.zones ?? [];
  const vehicles = fleet.data?.vehicles ?? [];
  const twinVehicle = vehicles.find(v => v.vehicle_id === selectedVehicle) ||
    vehicles.find(v => v.vehicle_id === dispatches.data?.dispatches.find(d => d.dispatch_id === selectedDispatch)?.vehicle_id) || vehicles[0];
  const sites = facilities.data?.facilities ?? [];
  const liveFacilities = facilities.data?.data_source === 'LTA_DATAMALL';
  // An unknown provider must not fall back to a zone-only destination.
  // Only the batch endpoint accepts a planning mode, so a dispatcher override always uses it.
  const facilityWorkflow = facilities.data?.data_source !== 'MOCK' || quantity > 1 || purpose === 'CHARGE' || !!selectedVehicle
    || planningMode === 'FULFIL_REQUEST';
  const criticalZones = zones.filter(item => rentalDemandLevel(item) === 'Critical').length;
  const feedIssues = [heat, fleet, dispatches, facilities].filter(resource => resource.error).length;
  const feedsReady = [heat, fleet, dispatches, facilities].every(resource => resource.data);
  const zone = zones.find(z => z.zone_id === selected);
  const current = recommendation && vehicles.find(v => v.vehicle_id === recommendation.selected_vehicle_id);
  const fleetFeedHealthy = !!fleet.updatedAt && !fleet.error && now - Date.parse(fleet.updatedAt) < 4500;
  const unparkedIdle = fleetFeedHealthy ? vehicles.filter(v => !v.stale && v.connectivity === 'ONLINE' &&
    v.telemetry && !v.telemetry.parked_facility_id && !v.telemetry.active_dispatch_id &&
    v.effective_relocation_state === 'IDLE' && !v.active_claim && !v.active_rental && !v.active_charging_task) : [];
  const outsideZones = heatUnavailable ? null : heat.data?.available_outside_zones;
  const parkingUnconfirmed = !heatUnavailable && heat.data?.issues.includes('PARKING_ASSIGNMENT_UNCONFIRMED');
  function openUnparkedFleet() {
    setUnparkedOnly(true);
    setOperationalTab('fleet');
    requestAnimationFrame(() => document.getElementById('fleet-overview-title')?.focus());
  }
  const fresh = !heatUnavailable && heat.data?.fleet_status === 'FRESH'
    && !!fleet.data && !fleet.error && !!fleet.updatedAt && now - Date.parse(fleet.updatedAt) < 10000;
  const remaining = recommendation ? Math.max(0, Math.ceil((Date.parse(recommendation.expires_at) - now) / 1000)) : 0;
  const batchRemaining = batch ? Math.max(0, Math.ceil((Date.parse(batch.expires_at) - now) / 1000)) : 0;
  const facilitiesFresh = !!facilities.data && !facilities.error && !!facilities.updatedAt
    && now - Date.parse(facilities.updatedAt) < 30000;
  const chargingAdviceFresh = !!chargingAdvice.data && !chargingAdvice.error && !!chargingAdvice.updatedAt
    && now - Date.parse(chargingAdvice.updatedAt) < 30000 && now - Date.parse(chargingAdvice.data.generated_at) < 30000;
  const approvalBlocked = !fresh || !current || current.stale || submitting || remaining <= 0
    || (!!recommendation?.crds && (recommendation.crds.decision !== 'MOVE' || !recommendation.crds.approval_allowed));
  const batchBlocked = !fresh || !facilitiesFresh || submitting || batchRemaining <= 0
    || (!!batch?.crds && (batch.crds.decision !== 'MOVE' || !batch.crds.approval_allowed));
  const queuedRebalance = purpose === 'REBALANCE' && queueEnabled;
  const shortlist = queuedRebalance ? selectedVehicles : null;
  const requestQuantity = shortlist?.length ?? quantity;
  const draft = drafts.find(item => {
    const ids = item.vehicle_ids ?? (item.vehicle_id ? [item.vehicle_id] : []);
    return item.target_zone_id === selected && item.quantity === requestQuantity &&
      (item.planning_mode ?? 'OPTIMIZE_COVERAGE') === planningMode &&
      ids.length === (shortlist?.length ?? 0) && ids.every(id => shortlist?.includes(id));
  });
  async function submitRecommendation(inputs: RecommendationDraft) {
    if (sendingRequestRef.current) return;
    sendingRequestRef.current = true;
    setSendingRequest(true);
    updateDraft(inputs, true);
    try {
      const receipt = await api<RecommendationRequest>(recommendationRequestsPath, {
        method: 'POST', body: JSON.stringify(inputs),
      });
      updateDraft(inputs, false);
      setQueuedReceipt(receipt);
      requestPage.refresh();
    } catch (error) {
      if (error instanceof ApiError && (error.status === 422 ||
          ['RECOMMENDATION_REQUEST_ID_CONFLICT', 'RECOMMENDATION_QUEUE_FULL', 'RECOMMENDATION_QUEUE_DISABLED'].includes(error.message))) {
        updateDraft(inputs, false);
        notify('error', recommendationError(error));
      } else {
        notify('warning', 'Could not confirm the recommendation request. Retry the same request to check whether it was queued.');
      }
    } finally {
      sendingRequestRef.current = false;
      setSendingRequest(false);
    }
  }
  async function recommend() {
    if (queuedRebalance) {
      if (!requestQuantity) return;
      await submitRecommendation(draft ?? {
        request_id: crypto.randomUUID(), target_zone_id: selected, quantity: requestQuantity, planning_mode: planningMode,
        ...(shortlist ? { vehicle_ids: shortlist } : {}),
      });
      return;
    }
    recommendationRequest.current?.abort();
    const controller = new AbortController(); recommendationRequest.current = controller;
    setRecommending(true); setRecommendation(null); setBatch(null); clearFinishedBatches();
    try {
      if (!facilityWorkflow) {
        const result = await api<Recommendation>('/api/rebalancing/recommendations', {
          method: 'POST', timeoutMs: 135000, body: JSON.stringify({ target_zone_id: selected }), signal: controller.signal,
        });
        if (!controller.signal.aborted) setRecommendation(result);
      } else {
        const result = await api<BatchRecommendation>('/api/rebalancing/batch-recommendations', {
          method: 'POST', timeoutMs: 135000, body: JSON.stringify({ target_zone_id: selected, quantity,
            ...(purpose === 'CHARGE' ? { purpose } : { planning_mode: planningMode }),
            ...(selectedVehicle ? { vehicle_id: selectedVehicle } : {}),
            ...(purpose === 'CHARGE' && selectedFacility ? { facility_id: selectedFacility } : {}) }), signal: controller.signal,
        });
        if (!controller.signal.aborted) setBatch(result);
      }
      if (!controller.signal.aborted) setNow(Date.now());
    } catch (error) { if (!controller.signal.aborted) notify('error', recommendationError(error)); }
    finally { if (!controller.signal.aborted) setRecommending(false); }
  }
  async function approve(retry?: Approval) {
    if (submittingRef.current || (!retry && (!recommendation || approvalBlocked || Date.now() >= Date.parse(recommendation.expires_at)))) return;
    submittingRef.current = true; setSubmitting(true); setConfirming(false);
    const approval = retry ?? { request_id: crypto.randomUUID(), recommendation_id: recommendation!.recommendation_id };
    updateApproval(approval, true);
    if (!retry) setRecommendation(null);
    try {
      const result = await api<DispatchApprovalResult>('/api/dispatch/dispatches', { method: 'POST', timeoutMs: 135000, body: JSON.stringify(approval) });
      if (!mounted.current) return;
      if (result.command?.route) setSelectedDispatch(result.dispatch_id);
      const status = approvalStatus(result);
      if (status !== 'UNKNOWN') updateApproval(approval, false);
      notify(status === 'UNKNOWN' ? 'warning' : status === 'REJECTED' ? 'error' : 'info',
        dispatchApprovalMessage(result));
    } catch (error) {
      if (!mounted.current) return;
      if (error instanceof ApiError && error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
        updateApproval(approval, false); notify('error', approvalErrorMessage(error, 'REJECTED'));
      } else notify('warning', approvalErrorMessage(error, 'UNKNOWN'));
    } finally {
      submittingRef.current = false;
      if (mounted.current) { setSubmitting(false); dispatches.refresh(); fleet.refresh(); heat.refresh(); }
    }
  }
  async function approveBatch(retry?: PendingBatchApproval) {
    if (retry?.accepted || (retry && pendingBatches.some(item => item.request_id === retry.request_id && item.accepted))) return;
    if (submittingRef.current || (!retry && (!batch || batchBlocked || Date.now() >= Date.parse(batch.expires_at)))) return;
    submittingRef.current = true; setSubmitting(true); setBatchConfirming(false);
    const approval = retry ?? { request_id: crypto.randomUUID(), batch_recommendation_id: batch!.batch_recommendation_id };
    const queued = approval.queued;
    updateBatch(approval, true);
    if (!retry) setBatch(null);
    try {
      const result = await api<BatchDispatch>('/api/dispatch/batches', { method: 'POST', timeoutMs: 135000,
        body: JSON.stringify({ request_id: approval.request_id, batch_recommendation_id: approval.batch_recommendation_id }) });
      if (!mounted.current) return;
      receiveBatchResult(result, approval, true);
      const dispatched = result.items.find(item => item.dispatch_id && !['REJECTED', 'FAILED'].includes(item.state));
      if (!queued && dispatched?.dispatch_id) setSelectedDispatch(dispatched.dispatch_id);
      if (result.status === 'UNKNOWN' && !result.job_status) {
        notify('warning', 'Some batch outcomes are uncertain. Reconcile or retry using the same batch request ID.');
      }
    } catch (e) {
      if (!mounted.current) return;
      if (e instanceof ApiError && e.status >= 400 && e.status < 500 && ![408, 429].includes(e.status)) {
        if (!queued || e.message === 'BATCH_REQUEST_ID_CONFLICT') updateBatch(approval, false);
        notify('error', approvalErrorMessage(e, 'REJECTED'));
      } else notify('warning', approvalErrorMessage(e, 'UNKNOWN'));
    } finally {
      submittingRef.current = false;
      if (mounted.current) {
        setSubmitting(false); queueApprovals.refresh(); dispatches.refresh(); fleet.refresh(); heat.refresh(); facilities.refresh();
      }
    }
  }
  const reportBatchOutcome = useCallback((result: BatchDispatch) => {
    const issues = result.job_status === 'EXPIRED' || (result.job_status === 'COMPLETED'
      && result.completed_count !== result.recommended_count);
    notify(result.status === 'REJECTED' ? 'error' : issues || result.status === 'PARTIAL' ? 'warning' : 'info',
      batchInProgress(result) && result.job_status === 'EXPIRED'
        ? 'Authorization expired. Existing journeys and claim confirmation continue automatically. New starts are blocked; follow batch progress in Dispatch.'
        : batchInProgress(result)
        ? 'Human approval accepted. EVs start gradually over a 30-minute window. Temporary failures retry automatically; follow batch progress in Dispatch.'
        : `${batchProgressLabel(result)}. See the batch results for each EV. The journey status records arrival separately from admission.`);
  }, [notify]);
  const receiveBatchResult = useCallback((result: BatchDispatch, approval: PendingBatchApproval, announce = false) => {
    setBatchResults(previous => ({ ...previous, [result.request_id]: result }));
    setBatchProgressErrors(previous => ({ ...previous, [result.request_id]: false }));
    if (batchInProgress(result)) {
      // Accepted jobs survive a reload without creating another approval or recommendation.
      if (!approval.accepted) updateBatch({ ...approval, accepted: true }, true);
    } else if (result.job_status || result.status !== 'UNKNOWN') {
      updateBatch(approval, false);
    }
    if (announce && (result.job_status || result.status !== 'UNKNOWN')) reportBatchOutcome(result);
  }, [updateBatch, reportBatchOutcome]);
  useEffect(() => {
    const accepted = pendingBatches.filter(pending => pending.accepted);
    if (!accepted.length) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function pollBatches() {
      await Promise.all(accepted.map(async pending => {
        try {
          const result = await api<BatchDispatch>(`/api/dispatch/batches/${pending.request_id}`, { signal: controller.signal });
          if (controller.signal.aborted) return;
          receiveBatchResult(result, pending, !batchInProgress(result));
        } catch {
          if (!controller.signal.aborted) setBatchProgressErrors(previous => ({ ...previous, [pending.request_id]: true }));
        }
      }));
      if (!controller.signal.aborted) timer = setTimeout(() => void pollBatches(), 2000);
    }
    void pollBatches();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [pendingBatches, receiveBatchResult]);
  useEffect(() => {
    if (!queueApprovals.data) return;
    for (const pending of pendingBatches.filter(item => item.queued)) {
      if (queueApprovals.data.refusals.some(item => item.request_id === pending.request_id)) {
        updateBatch(pending, false);
        continue;
      }
      const result = queueApprovals.data.batches.find(item => item.request_id === pending.request_id);
      if (result) receiveBatchResult(result, pending);
    }
  }, [queueApprovals.data, pendingBatches, updateBatch, receiveBatchResult]);
  const [checkingApproval, setCheckingApproval] = useState(false);
  async function checkApproval(requestId: string, close = false) {
    setCheckingApproval(true);
    try {
      const result = await api<DispatchApprovalResult>(`/api/dispatch/approvals/${requestId}${close ? '/close' : ''}`,
        close ? { method: 'POST' } : undefined);
      const pending = pendingApprovals.find(item => item.request_id === requestId);
      const status = approvalStatus(result);
      if (pending && status !== 'UNKNOWN') updateApproval(pending, false);
      dispatches.refresh();
      notify(status === 'UNKNOWN' ? 'warning' : status === 'REJECTED' ? 'error' : 'info',
        dispatchApprovalMessage(result));
    } catch (error) {
      notify('warning', approvalErrorMessage(error, 'UNKNOWN', true));
    } finally { setCheckingApproval(false); }
  }
  async function reconcileBatch(requestId: string, closeItem?: string, queued = false) {
    setCheckingApproval(true);
    try {
      const path = `/api/dispatch/batches/${requestId}`;
      let result: BatchDispatch;
      if (queued && !closeItem) {
        const status = await api<BatchApprovalLookup>(`/api/dispatch/batches?request_id=${requestId}`);
        const refusal = status.refusals[0];
        if (refusal) {
          const pending = pendingBatches.find(item => item.request_id === requestId);
          if (pending) updateBatch(pending, false);
          notify('error', approvalErrorMessage(new ApiError(409, refusal.reason_code), 'REJECTED'));
          return;
        }
        if (!status.batches[0]) throw new ApiError(404, 'BATCH_NOT_FOUND');
        result = status.batches[0];
      } else {
        result = closeItem
          ? await api<BatchDispatch>(`${path}/items/${closeItem}/close`, { method: 'POST' })
          : await api<BatchDispatch>(path);
      }
      const pending = pendingBatches.find(item => item.request_id === requestId)
        ?? { request_id: result.request_id, batch_recommendation_id: result.batch_recommendation_id, ...(queued ? { queued: true as const } : {}) };
      receiveBatchResult(result, pending, true);
      if (result.status === 'UNKNOWN' && !result.job_status) notify('warning', 'Some approvals are still unconfirmed. Check the individual batch results.');
      if (!queued) setOperationalTab('dispatch');
      dispatches.refresh();
    } catch (error) {
      notify('warning', approvalErrorMessage(error, 'UNKNOWN', true));
    } finally { setCheckingApproval(false); if (queued) queueApprovals.refresh(); }
  }

  const activeNotifications: ActiveNotification[] = [];
  for (const unconfirmed of drafts) activeNotifications.push({
    id: unconfirmed.request_id, title: 'Unconfirmed recommendation request',
    message: `${zones.find(zone => zone.zone_id === unconfirmed.target_zone_id)?.name ?? unconfirmed.target_zone_id} · ${unconfirmed.quantity} EVs requested`,
    actions: <button disabled={sendingRequest || !queueEnabled}
      onClick={() => void submitRecommendation(unconfirmed)}>Retry same request</button>,
  });
  for (const pending of pendingApprovals) activeNotifications.push({
    id: pending.request_id, title: 'Unconfirmed approval',
    message: `Request ${pending.request_id}. You can continue with other relocations.`,
    actions: <>
      <button disabled={submitting || checkingApproval} onClick={() => void checkApproval(pending.request_id)}>Check status</button>
      <button disabled={submitting || checkingApproval} onClick={() => void approve(pending)}>Retry same approval</button>
    </>,
  });
  for (const pending of pendingBatches) activeNotifications.push({
    id: pending.request_id, title: pending.accepted ? 'Accepted batch in progress' : 'Unconfirmed batch approval',
    message: pending.accepted
      ? `Batch ${pending.request_id}. ${batchResults[pending.request_id]?.job_status === 'EXPIRED' ? 'Authorization expired. Existing journeys and claim confirmation continue automatically. New starts are blocked.' : 'Starts and temporary retries run automatically.'} ${batchProgressErrors[pending.request_id] ? 'Progress update unavailable; checking again automatically.' : 'See live progress in Dispatch.'}`
      : `Batch ${pending.request_id}. You can continue with other relocations.`,
    actions: <>
      <button disabled={submitting || checkingApproval} onClick={() => void reconcileBatch(pending.request_id, undefined, pending.queued)}>Check batch status</button>
      {!pending.accepted && <button disabled={submitting || checkingApproval} onClick={() => void approveBatch(pending)}>Retry same batch approval</button>}
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
  const queueAvailabilityUnknown = purpose === 'REBALANCE' && !queueEnabled && (!requestPage.data || !!requestPage.error);
  const requestBlocked = queueAvailabilityUnknown || !requestQuantity || !fresh || ((facilityWorkflow || queuedRebalance) && !facilitiesFresh);
  const readyCount = queueApprovals.data && !queueApprovals.error && !requestPage.error && !requestOffset &&
    requestPage.data?.next_offset === null
    ? requestPage.data.requests.filter(row => {
      const batchId = row.batch_recommendation_id ?? '';
      return bulkApproval.statusKnown && !bulkApproval.waitingIds.has(batchId) && !bulkApproval.plansByBatch.has(batchId)
        && requestApprovalStatus(row, now, queueApprovals.data, pendingBatches).ready;
    }).length : 0;
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
        <button className="refresh-action" onClick={() => { heat.refresh(); fleet.refresh(); dispatches.refresh(); facilities.refresh(); chargingAdvice.refresh(); traffic.refresh(); routing.refresh(); }}>Refresh now</button>
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
              <SingaporeMap zones={zones} vehicles={vehicles} placements={heatUnavailable ? null : heat.data?.vehicle_placements}
                facilities={sites} dispatches={dispatches.data?.dispatches} traffic={traffic.data}
                telemetryFeedHealthy={fleetFeedHealthy} selected={selected} onSelect={choose} selectedDispatch={selectedDispatch}
                onSelectDispatch={setSelectedDispatch} focusTarget={mapFocus} rentalDemandLevel={rentalDemandLevel}/>
              {zone && <div className="compact-map-selection">
                <strong>{zone.name}</strong><Badge value={rentalDemandLevel(zone)}/>
                <button onClick={() => {
                  setDemandDetails(true);
                  setCompactView('dispatch');
                  requestAnimationFrame(() => inspectorTitle.current?.focus());
                }}>View demand</button>
              </div>}
              <div className="map-caption">
                <div className="legend" aria-label="Rental demand level"><span>Rental demand</span>{demandLevels.map(value => <Badge key={value} value={value}/>)}</div>
                <p className="snapshot">Demand {time(heat.data?.demand_snapshot_at)} · Fleet {time(heat.data?.fleet_snapshot_at)} SGT</p>
              </div>
            </section>
            <aside tabIndex={-1} className="panel target-panel" aria-labelledby="inspector-title">
              <div className="inspector-header">
                <h2 id="inspector-title" ref={inspectorTitle} tabIndex={-1}>{demandDetails ? 'Demand & supply' : result ? 'Recommendation' : 'Dispatch'}</h2>
                {demandDetails ? <button onClick={() => { setDemandDetails(false); inspectorTitle.current?.focus(); }}>Back to {result ? 'recommendation' : 'dispatch'}</button>
                  : result ? <button onClick={editRequest}>Edit request</button> : <span className="muted">{fresh && heat.data?.status === 'COMPLETE' && (criticalZones ? `${criticalZones} ${criticalZones === 1 ? 'zone' : 'zones'} with critical demand` : 'No zones with critical demand')}</span>}
              </div>
              <div className="inspector-body" ref={inspectorBody}>
                {routing.data?.provider === 'OFFLINE_DEMO' && <section className="warning routing-setup" role="status" aria-label="Offline demo routing">
                  <strong>Offline demo routes: straight-line, not real roads.</strong>
                  <p>EVs move in straight lines and may cross buildings or water. Set <code>ROUTING_PROVIDER=ONEMAP</code> and a
                    {' '}valid <code>ONEMAP_ACCESS_TOKEN</code> for real road routes.</p>
                </section>}
                {routing.data && !routing.data.configured && <section className="warning routing-setup" role="status" aria-label="Road routing setup">
                  <strong>Road routing is not configured for dispatch.</strong>
                  <p>Demand, vehicles and the map can still be viewed, but this computer cannot verify the road routes needed for EV relocation.</p>
                  <p>In this computer's <code>.env</code>, set <code>ROUTING_PROVIDER=ONEMAP</code> and a valid
                    {' '}<code>ONEMAP_ACCESS_TOKEN</code>, then run a normal Kubernetes startup (not ConnectOnly).
                    A saved token does not guarantee the route provider is reachable.</p>
                </section>}
                {(outsideZones != null || unparkedIdle.length > 0) &&
                  <div className="fleet-coverage" aria-label="Fleet coverage">
                    {outsideZones != null && <span className="muted">Available outside zones <strong>{outsideZones}</strong></span>}
                    {unparkedIdle.length > 0 && <button type="button" className="text-button" onClick={openUnparkedFleet}>
                      {unparkedIdle.length} idle {unparkedIdle.length === 1 ? 'EV' : 'EVs'} without parking
                    </button>}
                  </div>}
                {parkingUnconfirmed && <p className="warning demand-inbound">Some parking assignments could not be verified. Zone counts use vehicle position for those EVs.</p>}
                {!result && !demandDetails && <>
                  <DemandThresholds applied={thresholds} defaults={defaultThresholds} onApply={setCustomThresholds}/>
                  <div className="zone-list" role="group" aria-label="Select target zone">{zones.map(z =>
                    <button key={z.zone_id} aria-pressed={selected === z.zone_id} onClick={() => {
                      choose(z.zone_id);
                      openMap({ zone: z });
                    }}>
                      <strong>{z.name}</strong><Badge value={rentalDemandLevel(z)}/>
                      <small>{heatUnavailable || z.current_shortage == null ? 'Shortage unknown' : z.current_shortage ? `${z.current_shortage} short now` : 'No current shortage'}</small>
                    </button>)}</div>
                  {!zones.length && <p className="empty">Waiting for zone data from Demand Service.</p>}
                  {!!zones.length && !zone && <p className="empty inspector-empty">Choose a zone to see its shortage and request a relocation.</p>}
                </>}
                {zone && (demandDetails ? <ZoneDetails zone={zone} rentalDemandLevel={rentalDemandLevel(zone)}
                  feedUnavailable={heatUnavailable} onShowOnMap={() => openMap({ zone })}/> : <section className="selected-demand" aria-label="Selected zone demand">
                  <div className="section-title"><h3><button type="button" className="text-button" aria-label={`Zoom to ${zone.name} on map`}
                    onClick={() => openMap({ zone })}>{zone.name}</button></h3>{result ? <Badge value={rentalDemandLevel(zone)}/> :
                    <button className="text-button" onClick={() => { setDemandDetails(true); inspectorTitle.current?.focus(); }}>Demand details</button>}</div>
                  {result ? heatUnavailable || zone.demand_status !== 'FRESH'
                    ? <ZoneDemandStatus zone={zone} feedUnavailable={heatUnavailable}/>
                    : <p className="muted">Shortage now {number(zone.current_shortage)} · Projected {number(zone.projected_shortage)}</p>
                    : <ZoneMetrics zone={zone} feedUnavailable={heatUnavailable}/>}
                </section>)}
                {zone && !result && !demandDetails && <section className="relocation-request" aria-label="Relocation request">
                  <div className="request-fields">
                    <div className="request-field"><label htmlFor="relocation-purpose">Destination purpose</label>
                      <div className="select-wrap"><select className="form-select" id="relocation-purpose" value={purpose} disabled={recommending || submitting} onChange={event => {
                        setPurpose(event.target.value as 'REBALANCE' | 'CHARGE'); setSelectedFacility(''); setSelectedVehicles(null);
                        clearFinishedBatches();
                      }}><option value="REBALANCE">{facilityWorkflow ? 'Automatic' : 'Rebalance'}</option><option value="CHARGE">Charging only</option></select></div>
                    </div>
                    <div className="request-field request-count"><label htmlFor="ev-count">EV count</label><input id="ev-count" aria-label="Number of EVs needed" type="number" min={1} max={50} value={requestQuantity} disabled={recommending || submitting || shortlist !== null || (!queuedRebalance && !!selectedVehicle)} onChange={event => {
                      setQuantity(Math.max(1, Math.min(50, Math.trunc(Number(event.target.value) || 1))));
                      clearFinishedBatches();
                    }}/></div>
                    {queuedRebalance ? <div className="request-field request-vehicle">
                      <label htmlFor="relocation-vehicle-mode">Vehicle selection</label>
                      <div className="select-wrap"><select className="form-select" id="relocation-vehicle-mode"
                        value={shortlist === null ? 'any' : 'specific'} disabled={sendingRequest || submitting}
                        onChange={event => setSelectedVehicles(event.target.value === 'any' ? null : [])}>
                        <option value="any">Any eligible EV</option><option value="specific">Specific EVs</option>
                      </select></div>
                    </div> : <div className="request-field request-vehicle"><label htmlFor="relocation-vehicle">Vehicle</label>
                      <div className="select-wrap"><select className="form-select" id="relocation-vehicle" value={selectedVehicle} disabled={recommending || submitting} onChange={event => {
                        setSelectedVehicle(event.target.value); setSelectedFacility(''); if (event.target.value) setQuantity(1);
                        clearFinishedBatches();
                      }}><option value="">Any eligible</option>{vehicles.map(vehicle => <option key={vehicle.vehicle_id} value={vehicle.vehicle_id} disabled={vehicle.stale || vehicle.effective_availability !== 'AVAILABLE'}>{vehicle.vehicle_id} · {vehicle.telemetry?.soc_pct.toFixed(1) ?? '—'}% · {vehicle.effective_availability}</option>)}</select></div>
                    </div>}
                    {purpose === 'REBALANCE' && <div className="request-field request-planning"><label htmlFor="planning-mode">Planning mode</label>
                      <div className="select-wrap"><select className="form-select" id="planning-mode" value={planningMode} disabled={recommending || sendingRequest || submitting} onChange={event => {
                        setPlanningMode(event.target.value as PlanningMode); clearFinishedBatches();
                      }}><option value="OPTIMIZE_COVERAGE">Optimise coverage</option><option value="FULFIL_REQUEST">Fulfil exact quantity</option></select></div>
                      <p className="muted">{planningMode === 'FULFIL_REQUEST'
                        ? 'Overrides CRDS: plans the requested number of safe moves even if they lose EV cover elsewhere. Trade-offs are shown before approval.'
                        : 'CRDS only proposes moves that gain EV cover in every scenario it checks, and otherwise recommends waiting.'}</p></div>}
                  </div>
                  {shortlist !== null && <RecommendationVehiclePicker vehicles={vehicles} selected={shortlist}
                    onChange={setSelectedVehicles} disabled={sendingRequest || submitting}/>}
                  {purpose === 'CHARGE' ? <p className="muted">After arrival, ground staff connect the cable. Simulated charging runs to 80%; staff then disconnect it before the EV returns to service.</p> : facilityWorkflow && <p className="muted">Automatic chooses charging below 35% predicted arrival SOC, or parking otherwise.</p>}
                  {selectedFacility && <p className="muted">Charging advisory destination: <strong>{selectedFacility}</strong> (exact facility required). <button disabled={recommending || submitting} onClick={() => setSelectedFacility('')}>Clear advised charger</button></p>}
                </section>}
                {!demandDetails && recommendation && <RecommendationPanel key={recommendation.recommendation_id} recommendation={recommendation} onRelocation={openPlanningRelocation}/>}
                {!demandDetails && batch && <BatchPanel key={batch.batch_recommendation_id} batch={batch} onRelocation={openPlanningRelocation}/>}
                {!demandDetails && result?.crds?.decision === 'DEFER' && unresolvedRelocations.length > 0 &&
                  <section className="assessment-followup" aria-label="Relocations to review">
                    <h3>Unresolved relocations</h3>
                    {unresolvedRelocations.map(d => <button key={d.dispatch_id} onClick={() => openRelocation(d.dispatch_id)}>Review {d.vehicle_id ?? d.dispatch_id} relocation / recovery</button>)}
                  </section>}
              </div>
              {((zone && !demandDetails) || hasPending) && <div className="inspector-actions">
                {(hasPending) && <button className="approval-notification-link" onClick={() => {
                  setNotificationsOpen(true);
                  requestAnimationFrame(() => document.getElementById('notifications-title')?.focus());
                }}>{submitting ? 'Approval in progress' : 'Resolve pending approval'}</button>}
                {zone && !demandDetails && (result ? <>
                  {result.crds?.decision === 'DEFER' ?
                    <button className="primary" disabled={recommending || requestBlocked} onClick={() => {
                      shouldFocusResult.current = true;
                      void recommend();
                    }}>{recommending ? 'Finding suitable EVs…' : 'Request fresh recommendation'}</button> : <>
                      {resultRemaining > 0 ? <span className="muted">Expires in {resultRemaining}s</span> : <span className="warning">Expired · edit request to start again</span>}
                      {resultRemaining > 0 && (recommendation?.selected_vehicle_id || !!batch?.recommended_count) &&
                        (!fresh || (batch && !facilitiesFresh) || (recommendation && (!current || current.stale))) &&
                        <span className="warning">{!fresh ? 'Wait for current demand and fleet data before reviewing.'
                          : batch && !facilitiesFresh ? 'Wait for current parking availability before reviewing.'
                            : 'Wait for a current update from the recommended EV before reviewing.'}</span>}
                      <button className="primary" disabled={!canReview} onClick={() => recommendation ? setConfirming(true) : setBatchConfirming(true)}>Review dispatch</button>
                    </>}
                </> : <>
                  {requestBlocked && <p className="warning">{queueAvailabilityUnknown ? 'Waiting for recommendation service status.'
                    : !requestQuantity ? 'Select at least one EV.'
                    : !fresh ? 'Fresh demand and fleet data are required.' : 'Fresh facility availability is required.'}</p>}
                  <button className="primary" disabled={recommending || sendingRequest || (requestBlocked && !(queuedRebalance && draft))} onClick={() => {
                    shouldFocusResult.current = true;
                    void recommend();
                  }}>{sendingRequest ? 'Submitting request…' : recommending ? 'Finding suitable EVs…'
                    : queuedRebalance ? draft ? 'Retry same request' : 'Request recommendation'
                    : quantity === 1 ? 'Recommend EV' : `Recommend ${quantity} EVs`}</button>
                  {queuedRebalance && queuedReceipt && <div className="queued-receipt" role="status">
                    <span>{queuedReceipt.status === 'QUEUED' ? 'Request queued' : 'Request received'} for {zones.find(z => z.zone_id === queuedReceipt.target_zone_id)?.name ?? queuedReceipt.target_zone_id}.</span>
                    <button className="text-button" onClick={() => openRequest(queuedReceipt.request_id)}>View request</button>
                  </div>}
                  <span className="muted">Dispatch requires your approval.</span>
                </>)}
              </div>}
            </aside>
          </div>
          {Object.values(batchResults).map(result => <BatchResult key={result.request_id} result={result}
            onRelocation={openRelocation} busy={submitting || checkingApproval} progressError={batchProgressErrors[result.request_id]}
            onClose={item => void reconcileBatch(result.request_id, item)}/>)}
        </> },
        { id: 'recommendations', label: 'Recommendations', summary: readyCount ? `${readyCount} ready` : undefined,
          content: <RecommendationRequests bulk={bulkApproval} page={requestPage} approvals={queueApprovals} pending={pendingBatches}
            active={operationalTab === 'recommendations'} now={now} offset={requestOffset}
            rows={visibleRequests} focusUnavailable={!!focusedRequest.error}
            onPage={offset => { setRequestFocus(undefined); setRequestOffset(offset); }}
            focus={requestFocus} zoneName={id => zones.find(z => z.zone_id === id)?.name ?? id}
            onMap={id => {
              const target = zones.find(z => z.zone_id === id);
              if (target) { choose(id); openMap({ zone: target }); }
              else notify('warning', 'This zone is unavailable on the map.');
            }}
            onRevise={queueEnabled ? row => {
              const target = zones.find(zone => zone.zone_id === row.target_zone_id);
              if (!target) { notify('warning', 'This zone is unavailable on the map.'); return; }
              choose(target.zone_id);
              setPurpose('REBALANCE');
              setPlanningMode(row.planning_mode ?? 'OPTIMIZE_COVERAGE');
              setQuantity(row.quantity);
              setSelectedVehicles(row.vehicle_ids ?? (row.vehicle_id ? [row.vehicle_id] : null));
              setMapFocus({ zone: target });
              setOperationalTab('dispatch');
              setCompactView('dispatch');
              requestAnimationFrame(() => document.getElementById('relocation-vehicle-mode')?.focus());
            } : undefined}
            onRelocation={openRelocation} onApprove={approval => void approveBatch(approval)}
            onCheck={(id, item) => void reconcileBatch(id, item, true)}
            approvalBusy={submitting || checkingApproval} feedsFresh={fresh && facilitiesFresh}/> },
        { id: 'fleet', label: 'Fleet', content: <>
          <FleetPulse rentalDemandLevel={rentalDemandLevel} fleet={fleet} heat={heat} dispatches={dispatches} now={now}/>
          <FleetPanel vehicles={unparkedOnly ? unparkedIdle : vehicles} unparkedOnly={unparkedOnly} fleetFeedHealthy={fleetFeedHealthy}
            initialVehicle={unparkedOnly ? unparkedIdle[0]?.vehicle_id : twinVehicle?.vehicle_id}
            onClearFilter={() => setUnparkedOnly(false)}/>
        </> },
        { id: 'relocations', label: 'Relocations',
          summary: dispatches.updatedAt && !dispatches.error && now - Date.parse(dispatches.updatedAt) < 10000
            ? `${dispatches.data?.dispatches.filter(d => !['SUCCEEDED', 'REJECTED', 'FAILED'].includes(d.state)).length ?? 0} unfinished`
            : 'unavailable',
          content: <RelocationPanel key={relocationFocus?.revision} focusId={relocationFocus?.id}
            zones={zones} facilities={sites} notify={notify} approvalBusy={submitting || checkingApproval}
            onCloseApproval={id => void checkApproval(id, true)}
            onRetryApproval={d => {
              if (d.recommendation_id) void approve({ request_id: d.request_id, recommendation_id: d.recommendation_id });
            }} dispatches={dispatches.data?.dispatches ?? []} vehicles={vehicles} onTrack={id => {
            const target = dispatches.data?.dispatches.find(dispatch => dispatch.dispatch_id === id)?.command?.target_zone_id;
            if (target && target !== selected) choose(target);
            openMap({ dispatchId: id });
          }}/> },
        { id: 'charging', label: 'Charging',
          summary: fleetFeedHealthy
            ? `EVs ≤30% SOC: ${vehicles.filter(v => !v.stale && v.connectivity === 'ONLINE' && v.telemetry && v.telemetry.soc_pct <= 30).length}`
            : 'unavailable',
          content: <ChargingOperations notify={notify} vehicles={vehicles}
            fleetFresh={!fleet.error && !!fleet.updatedAt && now - Date.parse(fleet.updatedAt) < 10000} onUpdated={() => fleet.refresh()}
            advice={chargingAdvice.data} adviceError={chargingAdvice.error} adviceFresh={chargingAdviceFresh}
            canPrepare={!!fresh && facilitiesFresh && !submitting && !recommending}
            onPrepare={(item: ChargerAdvice) => {
              if (!item.charger || !chargingAdviceFresh || !fresh || !facilitiesFresh
                  || submitting || recommending) return;
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
