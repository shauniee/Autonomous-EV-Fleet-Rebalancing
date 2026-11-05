import type { Vehicle } from '../api/contracts';
import { number, time } from './format';

export function SimulatorStatus({ vehicle }: { vehicle: Vehicle }) {
  const runtime = vehicle.simulator_runtime;
  if (!runtime) return <span className="muted">Not reported<small>Process health unknown</small></span>;
  const modeLabels = {
    STARTING: 'Starting', RUNNING: 'Running', RETRYING: 'Retrying communication',
    RECOVERY_WAIT: 'Waiting for recovery', STOPPED: 'Stopped',
  };
  const label = runtime.status === 'STALE' ? 'Status expired' :
    runtime.status === 'UNRESPONSIVE' ? 'Not responding' : modeLabels[runtime.mode];
  return <div className="simulator-status">
    <strong>{label}</strong>
    <small>Local coordination {runtime.ready ? 'ready' : 'not ready'}</small>
    <details><summary>Simulator details</summary>
      <small>Loop heartbeat age: {number(runtime.heartbeat_age_seconds, ' s')}</small>
      <small>Status report age: {number(runtime.report_age_seconds, ' s')}</small>
      <small>Last accepted checkpoint: {runtime.checkpoint_revision || 'None'}</small>
      <small>Checkpoint observation: {time(runtime.checkpoint_at)}</small>
      {runtime.blocking_reason && <small>Blocking reason: {runtime.blocking_reason}</small>}
    </details>
  </div>;
}
