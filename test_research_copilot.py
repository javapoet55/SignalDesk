import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import research_copilot as copilot


class ResearchTests(unittest.TestCase):
    def test_horizon_and_missing_results(self):
        job = {
            "inputs": {
                "strategy": "FMFI",
                "symbol": "2 buys",
                "holds": [1, 5],
                "fee_bps": 1,
                "dates": ["2026-11-27", "2026-11-30", "2026-12-04"],
                "signal_day": "2026-11-25",
            },
            "error": None,
            "result": {
                "engine": "2026.10.5",
                "outcomes": {"A": [2.0, "Pending exit"], "B": [-1.0, "Pending exit"]},
            },
        }
        hold, facts = copilot.evidence(job, "5")
        report = copilot.report_text(facts, [])
        self.assertEqual(hold, 5)
        self.assertIn("0 of 2 outcomes closed", report)
        self.assertNotIn("Mean net return", report)
        self.assertIn("not establish independent observations", report)
        self.assertIn("+0.50%", copilot.report_text(copilot.evidence(job, "1")[1], []))

    def test_model_and_saved_report(self):
        def response(req, timeout):
            payload = json.loads(req.data)
            self.assertFalse(payload["store"])
            self.assertEqual(
                set(json.loads(payload["input"])), {"mode", "question", "evidence"}
            )
            return io.BytesIO(
                json.dumps(
                    {
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "No closed trades [E1].",
                                    }
                                ],
                            }
                        ],
                    }
                ).encode()
            )

        with (
            patch.dict(os.environ, OPENAI_API_KEY="test-only"),
            patch("urllib.request.urlopen", response),
        ):
            model, answer = copilot.ask(
                {"E1": {"label": "Coverage", "statement": "No trades."}},
                "Explain results",
                "Why?",
            )
        with tempfile.TemporaryDirectory() as root:
            saved = copilot.history(
                root, "job", 1, ("Explain results", "Why?", model, answer)
            )
            self.assertIn(answer, copilot.report_text({}, saved))
            self.assertFalse(copilot.history(root, "job", 5))
