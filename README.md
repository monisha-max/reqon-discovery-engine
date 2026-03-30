# ReQon Discovery Engine

This project is the integrated ReQon discovery and intelligence application.

It includes:
- crawl orchestration
- authentication-aware scanning
- performance testing
- defect detection
- intelligence scoring and history
- a local web UI for running scans and reviewing results

## Quick Start

If you want the shortest path from clone to UI, use this section.

### 1. Open a terminal in the project root

Project root:

```powershell
C:\Users\Pranav Yeturu\Desktop\Feuji-main
```

### 2. Create a virtual environment

```powershell
py -3.13 -m venv .venv
```

### 3. Install Python dependencies

Without activating the venv:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 4. Install Playwright browser binaries

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

### 5. Start Neo4j

If you already have Neo4j running locally, make sure it is available on:

- `bolt://localhost:7687`
- username: `neo4j`
- password: `reqonpassword`

If you do not have Neo4j running, the easiest option is Docker:

```powershell
docker run --name reqon-neo4j `
  -p 7474:7474 `
  -p 7687:7687 `
  -e NEO4J_AUTH=neo4j/reqonpassword `
  -d neo4j:5.20-community
```

### 6. Start the app server

From the project root:

```powershell
cd .\api
..\.venv\Scripts\python.exe server.py
```

### 7. Open the UI

Open this in your browser:

```text
http://localhost:8765
```

Important:
- open `localhost`
- do not open `0.0.0.0`

## Alternative Server Start Commands

You can also start the server from the project root:

```powershell
.\.venv\Scripts\python.exe run_server.py
```

Or:

```powershell
.\.venv\Scripts\python.exe server.py
```

## Virtual Environment Activation

Activation is optional. You can always use `.\.venv\Scripts\python.exe ...` directly.

If you still want to activate the venv:

### PowerShell

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### Command Prompt

```cmd
.\.venv\Scripts\activate.bat
```

### Git Bash

```bash
source .venv/Scripts/activate
```

## Environment Variables

The app works with defaults, but these can be overridden in a `.env` file in the project root.

### Core settings

```env
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=reqonpassword
NEO4J_DATABASE=neo4j
REQON_DEFAULT_TENANT=default
```

### Optional LLM settings

```env
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
LLM_PROVIDER=openai
```

## What the UI Shows

The UI is available at:

```text
http://localhost:8765
```

The results screen shows:
- pages crawled
- coverage score
- performance results
- defect summaries
- intelligence score
- grade
- risk
- trend
- new, recurring, resolved, and regression counts
- page score table
- application history
- page history
- audit log
- recent scan events

## Recommended First Scan

Use this public URL for a simple smoke test:

```text
https://example.com
```

It is stable, public, and good for verifying that the UI and backend flow are connected.

## Useful Endpoints

UI:

```text
http://localhost:8765
```

Core API:

```text
http://localhost:8765/api/scan
```

History endpoints:

```text
http://localhost:8765/api/intelligence/application/history?application_key=...
http://localhost:8765/api/intelligence/page/history?page_url=...
http://localhost:8765/api/intelligence/audit
http://localhost:8765/api/intelligence/events
```

## Running Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Troubleshooting

### The browser says `ERR_ADDRESS_INVALID`

You probably opened:

```text
http://0.0.0.0:8765
```

Use:

```text
http://localhost:8765
```

### `Activate.ps1` is not found

That usually means either:
- the virtual environment was not created yet
- or you are in the wrong folder

Recreate it:

```powershell
py -3.13 -m venv .venv
```

### `playwright` or browser launch errors

Install Playwright browsers:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

### Neo4j connection/auth errors

Verify:
- Neo4j is running
- the Bolt port `7687` is open
- username is `neo4j`
- password matches `reqonpassword` unless overridden in `.env`

### The UI loads but intelligence is missing

Check:
- Neo4j is running
- the scan result JSON contains an `intelligence` block
- the history endpoints return data

## Repo Structure

Important directories:

- [api](/Users/Pranav Yeturu/Desktop/Feuji-main/api)
- [config](/Users/Pranav Yeturu/Desktop/Feuji-main/config)
- [intelligence](/Users/Pranav Yeturu/Desktop/Feuji-main/intelligence)
- [layer1_orchestrator](/Users/Pranav Yeturu/Desktop/Feuji-main/layer1_orchestrator)
- [layer2_crawler](/Users/Pranav Yeturu/Desktop/Feuji-main/layer2_crawler)
- [layer3_performance](/Users/Pranav Yeturu/Desktop/Feuji-main/layer3_performance)
- [layer4_auth](/Users/Pranav Yeturu/Desktop/Feuji-main/layer4_auth)
- [layer5_defect_detection](/Users/Pranav Yeturu/Desktop/Feuji-main/layer5_defect_detection)
- [ui](/Users/Pranav Yeturu/Desktop/Feuji-main/ui)
- [tests](/Users/Pranav Yeturu/Desktop/Feuji-main/tests)

## Current Status

This repo now has direct intelligence integration:
- scan output is normalized after orchestration
- issue lifecycle history is stored in Neo4j
- scoring is computed and returned to the UI
- history, audit, and event endpoints are exposed through FastAPI
