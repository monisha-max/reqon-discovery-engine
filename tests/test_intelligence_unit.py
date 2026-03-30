import unittest
from datetime import datetime, timezone

from intelligence.services.normalizer import build_discovery_bundle, normalize_discovery_bundle


class IntelligenceNormalizerTests(unittest.TestCase):
    def test_normalize_crawl_and_perf_bundle(self):
        final_state = {
            "pages": [
                {
                    "url": "https://example.com/",
                    "title": "Example",
                    "page_type": "landing",
                    "console_errors": ["Boom"],
                    "failed_requests": [{"method": "GET", "url": "https://example.com/api", "error": "500"}],
                    "accessibility": {
                        "violations": [
                            {
                                "rule_id": "color-contrast",
                                "description": "Low contrast",
                                "impact": "serious",
                                "target_selector": "#hero",
                            }
                        ]
                    },
                    "performance": {
                        "lcp_ms": 2400,
                        "fcp_ms": 1200,
                        "ttfb_ms": 180,
                        "cls": 0.12,
                    },
                }
            ],
            "perf_result": {
                "bottlenecks": ["[LOAD] GET /: error_rate=50.0% exceeds 5% threshold"],
                "report_path": "output/perf.html",
            },
            "defect_result": None,
            "coverage_score": 0.5,
            "page_type_distribution": {"landing": 1},
            "iteration": 1,
            "errors": [],
        }

        bundle = build_discovery_bundle(
            final_state=final_state,
            target_url="https://example.com",
            scan_id="scan-123",
            scanned_at=datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc),
        )
        scan = normalize_discovery_bundle(bundle)

        self.assertEqual(scan.application_name, "example.com")
        self.assertEqual(scan.application_key, "https://example.com")
        self.assertEqual(len(scan.pages), 1)
        page = scan.pages[0]
        self.assertIsNotNone(page.performance_snapshot)
        all_issues = [issue for element in page.elements for issue in element.issues]
        self.assertGreaterEqual(len(all_issues), 4)
        self.assertTrue(any(issue.dimension.value == "performance" for issue in all_issues))
        self.assertTrue(any(issue.dimension.value == "accessibility" for issue in all_issues))
        self.assertTrue(any(issue.source_type == "crawl" for issue in all_issues))

    def test_normalize_defect_bundle_without_existing_page_match(self):
        final_state = {
            "pages": [],
            "perf_result": None,
            "defect_result": {
                "report_path": "output/defect.html",
                "pages_analyzed": [
                    {
                        "url": "https://example.com/settings",
                        "snapshots": [
                            {
                                "phase": "post",
                                "screenshot_path": "settings.png",
                                "annotated_screenshot_path": "settings_annotated.png",
                                "findings": [
                                    {
                                        "category": "overflow",
                                        "severity": "high",
                                        "description": "Settings drawer overflows viewport",
                                        "element_selector": "#drawer",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            "coverage_score": 0.0,
            "page_type_distribution": {},
            "iteration": 0,
            "errors": [],
        }

        bundle = build_discovery_bundle(
            final_state=final_state,
            target_url="https://example.com",
            scan_id="scan-456",
        )
        scan = normalize_discovery_bundle(bundle)

        self.assertEqual(len(scan.pages), 1)
        page = scan.pages[0]
        self.assertEqual(page.url, "https://example.com/settings")
        issues = [issue for element in page.elements for issue in element.issues]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].dimension.value, "visual")
        self.assertEqual(issues[0].source_type, "defect")


if __name__ == "__main__":
    unittest.main()
