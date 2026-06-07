GitHub Autopilot — Documentation
Complete technical documentation for operators, contributors, and integrators.
Where to Start
Goal
Document
Install and configure the bot
User Setup Guide
See all slash commands with examples
Slash Commands Reference
Deploy to Render
Render Deployment Guide
Understand the system architecture
System Architecture
Understand the webhook security pipeline
Webhook Pipeline
Understand the AI routing layer
AI Routing
Understand how /autofix works
Autofix Engine
Understand the security model
Threat Model
Understand secret scanning
Secret Scanning
Monitor in production
Observability
Write or run tests
Testing Guide
View system diagrams
Diagrams
Documentation Structure
docs/
├── README.md                        ← You are here
│
├── architecture/
│   ├── system-architecture.md       ← Component map, data flow, design decisions
│   └── webhook-pipeline.md          ← 7-stage security pipeline in detail
│
├── ai-system/
│   ├── ai-routing.md                ← 4-provider router, circuit breakers, task classification
│   └── autofix-engine.md            ← /autofix 5-stage pipeline: plan → read → fix → branch → PR
│
├── security/
│   ├── threat-model.md              ← Attack vectors and mitigations
│   └── secret-scanning.md          ← 35+ patterns, entropy gating, false-positive prevention
│
├── deployment/
│   └── render-deploy.md             ← Step-by-step Render + GitHub App setup
│
├── guides/
│   ├── user-setup.md                ← First-time installation and configuration
│   └── slash-commands.md            ← All 26 commands with examples and permissions
│
├── observability/
│   └── observability.md             ← /ping, /health, /metrics, logging, alerting
│
├── testing/
│   └── testing-guide.md             ← Test patterns, mocking strategy, how to run
│
└── diagrams/
    └── diagrams.md                  ← ASCII and Mermaid diagrams
Document Descriptions
System Architecture The foundational reference document. Covers design goals, the full component map with ASCII diagram, request lifecycle across 4 phases, data flow to Redis, reliability model, failure handling, and architectural decisions with rationale (threading vs Celery, Redis SET NX, in-memory config cache).
Webhook Pipeline Deep dive into all 7 security stages. For each stage: what threat it prevents, the exact implementation, why this approach was chosen over alternatives, and failure modes.
AI Routing How the 4-provider LLM router works. Covers the single-interface design, task classification map, provider specifications, selection algorithm, circuit breaker state machine, fallback chain, and cost tracking.
Autofix Engine How /autofix creates branches and pull requests. Covers the 5-stage pipeline, file safety model (blocked paths, allowed extensions), the human confirmation step, diff preview generation, and the 70% length guard.
Threat Model All identified attack vectors with mitigations, residual risk assessment, and security boundaries.
Secret Scanning The 35+ pattern library, entropy gating logic, false-positive prevention strategy, and why scanner source files cannot contain real credential patterns.
Render Deployment Step-by-step guide: GitHub App creation, Render web service and Redis setup, environment variable configuration, health check setup, and post-deploy verification.
User Setup Guide End-to-end first-time setup: creating the GitHub App, configuring webhook permissions, installing on repositories, and verifying the installation.
Slash Commands Reference All 26 commands with syntax, examples, permission requirements, rate limits, and expected output format.
Observability The /ping, /health, and /metrics endpoints. Redis key reference for analytics. Logging structure and how to set up uptime monitoring.
Testing Guide How to write tests for this codebase: the mocking strategy for external dependencies, test structure patterns, how to run the suite, and coverage expectations.
Diagrams ASCII and Mermaid diagrams of the system: webhook flow, AI routing, autofix pipeline, data model, and deployment topology.
