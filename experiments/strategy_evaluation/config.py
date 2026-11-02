from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class EvaluationConfig:
    repetitions: int = 30
    seed_start: int = 0
    low_arrival_soc_pct: float = 30.0
    speed_kph: float = 45.0
    step_seconds: float = 60.0
    bootstrap_samples: int = 2000
    analysis_seed: int = 4242

    def as_dict(self):
        return asdict(self)

