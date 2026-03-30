import asyncio
import os
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("CRAWL4_AI_BASE_DIRECTORY", str(REPO_ROOT))

import api.scan_manager as scan_manager
from api.scan_manager import _scans, create_scan, get_scan_result, get_scan_status, run_scan
from api.server import app
from config.settings import settings
from intelligence.services.runtime import application_history, audit_history, event_history, page_history


class IntelligenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.original_tenant = settings.REQON_DEFAULT_TENANT
        self.tenant_id = f"test-int-{uuid.uuid4().hex[:8]}"
        settings.REQON_DEFAULT_TENANT = self.tenant_id
        self.target_url = f"https://integration-{uuid.uuid4().hex[:8]}.example.com"
        self.page_url = f"{self.target_url}/home"
        self.original_run_orchestrator = scan_manager.run_orchestrator
        self._orchestrator_calls = 0

        async def fake_run_orchestrator(**kwargs):
            self._orchestrator_calls += 1
            return self._build_final_state(self._orchestrator_calls)

        scan_manager.run_orchestrator = fake_run_orchestrator
        _scans.clear()
        if not self._neo4j_available():
            self.skipTest("Neo4j is not available on localhost:7687")
        self._cleanup_tenant()

    def tearDown(self):
        scan_manager.run_orchestrator = self.original_run_orchestrator
        _scans.clear()
        self._cleanup_tenant()
        settings.REQON_DEFAULT_TENANT = self.original_tenant

    def _cleanup_tenant(self):
        driver = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD),
        )
        try:
            with driver.session(database=settings.NEO4J_DATABASE) as session:
                session.run(
                    """
                    MATCH (n {tenant_id: $tenant_id})
                    DETACH DELETE n
                    """,
                    tenant_id=self.tenant_id,
                ).consume()
        finally:
            driver.close()

    def _neo4j_available(self) -> bool:
        driver = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD),
        )
        try:
            driver.verify_connectivity()
            return True
        except Exception:
            return False
        finally:
            driver.close()

    def _build_final_state(self, sequence: int):
        include_accessibility = sequence != 2
        page = {
            "url": self.page_url,
            "title": "Integration Home",
            "page_type": "landing",
            "console_errors": ["Console exploded"],
            "failed_requests": [],
            "accessibility": {
                "violations": [
                    {
                        "rule_id": "color-contrast",
                        "description": "Low contrast on CTA",
                        "impact": "serious",
                        "target_selector": "#cta",
                    }
                ]
                if include_accessibility
                else []
            },
            "performance": {
                "lcp_ms": 2100,
                "fcp_ms": 900,
                "ttfb_ms": 120,
                "cls": 0.08,
            },
        }
        return {
            "request": {"target_url": self.target_url},
            "result": {"target_url": self.target_url, "coverage_score": 1.0},
            "pages": [page],
            "perf_result": None,
            "defect_result": None,
            "coverage_score": 1.0,
            "page_type_distribution": {"landing": 1},
            "iteration": sequence,
            "errors": [],
        }

    async def _run_integrated_scan(self):
        scan_id = create_scan(
            target_url=self.target_url,
            auth_config=None,
            max_pages=1,
            max_depth=1,
            perf_config=None,
            defect_config=None,
        )
        await run_scan(scan_id)
        return get_scan_status(scan_id), get_scan_result(scan_id)

    def test_scan_manager_embeds_intelligence_and_tracks_lifecycle(self):
        status1, result1 = asyncio.run(self._run_integrated_scan())
        status2, result2 = asyncio.run(self._run_integrated_scan())
        status3, result3 = asyncio.run(self._run_integrated_scan())

        self.assertEqual(status1["status"], "done")
        self.assertEqual(status2["status"], "done")
        self.assertEqual(status3["status"], "done")

        intelligence1 = result1["intelligence"]
        intelligence2 = result2["intelligence"]
        intelligence3 = result3["intelligence"]

        self.assertEqual(intelligence1["status"], "ok")
        self.assertEqual(intelligence1["lifecycle_summary"]["new_issues"], 2)
        self.assertEqual(intelligence1["lifecycle_summary"]["recurring_issues"], 0)

        self.assertEqual(intelligence2["status"], "ok")
        self.assertEqual(intelligence2["lifecycle_summary"]["new_issues"], 0)
        self.assertEqual(intelligence2["lifecycle_summary"]["recurring_issues"], 1)
        self.assertEqual(intelligence2["lifecycle_summary"]["resolved_issues"], 1)
        self.assertEqual(intelligence2["lifecycle_summary"]["regressions"], 0)

        self.assertEqual(intelligence3["status"], "ok")
        self.assertEqual(intelligence3["lifecycle_summary"]["new_issues"], 0)
        self.assertEqual(intelligence3["lifecycle_summary"]["recurring_issues"], 1)
        self.assertEqual(intelligence3["lifecycle_summary"]["resolved_issues"], 0)
        self.assertEqual(intelligence3["lifecycle_summary"]["regressions"], 1)

        app_history = application_history(result3["application_key"])
        page_history_resp = page_history(intelligence3["page_scores"][0]["url"])
        audit_resp = audit_history(limit=20)
        event_resp = event_history(limit=20)

        self.assertEqual(len(app_history["entries"]), 3)
        self.assertEqual(len(page_history_resp["entries"]), 3)
        self.assertGreaterEqual(len(audit_resp["entries"]), 6)
        self.assertGreaterEqual(len(event_resp["events"]), 12)

    def test_history_api_endpoints_return_integrated_data(self):
        _, result = asyncio.run(self._run_integrated_scan())
        app_key = result["application_key"]
        page_url = result["intelligence"]["page_scores"][0]["url"]

        with TestClient(app) as client:
            app_resp = client.get("/api/intelligence/application/history", params={"application_key": app_key})
            page_resp = client.get("/api/intelligence/page/history", params={"page_url": page_url})
            audit_resp = client.get("/api/intelligence/audit", params={"limit": 10})
            events_resp = client.get("/api/intelligence/events", params={"limit": 10})

        self.assertEqual(app_resp.status_code, 200)
        self.assertEqual(page_resp.status_code, 200)
        self.assertEqual(audit_resp.status_code, 200)
        self.assertEqual(events_resp.status_code, 200)
        self.assertEqual(len(app_resp.json()["entries"]), 1)
        self.assertEqual(len(page_resp.json()["entries"]), 1)
        self.assertGreaterEqual(len(audit_resp.json()["entries"]), 2)
        self.assertGreaterEqual(len(events_resp.json()["events"]), 4)


if __name__ == "__main__":
    unittest.main()
