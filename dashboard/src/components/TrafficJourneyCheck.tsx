import { usePolling } from '../hooks/usePolling';
import type { TrafficRouteAssessment } from '../api/contracts';

interface JourneyTraffic {
  status: string; action: 'NONE' | 'OPERATOR_REVIEW_REQUIRED';
  reason: string; assessment?: TrafficRouteAssessment;
}
export function TrafficJourneyCheck({ dispatchId }: { dispatchId: string }) {
  const result = usePolling<JourneyTraffic>(`/api/dispatch/dispatches/${dispatchId}/traffic-check`, 15000);
  const report = result.data;
  return <section className="panel" aria-label="Active journey traffic check">
    <div className="section-title"><h2>Active journey traffic assessment</h2><strong>{report?.status || 'WAITING'}</strong></div>
    {result.error && <p className="warning">Traffic check unavailable: {result.error}. The EV's issued route is unchanged.</p>}
    {report && <>
      <p>{report.reason.replaceAll('_', ' ')}</p>
      {report.action === 'OPERATOR_REVIEW_REQUIRED' && <p className="warning" role="alert">
        Road conditions may have changed. Review this journey. No automatic mid-journey route switch occurs;
        the current immutable command and the 50 m arrival safety check remain in force.
      </p>}
      {report.assessment && <p className="muted">Observed incidents: {report.assessment.incidents};
        roadworks: {report.assessment.roadworks}; matched speed segments: {report.assessment.matched_speed_segments}.
        Estimated traffic factor: {report.assessment.delay_factor ?? 'unknown'}.</p>}
    </>}
  </section>;
}
