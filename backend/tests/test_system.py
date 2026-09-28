import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

_TEST_DIR = tempfile.TemporaryDirectory(prefix="aftermath-tests-", dir=os.path.dirname(__file__))
os.environ["AFTERMATH_DATABASE_PATH"] = os.path.join(_TEST_DIR.name, "test.sqlite3")
os.environ["AFTERMATH_MEMORY_MODE"] = "local"
os.environ["AFTERMATH_ENVIRONMENT"] = "test"
os.environ["GEMINI_API_KEY"] = ""

from fastapi.testclient import TestClient
from fastapi import HTTPException
from starlette.requests import Request

from app.auth import get_current_tenant
from app.main import app
from app.schemas.incident import IncidentCreate, IncidentRecord, IncidentResolutionUpdate
from app.services.hindsight_service import HindsightService
from app.services.incident_service import IncidentService
from app.services.llm_service import LLMService
from app.services.reasoning_service import ContextComparisonEngine
from app.services.storage_service import SQLiteStore


class BrokenMemory:
    def runtime_status(self):
        return {"mode": "MEMORY_UNAVAILABLE", "connected": False, "available": False}

    def retain(self, organization_id, incident):
        return {"saved": False, "mode": "MEMORY_UNAVAILABLE", "error": "test retention failure"}


class FakeHindsight:
    def __init__(self):
        self.calls = []
        self.records = []

    def get_version(self):
        return SimpleNamespace(version="test-hindsight")

    def retain(self, **kwargs):
        self.calls.append(("retain", kwargs))
        self.records = [{"text": kwargs["content"], "context": kwargs["context"], "metadata": kwargs["metadata"], "tags": kwargs["tags"], "document_id": kwargs["document_id"], "scores": SimpleNamespace(final=0.9)}]
        return SimpleNamespace(success=True)

    def recall(self, **kwargs):
        self.calls.append(("recall", kwargs))
        return SimpleNamespace(results=[SimpleNamespace(**item) for item in self.records])


