# Documentation

Start with [architecture](architecture.md), then follow the topic you need.

## Setup

- [Setup on Windows](setup-windows.md)
- [Setup on macOS](setup-macos.md)
- [Configuration and credentials](configuration.md): `.env`, LTA DataMall, OneMap and offline demo mode

## System

- [Architecture](architecture.md): services, ownership, consistency and failure handling
- [Docker, nginx and Kubernetes](deployment.md): images, Compose, the dashboard proxy and the cluster layout
- [API contracts](api-contracts.md): endpoints per service

## Front ends

- [Operator dashboard](operator-dashboard.md): what the operator sees and does
- [Simulation console](simulation-console.md): demand, rentals, traffic and load experiments, and how they reach the dashboard

## Core concepts

- [CRDS](crds.md): how relocation recommendations are decided
- [Dispatch lifecycle](dispatch-relocation.md): approval, claims, multi-EV plans and paced bulk jobs
- [Demand and heat map](demand-heatmap.md): demand targets, shortages and severity
- [Telemetry and digital twin](telemetry.md): simulators, reporting, recovery and map animation
- [Parking, charging and handoff](facilities-and-charging.md): facilities, holds, charger advice and cable handoff

## External data

- [OneMap routing](onemap-routing.md): road routes required for approval
- [Traffic-aware routing](traffic-aware-routing.md): LTA traffic feeds and the shared Redis cache
- [LTA DataMall](lta-datamall.md): real car park and charger observations

## Running and evaluating

- [Local Kubernetes deployment](kubernetes-local.md): start, recover and troubleshoot the cluster
- [Local testing](local-testing.md): test commands and environment notes
- [Strategy evaluation](strategy-evaluation.md): the archived legacy-selector study and its results
