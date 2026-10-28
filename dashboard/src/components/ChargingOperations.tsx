import { useState } from 'react';
import type { ChargingTask, Fleet, Vehicle } from '../api/contracts';
import { api, message } from '../api/client';
import { usePolling } from '../hooks/usePolling';

export function ChargingOperations({ vehicles, onUpdated }: {
  vehicles: Vehicle[]; onUpdated: () => void;
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
