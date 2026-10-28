import { useState } from 'react';
import type { ChargerAdvice, ChargingAdvice, ChargingTask, Fleet, Vehicle } from '../api/contracts';
import { api, message } from '../api/client';
import { usePolling } from '../hooks/usePolling';

export function ChargingOperations({ vehicles, onUpdated, advice, adviceError, adviceFresh, canPrepare, onPrepare }: {
  vehicles: Vehicle[]; onUpdated: () => void;
  advice?: ChargingAdvice; adviceError?: string; adviceFresh: boolean;
  canPrepare: boolean; onPrepare: (item: ChargerAdvice) => void;
}) {
  const tasks = usePolling<{ tasks: ChargingTask[] }>('/api/fleet/charging-tasks');
  const [staff, setStaff] = useState('Ground Team A');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const taskDataValid = Array.isArray(tasks.data?.tasks);
  const active = taskDataValid ? tasks.data!.tasks.filter(t => !['COMPLETED', 'CANCELLED'].includes(t.status)) : [];
  const low = vehicles.filter(v => v.telemetry && v.telemetry.soc_pct <= 30 && !v.stale && v.connectivity === 'ONLINE')
    .sort((a,b) => (a.telemetry?.soc_pct ?? 100) - (b.telemetry?.soc_pct ?? 100));

  async function latestVehicle(id: string) {
    const snapshot = await api<Fleet>('/api/fleet/vehicles');
    const current = snapshot.vehicles.find(v => v.vehicle_id === id);
    if (!current) throw new Error('Vehicle no longer exists');
    return current;
  }
  async function create(vehicle: Vehicle) {
    setBusy(true); setNotice('');
    try {
      const latest = await latestVehicle(vehicle.vehicle_id);
      await api('/api/fleet/charging-tasks', {
        method: 'POST',
        body: JSON.stringify({ vehicle_id: vehicle.vehicle_id, expected_version: latest.state_version, request_id: crypto.randomUUID() }),
      });
      setNotice(`Charging task queued for ${vehicle.vehicle_id}.`);
      tasks.refresh(); onUpdated();
    } catch (error) { setNotice(`Unable to queue task: ${message(error)}. Refresh and retry.`); }
    finally { setBusy(false); }
  }
  async function transition(task: ChargingTask, action: 'ASSIGN' | 'START_WORK' | 'COMPLETE' | 'CANCEL') {
    setBusy(true); setNotice('');
    try {
      const current = await latestVehicle(task.vehicle_id);
      await api(`/api/fleet/charging-tasks/${task.task_id}/transition`, {
        method: 'POST',
        body: JSON.stringify({
          request_id: crypto.randomUUID(), expected_version: current.state_version,
          action, ...(action === 'ASSIGN' ? { staff_name: staff.trim() } : {}),
        }),
      });
      setNotice(`Task ${task.task_id.slice(0, 8)} updated.`);
      tasks.refresh(); onUpdated();
    } catch (error) { setNotice(`Task not updated: ${message(error)}. Refresh and retry.`); }
    finally { setBusy(false); }
  }
  return <section className="panel" aria-label="Charging operations">
    <div className="section-title"><h2>Battery alerts & ground-staff charging log</h2>
      <span className="muted">{low.length} low-SOC EVs · {active.length} open tasks</span></div>
    <p className="muted">Simulated threshold: 30% low, below 20% critical. Creating a task reserves the EV from new rentals and relocations; it does not start a real charger.</p>
    {tasks.error && <p className="warning" role="status">Charging task service unavailable. Do not assume an empty queue.</p>}
    {notice && <p role="status">{notice}</p>}
    <div className="table-scroll"><table><thead><tr><th>EV</th><th>Current SOC</th><th>Alert</th><th>Task</th></tr></thead>
      <tbody>{low.map(v => <tr key={v.vehicle_id}><td>{v.vehicle_id}</td>
        <td>{v.telemetry!.soc_pct.toFixed(1)}%</td>
        <td><strong>{v.telemetry!.soc_pct < 20 ? 'CRITICAL' : 'LOW'}</strong></td>
        <td>{v.active_charging_task ? v.active_charging_task.status
          : <button disabled={busy || !!tasks.error || !taskDataValid || !!v.active_rental || !!v.active_claim || v.effective_relocation_state !== 'IDLE'}
              onClick={() => void create(v)}>Create charging task</button>}</td>
      </tr>)}</tbody></table></div>
    {!low.length && <p className="muted">No fresh online EV telemetry below the alert threshold.</p>}
    <h3>Suggested charging destinations (25% SOC or lower)</h3>
    <p className="muted">Read-only backend advice ranks reachable chargers using current battery, estimated travel energy, 20% default arrival reserve, and simulated available charging capacity. The recommendation is not a reservation or dispatch; the operator must request, review and approve a fresh route.</p>
    {(!adviceFresh || adviceError) && <p className="warning" role="status">Charging advice unavailable or stale. Do not assume there are no low-battery vehicles.</p>}
    {!advice && <p className="muted">Awaiting battery advice from Rebalancing.</p>}
    {adviceFresh && advice?.vehicles.length === 0 && <p className="muted">No EVs at or below the advisory threshold in the current Fleet snapshot.</p>}
    {advice && advice.vehicles.length > 0 && <div className="table-scroll"><table aria-label="Suggested chargers for low-battery vehicles">
      <thead><tr><th>EV / SOC</th><th>Suggested charging destination</th><th>Distance estimate</th><th>Arrival SOC estimate</th><th>Action</th></tr></thead>
      <tbody>{advice.vehicles.map(item => <tr key={item.vehicle_id}>
        <td>{item.vehicle_id} · {item.current_soc_pct.toFixed(1)}%</td>
        <td>{item.charger ? <>{item.charger.facility_name} ({item.charger.zone_id})<small>{item.charger.charging_available} simulated charging slot(s) currently available</small></>
          : <span className="warning">{item.status === 'INELIGIBLE' ? 'EV not eligible' : 'No safe reachable charger'} · {item.reasons.join(', ')}</span>}</td>
        <td>{item.charger ? item.charger.distance_km.toFixed(2) + ' km' : '—'}</td>
        <td>{item.charger ? item.charger.predicted_arrival_soc_pct.toFixed(1) + '%' : '—'}</td>
        <td>{item.charger && <button disabled={!adviceFresh || !canPrepare} onClick={() => onPrepare(item)}>Prepare charger route</button>}</td>
      </tr>)}</tbody>
    </table></div>}
    <p className="muted">A prepared route selects this exact charger in the Relocation request form. Press Recommend EV, check the new recommendation, then approve separately. No automatic dispatch.</p>
    <h3>Ground-staff task log</h3>
    <label>Assigned staff or team <input value={staff} maxLength={64} onChange={e => setStaff(e.target.value)} /></label>
    <div className="table-scroll"><table><thead><tr><th>EV</th><th>Task status</th><th>Staff</th><th>Action</th></tr></thead>
      <tbody>{active.map(t => <tr key={t.task_id}><td>{t.vehicle_id}</td><td>{t.status}</td>
        <td>{t.assigned_staff ?? 'Unassigned'}</td>
        <td>{t.status === 'PENDING' && <>
          <button disabled={busy || !staff.trim()} onClick={() => void transition(t, 'ASSIGN')}>Assign</button>{' '}
          <button disabled={busy} onClick={() => void transition(t, 'CANCEL')}>Cancel</button></>}
          {t.status === 'ASSIGNED' && <>
            <button disabled={busy} onClick={() => void transition(t, 'START_WORK')}>Start work</button>{' '}
            <button disabled={busy} onClick={() => void transition(t, 'CANCEL')}>Cancel</button></>}
          {t.status === 'IN_PROGRESS' && <button disabled={busy}
            onClick={() => void transition(t, 'COMPLETE')}>Confirm charged</button>}</td>
      </tr>)}</tbody></table></div>
    {!active.length && <p className="muted">No open ground-staff charging tasks.</p>}
    <p className="muted">Completion is rejected unless fresh EV telemetry reports charging COMPLETE and SOC at least 50%. Assignment and start-work actions are operational records, not proof that charging has occurred.</p>
  </section>;
}
