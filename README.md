![WhatsApp Image 2026-03-30 at 10 43 11 PM](https://github.com/user-attachments/assets/f44e7301-9b76-4f11-80aa-1de336a71be3)

# ReQon Discovery Engine

An autonomous, AI-powered web testing platform that crawls any web application, detects defects, runs performance tests, and produces a single Hygiene Score with evidence for every finding.

Built for the **Feuji Innovation Hackathon 2026**.

## What It Does

Give it a URL. It does the rest:

1. **Crawls** the application autonomously (handles SPAs, authentication, dynamic content)
2. **Classifies** every page into one of 12 types using a self-learning ML classifier
3. **Detects defects** across visual, accessibility, functional, and DOM layers
4. **Runs performance tests** (load, stress, soak) to find bottlenecks
5. **Scores** the application on a 0-100 Hygiene Score with an A-F grade
6. **Tracks history** across scans using a Neo4j knowledge graph (new, recurring, resolved, regression)

## Quick Start (Docker)

The fastest way to get everything running:

```bash
git clone https://github.com/monisha-max/reqon-discovery-engine.git
cd reqon-discovery-engine

# Create .env with your OpenAI key
echo "OPENAI_API_KEY=sk-your-key-here" > .env

# Start everything (Neo4j + Redis + App)
docker compose up --build
```

Open **http://localhost:8765** in your browser.

That's it. Neo4j, Redis, Playwright, and the app are all containerized.

## Quick Start (Manual)

If you prefer running without Docker:

### Prerequisites

- Python 3.11+
- Redis (running)
- Neo4j (running)

### Setup

```bash
git clone https://github.com/monisha-max/reqon-discovery-engine.git
cd reqon-discovery-engine

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browser
python -m playwright install chromium

# Start Redis
brew services start redis        # macOS
# sudo systemctl start redis     # Ubuntu

# Start Neo4j
neo4j console &                  # macOS (brew install neo4j)
# Or via Docker:
docker run --name reqon-neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/reqonpassword \
  -d neo4j:5.20-community

# Set Neo4j password (first time only)
neo4j-admin dbms set-initial-password reqonpassword

# Create .env
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY
```

### Run the Web UI

```bash
python run_server.py
```

Open **http://localhost:8765**

### Run via CLI

```bash
# Basic scan
python main.py https://example.com

# With options
python main.py https://example.com --max-pages 30 --max-depth 3

# With authentication
python main.py https://myapp.com \
  --auth-type form \
  --username admin \
  --password secret \
  --login-url https://myapp.com/login
```

## Architecture

Six layers, each with a clear responsibility:

```
Layer 1: Orchestration     LangGraph state machine with ReAct loop
Layer 2: Crawling          Crawl4AI (breadth) + Playwright (depth)
Layer 3: Performance       Locust load/stress/soak testing
Layer 4: Authentication    Form login, cookie replay, token injection
Layer 5: Defect Detection  Visual, DOM, accessibility, functional analysis
Layer 6: Intelligence      Neo4j knowledge graph + scoring + trends
```

### Data Flow

```
URL in
  |
  v
Plan (LLM) --> Auth (if needed) --> [Crawl --> Evaluate]* --> Finalize
                                         |                       |
                                    Classify pages          Performance tests
                                    (self-learning)         (load/stress/soak)
                                                                 |
                                                           Defect detection
                                                                 |
                                                           Knowledge graph
                                                           (Neo4j: score,
                                                            trend, history)
                                                                 |
                                                                 v
                                                         Hygiene Score (A-F)
```

## Project Structure

```
reqon-discovery-engine/
├── main.py                          CLI entry point
├── run_server.py                    Web server entry point
├── docker-compose.yml               Docker setup (Neo4j + Redis + App)
├── Dockerfile                       Application container
│
├── layer1_orchestrator/             Multi-Agent Orchestration
│   ├── orchestrator.py              LangGraph state machine (ReAct loop)
│   └── nodes/
│       ├── planner.py               LLM + rule-based crawl planner
│       ├── auth_handler.py          Auth node for orchestrator
│       ├── crawler_node.py          Iterative crawl batch node
│       ├── evaluator.py             Mid-crawl replanning
│       ├── perf_test_node.py        Performance test orchestration
│       └── defect_detect_node.py    Defect detection orchestration
│
├── layer2_crawler/                  Intelligent Adaptive Crawler
│   ├── crawler_agent.py             Dual-engine coordinator
│   ├── engines/
│   │   ├── crawl4ai_engine.py       Fast breadth-first engine
│   │   └── playwright_engine.py     Deep analysis (screenshots, a11y, perf)
│   ├── classifier/
│   │   ├── page_classifier.py       Unified classifier (XGBoost + LLM + rules)
│   │   ├── feature_extractor.py     64 DOM/URL/HTML features
│   │   ├── llm_labeler.py           GPT-4o-mini page labeler
│   │   └── xgboost_classifier.py    Self-learning local model
│   └── frontier/
│       └── url_frontier.py          Priority queue + information foraging
│
├── layer3_performance/              Performance Testing
│   ├── perf_orchestrator.py         5-stage pipeline
│   ├── discovery/                   Endpoint discovery + payload generation
│   ├── engines/                     Locust runner + script generation
│   ├── analyzers/                   Bottleneck detection + AI analysis
│   └── report/                      HTML report generation
│
├── layer4_auth/                     Authentication Handler
│   ├── auth_handler.py              Detection, routing, execution, monitoring
│   └── monitor_singleton.py         Session health monitor
│
├── layer5_defect_detection/         Defect Detection
│   ├── defect_orchestrator.py       Multi-analyzer pipeline
│   ├── analyzers/                   Layout, contrast, functional, DOM behavioral
│   ├── capture/                     Screenshot capture
│   ├── evidence/                    Annotated screenshots + reports
│   └── preprocessing/               Stabilization, normalization, masking
│
├── intelligence/                    Knowledge Graph + Scoring
│   ├── services/
│   │   ├── runtime.py               Entry points for scoring pipeline
│   │   ├── normalizer.py            Unifies crawl/perf/defect into Issue model
│   │   ├── ingestion.py             Graph persistence + lifecycle tracking
│   │   ├── scoring.py               Deterministic multi-dimension scoring
│   │   └── trends.py                Score comparison + trend detection
│   ├── repositories/
│   │   └── neo4j_store.py           Neo4j driver (graph, scores, audit, events)
│   └── models/
│       └── contracts.py             Pydantic models for all intelligence data
│
├── api/                             Web API
│   ├── server.py                    FastAPI routes + SSE streaming
│   └── scan_manager.py              Scan lifecycle + intelligence integration
│
├── ui/
│   └── index.html                   Single-page web dashboard
│
├── shared/                          Shared models and state
│   ├── models/                      Pydantic data models
│   └── state/
│       └── redis_state.py           Redis state manager
│
├── config/
│   └── settings.py                  Environment configuration
│
└── tests/                           Unit + integration tests
```

## Environment Variables

Create a `.env` file in the project root:

```env
# LLM (at least one required for smart planning and classification)
OPENAI_API_KEY=sk-your-key-here
LLM_PROVIDER=openai

# Neo4j (required for intelligence scoring and history)
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=reqonpassword
NEO4J_DATABASE=neo4j

# Redis (optional, fails gracefully)
REDIS_URL=redis://localhost:6379/0

# Crawl defaults
MAX_PAGES=100
MAX_DEPTH=5
```

Without an LLM key, the planner and classifier fall back to rule-based heuristics. Without Neo4j, scans still run but intelligence (scoring, trends, history) shows as degraded.

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Web UI |
| `/api/scan` | POST | Start a scan |
| `/api/scan/{id}/status` | GET | Poll scan progress |
| `/api/scan/{id}/result` | GET | Get full results |
| `/api/scan/{id}/stream` | GET | SSE live log stream |
| `/api/intelligence/application/history` | GET | Score history for an app |
| `/api/intelligence/page/history` | GET | Score history for a page |
| `/api/intelligence/audit` | GET | Audit log |
| `/api/intelligence/events` | GET | Scan events |

## Product Overview - Visualization

# ReQon Discovery Engine Product Walkthrough
AI-powered quality intelligence for modern web applications.

ReQon scans a web application, logs in when needed, crawls pages, runs performance and defect analysis, and then converts all of those findings into a persistent intelligence layer. Instead of only showing what is broken right now, it remembers what happened across scans and tells you whether quality is improving or declining.

## What ReQon Does

ReQon combines multiple capabilities into one end-to-end workflow:

- URL-based application discovery
- Authenticated crawling for protected applications
- Page classification and targeted analysis
- Visual, functional, and accessibility defect detection
- Performance bottleneck detection under load
- Persistent issue memory across scans
- Explainable scoring, grading, and trend analysis
- Ranked page risk and actionable recommendations

## Product Experience

### 1. Start with a target URL
The user begins by entering the application URL to analyze.

![WhatsApp Image 2026-03-30 at 10 43 11 PM](https://github.com/user-attachments/assets/3691899e-14e8-4c8a-9c9f-e309ffd096f8)

This keeps the workflow simple: one entry point, one scan trigger, one unified result.

### 2. Configure authentication when needed
If the target application is behind login, the user can provide credentials and choose the auth method.

![WhatsApp Image 2026-03-30 at 10 43 25 PM](https://github.com/user-attachments/assets/10b07fcc-7ea1-4bf8-8939-c998933bd71e)

This allows ReQon to test both public and protected application flows.

### 3. Crawl, analyze, and test
Once the scan begins, ReQon orchestrates the full analysis pipeline:

- discovers pages
- classifies page types
- captures DOM and runtime observations
- runs performance tests
- detects visual and functional defects
- collects accessibility findings and evidence

![d2303992-5314-4490-8d52-530c1e8a9f44](https://github.com/user-attachments/assets/f9ded941-2b8b-4ebf-bb94-f8f833204f07)


## The Core Innovation: Intelligence, Not Just Scanning

Traditional scanners tell you what they found in one run.

ReQon goes further. It converts findings into a persistent intelligence model that remembers issues across scans, tracks lifecycle changes, and produces explainable scores.

That means ReQon can tell the difference between:

- a brand new issue
- a recurring issue
- a resolved issue
- a regression

## Unified Scan Outcome

At the end of a scan, the platform produces a single application-level quality result.

![WhatsApp Image 2026-03-30 at 10 35 12 PM](https://github.com/user-attachments/assets/6044aa84-187e-4a1a-9b79-f0097224a8bb)


This result includes:

- overall hygiene score
- grade
- risk level
- trend direction
- page count
- defect count
- bottleneck count
- coverage
- lifecycle counts for new, recurring, resolved, and regressed issues

This is where the product changes from a reporting tool into a decision-making tool.

## Dimension-Based Quality Scoring

ReQon scores quality across independent dimensions so that strength in one area cannot hide weakness in another.

![WhatsApp Image 2026-03-30 at 10 36 38 PM](https://github.com/user-attachments/assets/d3f7b810-047e-4d92-89ee-26b1cb24d9a6)

Dimensions currently represented include:

- accessibility
- performance
- visual quality
- functional quality
- SEO
- security

This makes the score understandable and trustworthy.

## Intelligence Summary and Page Risk Ranking

The platform ranks pages by risk so teams know where to focus first.

![WhatsApp Image 2026-03-30 at 10 37 25 PM](https://github.com/user-attachments/assets/dc17f76c-1ba3-4689-8020-ddd715399104)

Pages with the lowest scores or highest issue concentration are surfaced first, along with priority badges such as:

- score below threshold
- high issue volume

A deeper page-by-page breakdown is also provided.

![WhatsApp Image 2026-03-30 at 10 37 39 PM](https://github.com/user-attachments/assets/d71ae792-793f-44d6-a44b-9e86b55b8c56)

This allows teams to move from “the app is unhealthy” to “these specific pages are causing the problem.”

## Trend and Historical Memory

Because findings are stored in a persistent graph-backed memory layer, ReQon can show how quality changes over time.

![WhatsApp Image 2026-03-30 at 10 36 50 PM](https://github.com/user-attachments/assets/2b1bbcc6-9c50-4a89-bb35-7cc3aabca546)


That same memory powers the history view.

![WhatsApp Image 2026-03-30 at 10 37 54 PM](https://github.com/user-attachments/assets/f25c830a-85e2-41cb-a950-c120a99372cc)

ReQon stores:

- application score history
- page score history
- issue lifecycle state
- audit entries
- scan events

This means every scan builds on the last one instead of starting from zero.

## Evidence-Backed Defect Analysis

Every intelligence decision is grounded in real evidence.

![WhatsApp Image 2026-03-30 at 10 38 28 PM](https://github.com/user-attachments/assets/bb9c7ca5-6bf6-4bfe-9451-3b84c2a118dc)


Findings include issues such as:

- network failures
- DOM structure problems
- console errors
- low contrast accessibility violations

The platform also shows severity distribution so teams can understand overall quality risk.

![WhatsApp Image 2026-03-30 at 10 37 05 PM](https://github.com/user-attachments/assets/cc4c1f5a-088f-4dd9-aba1-f9b260b9d39f)

## Performance Intelligence

ReQon does not stop at crawl data. It also runs performance tests and surfaces bottlenecks under load.

![WhatsApp Image 2026-03-30 at 10 38 42 PM](https://github.com/user-attachments/assets/7848bdc3-f951-4dce-b7eb-1533f46560a1)


The detailed performance report includes:

- endpoints tested
- total requests
- peak RPS
- max error rate
- p50, p90, p95, p99 latencies
- error rates by endpoint
- bottleneck hotspots

![WhatsApp Image 2026-03-30 at 10 39 53 PM](https://github.com/user-attachments/assets/f5c4693a-60ba-447a-9257-f9deb74c75a0)


This lets engineering teams move from “the app feels slow” to precise endpoint-level diagnosis.

## AI Analysis and Recommendations

ReQon also synthesizes crawl, defect, and performance outputs into a readable summary and prioritized recommendations.

![WhatsApp Image 2026-03-30 at 10 40 28 PM](https://github.com/user-attachments/assets/3313ab31-951c-45b6-93c2-084a881f61f9)


This gives users not only findings, but also direction on what to fix first.

## Full Reports

For deeper investigation, ReQon links to full reports for both defect and performance analysis.

![WhatsApp Image 2026-03-30 at 10 38 54 PM](https://github.com/user-attachments/assets/285580cb-126f-491d-a061-313476322dda)

Example defect detail report:

![WhatsApp Image 2026-03-30 at 10 39 11 PM](https://github.com/user-attachments/assets/d08c45a7-2cb8-4db1-9b5d-9a63f0f1d1c1)
![WhatsApp Image 2026-03-30 at 10 39 31 PM](https://github.com/user-attachments/assets/73c84e9d-1502-4abc-a9b9-c9b50c33c242)



This allows users to go from executive summary to concrete reproduction evidence.

## Architecture Summary

At a high level, the system works like this:

1. User submits a URL and optional authentication details
2. The orchestrator plans and coordinates the scan
3. The crawler discovers pages and classifies them
4. Defect and performance layers collect findings and evidence
5. Findings are normalized into structured issue records
6. The intelligence layer stores memory in Neo4j
7. ReQon computes:
   - scores
   - grades
   - risk
   - trends
   - recurring/resolved/regression status
8. The UI presents both live results and historical intelligence

## What We Built

From start to finish, we built:

- a working scan UI
- an authenticated discovery workflow
- performance and defect analysis layers
- a persistent intelligence layer with memory
- application and page scoring
- lifecycle-aware issue tracking
- trend history and score history
- AI-generated analysis and recommendations
- evidence-backed full reports

## Why It Matters

The biggest difference between ReQon and a normal scanning tool is memory.

A normal scanner answers:
- What is broken right now?

ReQon answers:
- What is broken now?
- Has it happened before?
- Did we fix it and break it again?
- Is the application getting healthier or worse?
- Which pages are creating the most risk?
- What should we fix first?

That is the shift from detection to intelligence.

## Final Summary

ReQon is an end-to-end web quality intelligence platform that discovers, analyzes, remembers, scores, and explains application quality across scans.

It does not just detect issues.
It builds a memory of product quality.


## Team

Built by:
- Deekshitha Karvan
- Harsha Dayini Akula
- Monisha Kollipara
- Pranav Yeturu

Feuji Innovation Hackathon 2026
