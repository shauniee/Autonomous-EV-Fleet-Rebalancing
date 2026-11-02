# Strategy evaluation verification

Verified on 9 October 2026 (Singapore time).

## Evaluation completion

The final evidence run is `results/strategy-evaluation/run-20261009-final`.
It executed 10 scenarios with 30 deterministic seeds and both selection
algorithms, for 600 paired scenario trials.

| Check | Observed result |
|---|---|
| Planned and recorded scenario trials | **600 / 600** |
| Paired starting-state hash mismatches | **0** |
| Experiment infrastructure errors | **0** |
| Completion status | **Complete** |
| Raw JSONL records | **600** |
| Raw CSV records | **600** |
| Generated SVG graphs | **6** |
| Evaluation source SHA-256 | `61ecdbd9209c7020e61b755ffb23e57e9d1e48e96cfc08a7fcc03962badd42c4` |

The source hash in the manifest was recomputed after the run and matched the
delivered evaluation and selector source. Both algorithms received the same
canonical starting-state hash for every scenario and seed. A new set of
in-memory service stores was created for every algorithm trial.

## Main result

The predeclared primary effect is energy-aware minus nearest EV, with scenarios
weighted equally. Negative values favour energy-aware selection for poor
outcomes, distance and completion time.

| Metric | Energy-aware mean | Nearest-EV mean | Paired effect | 95% bootstrap CI |
|---|---:|---:|---:|---:|
| Operationally poor outcome | 0.1000 | 0.2000 | **-0.1000** | **[-0.1367, -0.0667]** |
| Actual arrival SOC | 76.6400% | 57.2616% | **+19.3784 pp** | **[+16.8253, +22.0587]** |
| Relocation distance | 13.4382 km | 10.5496 km | **+2.8886 km** | **[+2.5028, +3.3035]** |
| Completion time | 1102.9 s | 880.7 s | **+222.2 s** | **[+190.0, +253.4]** |
| Recommendation request latency | 12.4054 ms | 12.4469 ms | -0.0416 ms | [-0.4806, +0.4212] |

Energy-aware selection produced no predicted or actual arrivals below 30% SOC.
Nearest EV produced 30 predicted and 30 actual low-arrival-SOC outcomes. Each
algorithm completed 300 of 330 dispatch requests. The 30 rejected requests per
algorithm were the deliberately concurrent second claims, all retained as
`VEHICLE_VERSION_CONFLICT` outcomes. Consequently, both request-level success
rates were 0.9091.

These data support the scoped conclusion that the fixed energy-aware policy
reduced poor relocations in this simulation while increasing average distance
and completion time. Its recommendation latency was not measurably different
at this scale because the confidence interval includes zero.

## Generated evidence

- [Generated evaluation report](../results/strategy-evaluation/run-20261009-final/report.md)
- [Run manifest](../results/strategy-evaluation/run-20261009-final/manifest.json)
- [Completion record](../results/strategy-evaluation/run-20261009-final/completion.json)
- [Raw JSONL trials](../results/strategy-evaluation/run-20261009-final/raw/trials.jsonl)
- [Raw CSV trials](../results/strategy-evaluation/run-20261009-final/raw/trials.csv)
- [Scenario summaries](../results/strategy-evaluation/run-20261009-final/tables/scenario-summary.csv)
- [Paired effects](../results/strategy-evaluation/run-20261009-final/tables/paired-effects.csv)
- [Failure summary](../results/strategy-evaluation/run-20261009-final/tables/failure-summary.csv)
- [Graphs](../results/strategy-evaluation/run-20261009-final/graphs/)
- [Backend JUnit report](evidence/strategy-evaluation-backend-tests.xml)
- [Frontend JUnit report](evidence/strategy-evaluation-frontend-tests.xml)
- [Browser JUnit report](evidence/strategy-evaluation-browser-tests.xml)

## Regression verification

| Check | Observed result |
|---|---|
| Complete Python suite | **129 passed**; one existing Starlette/httpx deprecation warning |
| Frontend component and polling tests | **13 passed** |
| Playwright real-backend browser suite | **2 passed** |
| Vite production build | Passed |
| `docker compose --profile relocation config --quiet` | Passed |
| `git diff --check` | Passed; Git emitted LF/CRLF conversion notices |
| Docker container runtime | **Not verified: Docker daemon unavailable** |

The browser journey again exercised explicit human approval and real relocation
through arrival. strategy evaluation did not alter dispatch semantics, dashboard behavior,
or the default energy-aware application policy.

## Limits

The evidence uses six approximate Singapore zone points, synthetic demand and
charging data, deterministic communication outages, a small simulated fleet,
and straight-line distance with a route factor. Prediction and movement use
related energy assumptions. Recommendation latency uses in-process service
transport and must not be interpreted as deployed network latency. The fixed
weights were not tuned against these results. Persistent storage, external maps,
traffic, charging APIs and ML remain outside the evaluation scope.
