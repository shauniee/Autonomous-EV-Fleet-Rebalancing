# strategy evaluation evaluation report

Energy-aware selection measurably reduced operationally poor outcomes in this simulation; the paired 95% confidence interval excluded zero.

The primary paired effect is energy-aware minus nearest EV. Negative values favour energy-aware selection for poor-outcome rate, distance, and time.

## Equal-scenario descriptive means

| Metric | Energy-aware | Nearest EV |
|---|---:|---:|
| operationally_poor_outcome | 0.1000 | 0.2000 |
| actual_arrival_soc_pct | 76.6400 | 57.2616 |
| actual_relocation_distance_km | 13.4382 | 10.5496 |
| relocation_completion_time_s | 1102.9000 | 880.7000 |
| recommendation_request_latency_ms | 12.4054 | 12.4469 |

## Request outcomes

Concurrent trials contain two approval requests. Request-level success is therefore reported separately from the equal-scenario mean.

| Algorithm | Successful requests | Total requests | Rate |
|---|---:|---:|---:|
| energy_aware | 300 | 330 | 0.9091 |
| nearest_ev | 300 | 330 | 0.9091 |

## Interpretation limits

- Six approximate Singapore simulation points; no road routing or traffic.
- Synthetic demand, charging and scheduled communication outages.
- Prediction and movement use related energy assumptions.
- In-process service transport; latency is not deployed network performance.
- Fixed policy weights were not tuned with these results.
- Missing arrival values from failed trials are retained as null and failure rates are reported separately.
