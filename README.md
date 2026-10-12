# Kybernaut

**Agentic AIOps for Infrastructure Reliability & Cost Optimization**

Kybernaut is an infrastructure reliability project that monitors services, detects incidents, diagnoses failures, recommends recovery actions, and supports approval-gated remediation with verification and audit logging.

The goal is to build a safer, more reliable approach to infrastructure operations by combining monitoring, rule-based diagnosis, controlled recovery, and traceable remediation.

## Key Features

- **Incident Detection:** Detect unhealthy services using Prometheus metrics.
- **Incident Diagnosis:** Analyze configured service-health and application-performance signals.
- **Decision Engine:** Assign incident priorities and recommend investigation or recovery actions.
- **Approval-Gated Remediation:** Require an explicit approval flag and a valid approval token before a remediation request can proceed.
- **Restricted Recovery:** Use an allowlisted remediation helper to control which services can be restarted.
- **Health Verification:** Check service health before and after a restart.
- **Cooldown and Retry Limits:** Apply remediation guardrails to limit repeated attempts.
- **Incident Lifecycle:** Persist incidents and track their status in SQLite.
- **Audit Logging:** Record remediation outcomes in a persistent audit log.
- **Automated Tests:** Test remediation reservation, cooldown, retry limits, and outcome updates.

## Architecture

```text
        Prometheus
            |
            v
    Incident Detection
            |
            v
     Incident Diagnosis
            |
            v
      Decision Engine
            |
            v
     Approval Required
            |
            v
    Guardrail Validation
            |
            v
 Restricted Remediation Helper
            |
            v
    Health Verification
            |
            v
       Audit Logging
```

The decision engine is rule-based. Automatic remediation is disabled; recovery requests require explicit approval and validation.

## Technology Stack

| Component | Technology |
|---|---|
| API | Python, FastAPI |
| Validation | Pydantic |
| Monitoring | Prometheus |
| Service Isolation | Docker, Docker Compose |
| Incident Storage | SQLite |
| Testing | pytest |
| HTTP Requests | HTTPX |
| Configuration | Environment variables and `.env` |

## Monitored Services

Kybernaut currently allowlists these target services:

- Order Service — port `8001`
- Payment Service — port `8002`
- User Service — port `8003`
- Inventory Service — port `8004`

The monitoring configuration is maintained in `monitoring/prometheus/prometheus.yml`.

## API Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/` | Project information and workflow |
| `GET` | `/health` | API health check |
| `GET` | `/incidents` | Detect unhealthy services |
| `GET` | `/diagnose/{service}` | Diagnose a monitored service |
| `GET` | `/decide/{service}` | Generate a recommended action |
| `POST` | `/remediate/{service}` | Request controlled remediation |

Interactive API documentation is available at `/docs` when the API is running.

### Remediation Safety

The remediation endpoint requires the `X-Approval-Token` header and a request body containing an explicit approval flag and incident ID.

Example request body:

```json
{
  "approved": true,
  "incident_id": "YOUR_ACTIVE_INCIDENT_ID"
}
```

Replace the example incident ID with the current incident ID returned by Kybernaut. A valid token and approval flag do not guarantee execution: the service also validates the incident state, checks health, and applies remediation guardrails.

Never commit real approval tokens or environment secrets to version control.

## Project Structure

```text
kybernaut/
├── app/
│   ├── main.py
│   ├── database.py
│   └── services/
│       ├── decision_engine.py
│       ├── incident_detector.py
│       ├── incident_diagnoser.py
│       ├── incident_repository.py
│       ├── remediation_repository.py
│       └── remediation_service.py
├── config/
├── monitoring/
│   └── prometheus/
│       └── prometheus.yml
├── scripts/
├── target_apps/
│   ├── inventory_service/
│   ├── order_service/
│   ├── payment_service/
│   └── user_service/
├── tests/
│   └── test_remediation_repository.py
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## Getting Started

### Prerequisites

- Python and a compatible virtual environment
- Docker Engine and Docker Compose
- Git
- A configured Prometheus instance through the provided monitoring setup

### 1. Clone the repository

```bash
git clone https://github.com/Janviswa/kybernaut.git
cd kybernaut
```

### 2. Create and activate a virtual environment

On Ubuntu:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure the environment

Create a local `.env` file:

```bash
nano .env
```

Configure `KYBERNAUT_REMEDIATION_TOKEN` with a strong, randomly generated secret. The application requires this variable at startup. Add any other environment settings required by your local deployment.

Keep `.env` private and never commit it.

### 5. Start the target services and monitoring

Review `docker-compose.yml` and `monitoring/prometheus/prometheus.yml` to understand the configured services, ports, and scrape targets. Then start the stack:

```bash
sudo docker compose up -d --build
```

Verify the containers:

```bash
sudo docker compose ps
```

### 6. Start the Kybernaut API

In a separate terminal, from the project root with the virtual environment activated:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open the API documentation at:

`http://localhost:8000/docs`

### 7. Run the tests

```bash
python -m pytest -q
```

## Safety Design

Kybernaut incorporates several controls around remediation:

- Service allowlisting prevents arbitrary service names from being accepted.
- A valid approval token is required by the API.
- The request must explicitly set `approved` to `true`.
- The remediation service validates the active incident and checks current health.
- Cooldown and retry limits restrict repeated remediation attempts.
- A restricted helper controls the restart operation.
- Health checks and audit records support post-action verification and traceability.

These controls reduce operational risk but do not replace production security reviews, least-privilege configuration, monitoring, and failure testing.

## Current Status

The current implementation includes FastAPI endpoints, Prometheus-based incident detection, rule-based diagnosis and decisions, SQLite incident persistence, approval-gated remediation, retry guardrails, and remediation auditing.

### Validation Results

- **Automated tests:** 25 tests passing with `python -m pytest -q`.
- **Incident detection:** Verified a controlled `user-service` outage was detected as a critical incident.
- **Recovery detection:** Confirmed the service returned to a healthy state and the incidents endpoint reported zero active incidents.
- **Approval safeguards:** An invalid approval token was rejected with HTTP 403.
- **Incident validation:** A request with an invalid incident ID was skipped without restarting a healthy service.
- **End-to-end remediation:** An approved request for a detected outage restarted `user-service` and returned `status: verified`, with `health_before: false` and `health_after: true`.
- **Audit persistence:** The successful remediation outcome was verified in both SQLite and the JSONL audit log.

These results validate the tested workflow in the current development environment. Broader failure-injection, concurrency, deployment, and production security testing remain necessary.

## Roadmap

- [ ] Expand automated unit and integration tests
- [ ] Add GitHub Actions CI for automated testing
- [ ] Improve incident correlation and diagnosis
- [ ] Add stronger remediation failure and recovery handling
- [ ] Develop cost-aware infrastructure recommendations
- [ ] Explore LLM-assisted diagnosis with explicit safety boundaries
- [ ] Add dashboards and operational metrics

## Author

**Janani V. — Janviswa**

GitHub: https://github.com/Janviswa

## License

No license has been selected yet. Until one is added, the repository should not be treated as granting general permission to reuse, modify, or redistribute the code.# kybernaut
Agentic AIOps for Infrastructure Reliability and Cost Optimization
