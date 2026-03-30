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

## Recommended Demo URLs

| URL | Good for |
|-----|----------|
| `https://the-internet.herokuapp.com` | Diverse page types, auth page, broken images, console errors |
| `https://juice-shop.herokuapp.com` | SPA (Angular), real defects, auth flows, many endpoints |
| `https://books.toscrape.com` | Many pages, fast crawl, good classification demo |

## Team

Built by:
- Deekshitha Karvan
- Harsha Dayini Akula
- Monisha Kollipara
- Pranav Yeturu

Feuji Innovation Hackathon 2026
