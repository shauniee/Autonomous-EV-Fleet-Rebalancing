# User-Controlled Demand Simulation Design

## Intent

Keep simulated zone demand fixed unless an operator explicitly starts the demand generator from the local Simulation Console. This separates demand generation from customer rentals and prevents background demand changes from confusing rebalancing observations.

## Approved behavior

- Demand simulation starts paused until an operator explicitly starts it.
- The operator's last explicit Start/Pause choice survives Demand service and Pod restarts.
- The Simulation Console provides explicit **Start demand simulation** and **Pause demand simulation** actions.
- Starting performs one immediate tick, then continues on the configured 30-second interval.
- Pausing stops future ticks without clearing or rewriting the last demand observations.
- Existing manual demand values and their hold behavior remain unchanged.
- The dashboard continues applying its existing five-minute observation freshness rule; paused values may therefore become unavailable and display as dashes.
- Customer rental simulation remains independently controlled by its existing Start/Stop actions.

## Approach

Add an in-process lifecycle controller to the Demand service and expose idempotent start/pause endpoints. Proxy those endpoints through the local-only Simulation Console backend and add a focused controller to the Demand workspace. Set the Kubernetes default to paused instead of mutating ConfigMaps, scaling the Demand service, or restarting Pods for each toggle.

## Constraints and non-goals

- Do not stop or scale the Demand service; it must continue serving zone observations while simulation is paused.
- A legacy snapshot without an operator choice defaults to paused. Normal service shutdown must not overwrite an explicit running choice.
- Do not change demand generation ranges, intervals, random seed behavior, manual demand semantics, rentals, or rebalancing policy.
- Start/pause must be idempotent and must not create duplicate background tasks.
- No deployment or live cluster mutation is part of this implementation.

## Alternatives considered

- Kubernetes ConfigMap mutation and Pod restart: rejected because it disrupts the Demand API and requires broader cluster permissions.
- One generated snapshot per click: rejected because it does not provide the requested optional live simulation.

