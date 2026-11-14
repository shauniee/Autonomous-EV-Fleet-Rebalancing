# Operator-requested relocation quantities

The user approved that requesting six EVs means planning six feasible relocations even when the coverage model prefers fewer. The site remains a rental-company operations dashboard; customer booking/delivery is out of scope.

## Design
- The dashboard explicitly requests FULFIL_REQUEST planning. Existing API callers default to OPTIMIZE_COVERAGE for compatibility and advisory use.
- Incoming quantities exclude EVs already assigned to the destination zone.
- Match compatible parking/charging capacity across eligible sites before route verification; retain the bounded route-search deadline.
- Rank feasible candidates using the existing scenario score, but do not reject an operator-requested move solely for insufficient gain or donor service-floor preferences. Never subtract stock that does not exist.
- Preserve fresh observations, route verification, operational battery reserve, exclusive ownership, available parking/charging capacity, expiry, deadlines, and explicit human approval.
- Save planning intent with each child recommendation and use that server-owned intent during approval revalidation.
- Show signed coverage impact and warn whenever any selected step is below the useful-gain threshold. A negative value must never be described as an improvement.
- Partial fulfilment remains possible for physical, capacity, freshness or dependency constraints, with evidence explaining the shortfall.

## Approaches
Keep useful-only optimisation (existing behavior), force all approvals (rejected because it bypasses checks), or separate operator fulfilment from advisory optimisation. The user approved fulfilment with coverage warnings and unchanged safety checks; implement the separate explicit mode.

## Scope
Use the current checkout and preserve all existing/staged changes. No customer portal, live dispatch, deployment, staging, commit, database mutation, or changes to Kubernetes are included.
