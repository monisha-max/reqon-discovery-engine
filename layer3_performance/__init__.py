"""
Layer 3 — AI-Powered Performance Testing.

Flow:
  1. EndpointDiscoverer  → discovers all endpoints (OpenAPI spec or crawled pages)
  2. PayloadGenerator    → AI generates realistic request payloads
  3. ScriptGenerator     → AI writes Locust HttpUser test scripts
  4. LoadEngine          → runs load / stress / soak tests headlessly
  5. ResultsAnalyzer     → aggregates metrics, detects bottlenecks, AI narrative
"""
