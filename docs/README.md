# GitHub Autopilot — Documentation

> Self-hosted AI assistant for GitHub repositories.

---

## Where to Start

| I want to… | Go here |
|------------|---------|
| Install the bot on my repository | [User Setup Guide →](guides/user-setup.md) |
| See all slash commands | [Slash Commands Reference →](guides/slash-commands.md) |
| Deploy to Render | [Render Deployment Guide →](deployment/render-deploy.md) |
| Understand how the system works | [System Architecture →](architecture/system-architecture.md) |
| Understand the security model | [Threat Model →](security/threat-model.md) |
| Understand the AI routing layer | [AI Routing →](ai-system/ai-routing.md) |
| Understand how `/autofix` works | [Autofix Engine →](ai-system/autofix-engine.md) |
| Monitor in production | [Observability →](observability/observability.md) |
| Write or run tests | [Testing Guide →](testing/testing-guide.md) |

---

## Documents

### Getting Started

**[User Setup Guide](guides/user-setup.md)**
End-to-end installation guide. Covers GitHub App creation, Render deployment, Redis setup, environment variables, and first-command verification. Start here.

**[Slash Commands Reference](guides/slash-commands.md)**
All 26 commands with syntax, examples, permission requirements, and expected output.

---

### Deployment

**[Render Deployment Guide](deployment/render-deploy.md)**
Step-by-step guide to deploying on Render's free tier. Covers web service configuration, Redis provisioning, environment variables, health check setup, and post-deploy verification.

---

### Architecture

**[System Architecture](architecture/system-architecture.md)**
The foundational reference. Covers the component map, request lifecycle, data flow, reliability model, failure handling, and key design decisions.

**[Webhook Pipeline](architecture/webhook-pipeline.md)**
Deep dive into the 7-stage security pipeline. For each stage: what threat it prevents, the implementation, and failure modes.

---

### AI System

**[AI Routing](ai-system/ai-routing.md)**
How the 4-provider LLM router works. Covers provider selection, task classification, circuit breakers, fallback chain, and cost tracking.

**[Autofix Engine](ai-system/autofix-engine.md)**
How `/autofix` creates branches and pull requests. Covers the 5-stage pipeline, file safety model, human confirmation step, and diff preview.

---

### Security

**[Threat Model](security/threat-model.md)**
All identified attack vectors with mitigations and residual risk assessment.

**[Secret Scanning](security/secret-scanning.md)**
The 35+ pattern library, entropy gating, and false-positive prevention strategy.

---

### Operations

**[Observability](observability/observability.md)**
The `/ping`, `/health`, and `/metrics` endpoints. Redis key reference. Logging structure and uptime monitoring setup.

**[Testing Guide](testing/testing-guide.md)**
How to write tests: mocking strategy, test patterns, how to run the suite.

**[Diagrams](diagrams/diagrams.md)**
ASCII and Mermaid diagrams — webhook flow, AI routing, autofix pipeline, deployment topology.

---

## File Structure

```
docs/
├── README.md                    ← You are here
├── guides/
│   ├── user-setup.md
│   └── slash-commands.md
├── deployment/
│   └── render-deploy.md
├── architecture/
│   ├── system-architecture.md
│   └── webhook-pipeline.md
├── ai-system/
│   ├── ai-routing.md
│   └── autofix-engine.md
├── security/
│   ├── threat-model.md
│   └── secret-scanning.md
├── observability/
│   └── observability.md
├── testing/
│   └── testing-guide.md
└── diagrams/
    └── diagrams.md
```