class AftermathSystemTests(unittest.TestCase):
    def setUp(self):
        self.token = f"test-token-{self.id()}-" + ("x" * 40)
        self.organization = f"org-{self.id()}"
        os.environ["AFTERMATH_API_TOKENS"] = json.dumps({self.token: self.organization})
        self.client = TestClient(app)
        self.client.__enter__()
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def create(self, title="Checkout write latency", changes="", service="Checkout API", env="Production", symptoms="Write requests time out and the pending queue grows.", dependencies=None):
        return self.client.post("/api/incidents", headers=self.headers, json={
            "title": title, "service": service, "environment": env,
            "error_message": "CHECKOUT_WRITE_TIMEOUT",
            "symptoms": symptoms,
            "recent_changes": changes, "severity": "SEV-2", "impact": "Checkout requests delayed",
            "dependencies": dependencies or ["PostgreSQL"], "context": "Primary region",
        })

    def analyze(self, incident_id):
        return self.client.post(f"/api/incidents/{incident_id}/analyze", headers=self.headers)

    def test_incident_a_b_c_learning_loop_and_timeline(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.text)
        incident_a = response.json()
        self.assertEqual(self.analyze(incident_a["id"]).json()["recommendation_type"], "UNKNOWN")

        actions = [
            {"action": "Restart checkout workers", "result": "FAILED", "notes": "Queue saturation remained."},
            {"action": "Reduce concurrency temporarily", "result": "PARTIAL", "evidence": "Queue growth slowed."},
            {"action": "Set writer concurrency to 24", "result": "SUCCESS", "evidence": "Queue returned to baseline."},
        ]
        for action in actions:
            result = self.client.post(f"/api/incidents/{incident_a['id']}/actions", headers=self.headers, json=action)
            self.assertEqual(result.status_code, 201, result.text)
        resolution = {"successful_action": "Set writer concurrency to 24", "root_cause": "Worker concurrency exceeded the database connection budget.", "lessons_learned": ["Keep worker concurrency within the database connection budget."]}
        resolved = self.client.post(f"/api/incidents/{incident_a['id']}/resolve", headers=self.headers, json=resolution)
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertTrue(resolved.json()["learning_saved"])
        memories = self.client.get("/api/memories", headers=self.headers).json()
        retained = next(memory for memory in memories["memories"] if memory["id"] == incident_a["id"])
        self.assertEqual([action["result"] for action in retained["actions"]], ["FAILED", "PARTIAL", "SUCCESS"])
        self.assertIn("Worker concurrency exceeded", retained["metadata"]["root_cause"])

        incident_b = self.create(title="Checkout write latency recurrence").json()
        analysis_b = self.analyze(incident_b["id"]).json()
        self.assertEqual(analysis_b["recommendation_type"], "REUSE", json.dumps(analysis_b, indent=2))
        self.assertEqual(analysis_b["historical_matches"][0]["incident_id"], incident_a["id"])
        self.assertIsNone(analysis_b["historical_matches"][0]["similarity_score"])
        self.assertIn("Set writer concurrency to 24", analysis_b["recommendation"])
        self.assertTrue(any("Restart checkout workers" in action for action in analysis_b["actions_to_avoid"]))
        self.assertNotIn("Restart checkout workers", analysis_b["recommendation"])

        incident_c = self.create(title="Checkout write latency after dependency release", changes="ledger-writer v2.0 deployed 12 minutes ago",).json()
        analysis_c = self.analyze(incident_c["id"]).json()
        self.assertEqual(analysis_c["recommendation_type"], "CAUTION")
        self.assertTrue(any("ledger-writer v2.0" in difference for difference in analysis_c["key_differences"]))
        self.assertNotIn("Set writer concurrency to 24", analysis_c["recommendation"])

        timeline = self.client.get(f"/api/incidents/{incident_a['id']}/timeline", headers=self.headers).json()
        event_types = [event["event_type"] for event in timeline]
        self.assertEqual(event_types.count("ACTION_RECORDED"), 3)
        self.assertIn("MEMORY_RETRIEVAL", event_types)
        self.assertIn("INCIDENT_RESOLVED", event_types)
        self.assertIn("LEARNING_RETAINED", event_types)
        before = memories["total_memories"]
        repeated = self.client.post(f"/api/incidents/{incident_a['id']}/resolve", headers=self.headers, json=resolution)
        self.assertTrue(repeated.json()["learning_saved"])
        self.assertEqual(self.client.get("/api/memories", headers=self.headers).json()["total_memories"], before)

    def test_unrelated_memory_is_unknown_and_tenant_isolation(self):
        first = self.create().json()
        failed_action = {"action": "Restart checkout workers", "result": "FAILED"}
        self.client.post(f"/api/incidents/{first['id']}/actions", headers=self.headers, json=failed_action)
        self.client.post(f"/api/incidents/{first['id']}/resolve", headers=self.headers, json={"successful_action": "Set writer concurrency to 24", "root_cause": "Concurrency exhausted database connections."})
        unrelated = self.create(service="Search Indexer", title="Search shard delay").json()
        analysis = self.analyze(unrelated["id"]).json()
        self.assertEqual(analysis["recommendation_type"], "UNKNOWN")
        self.assertEqual(analysis["historical_evidence"], [])
        weak = self.create(service="Checkout API", title="Unrelated checkout UI failure", symptoms="The browser login screen becomes blank during navigation.", dependencies=["Auth service"]).json()
        self.assertEqual(self.analyze(weak["id"]).json()["recommendation_type"], "UNKNOWN")

        other = TestClient(app)
        with other:
            foreign_token = "another-org-token-" + ("y" * 40)
            os.environ["AFTERMATH_API_TOKENS"] = json.dumps({self.token: self.organization, foreign_token: "another-org"})
            foreign_headers = {"Authorization": f"Bearer {foreign_token}"}
            self.assertEqual(other.get(f"/api/incidents/{first['id']}", headers=foreign_headers).status_code, 404)
            self.assertEqual(other.get("/api/incidents", headers=foreign_headers).json(), [])

    def test_authentication_is_required(self):
        self.assertEqual(self.client.get("/api/incidents").status_code, 401)
        self.assertEqual(self.client.get("/api/runtime", headers={"Authorization": "Bearer invalid"}).status_code, 401)
        runtime = self.client.get("/api/runtime", headers=self.headers).json()
        self.assertEqual(runtime["workspace_mode"], "AUTHENTICATED")
        self.assertEqual(runtime["memory"]["mode"], "LOCAL_DEVELOPMENT")
        self.assertEqual(runtime["reasoning"]["mode"], "RULE_BASED_EVIDENCE")
        self.assertEqual(self.client.get("/health/live").status_code, 200)
        self.assertEqual(self.client.get("/health/ready").status_code, 200)

    def test_local_demo_auth_is_explicit_loopback_only_and_not_production(self):
        def request_from(host):
            return Request({"type": "http", "method": "GET", "path": "/api/runtime", "headers": [], "client": (host, 43210), "server": ("127.0.0.1", 8000), "scheme": "http", "query_string": b""})

        demo_environment = {
            "AFTERMATH_API_TOKENS": "",
            "AFTERMATH_ENVIRONMENT": "development",
            "AFTERMATH_LOCAL_DEMO": "true",
            "AFTERMATH_DEMO_ORGANIZATION_ID": "test-demo",
        }
        with patch.dict(os.environ, demo_environment):
            tenant = get_current_tenant(request=request_from("127.0.0.1"), credentials=None)
            self.assertEqual(tenant.organization_id, "test-demo")
            self.assertTrue(tenant.local_demo)
            with self.assertRaises(HTTPException):
                get_current_tenant(request=request_from("192.168.1.12"), credentials=None)
        with patch.dict(os.environ, {**demo_environment, "AFTERMATH_ENVIRONMENT": "production"}):
            with self.assertRaises(HTTPException):
                get_current_tenant(request=request_from("127.0.0.1"), credentials=None)

    def test_live_hindsight_adapter_uses_scoped_retain_and_recall(self):
        database = SQLiteStore(os.path.join(_TEST_DIR.name, f"hindsight-{self.id()}.sqlite3"))
        fake = FakeHindsight()
        memory = HindsightService(database=database, mode="hindsight", client=fake)
        incident = {
            "id": "INC-1", "title": "Checkout timeout", "service": "Checkout API", "environment": "Production",
            "error_message": "CHECKOUT_WRITE_TIMEOUT", "symptoms": "Writes time out", "recent_changes": "", "dependencies": ["PostgreSQL"],
            "actions": [], "attempts": [], "successful_action": "Set writer concurrency to 24", "root_cause": "Connection budget exceeded.",
            "lessons_learned": ["Set limits before deployment."],
        }
        self.assertTrue(memory.retain(self.organization, incident)["saved"])
        results = memory.recall("Checkout API CHECKOUT_WRITE_TIMEOUT", self.organization, incident)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["metadata"]["organization_id"], self.organization)
        self.assertEqual([name for name, _ in fake.calls], ["retain", "recall"])
        self.assertEqual(fake.calls[1][1]["tags_match"], "all_strict")
        self.assertEqual(memory.runtime_status()["mode"], "LIVE_MEMORY")

    def test_unavailable_memory_and_llm_degrade_without_claims(self):
        database = SQLiteStore(os.path.join(_TEST_DIR.name, f"unavailable-{self.id()}.sqlite3"))
        memory = HindsightService(database=database, mode="hindsight", client=FakeHindsight())
        memory.client = None
        memory.last_error = "Hindsight is unavailable."
        self.assertEqual(memory.runtime_status()["mode"], "MEMORY_UNAVAILABLE")
        result = memory.retain(self.organization, {"id": "INC-1", "service": "x", "environment": "prod", "error_message": "x", "symptoms": "x", "title": "x"})
        self.assertFalse(result["saved"])
        incident = IncidentRecord(id="INC-9", organization_id=self.organization, title="Unknown", service="Unknown service", environment="Production", error_message="UNKNOWN", symptoms="No evidence.")
        analysis = ContextComparisonEngine(memory=memory, llm=LLMService()).analyze_incident(incident)
        self.assertEqual(analysis.recommendation_type.value, "UNKNOWN")
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            llm = LLMService()
        self.assertFalse(llm.available)

    def test_retention_failure_is_not_reported_as_learning_success(self):
        database = SQLiteStore(os.path.join(_TEST_DIR.name, f"retain-failure-{self.id()}.sqlite3"))
        service = IncidentService(database=database, memory=BrokenMemory())
        incident = service.create_incident(IncidentCreate(title="Indexer timeout", service="Indexer", error_message="INDEX_TIMEOUT", symptoms="Index requests time out."), self.organization)
        resolved = service.resolve_incident(incident.id, IncidentResolutionUpdate(successful_action="Reduce shard batch size", root_cause="Shard queue saturated"), self.organization)
        self.assertEqual(resolved.status, "RESOLVED")
        self.assertFalse(resolved.learning_saved)
        self.assertIn("retention failure", resolved.learning_error)
        self.assertIn("LEARNING_RETENTION_FAILED", [event.event_type for event in database.list_events(self.organization, incident.id)])

    def test_incident_ids_are_safe_under_concurrent_creation(self):
        database = SQLiteStore(os.path.join(_TEST_DIR.name, f"concurrency-{self.id()}.sqlite3"))
        with ThreadPoolExecutor(max_workers=8) as pool:
            identifiers = list(pool.map(lambda _: database.next_incident_id(self.organization), range(32)))
        self.assertEqual(len(set(identifiers)), 32)
        self.assertEqual(set(identifiers), {f"INC-{value:06d}" for value in range(1, 33)})


if __name__ == "__main__":
    unittest.main()
