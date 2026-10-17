import time

from fastapi.testclient import TestClient

from backend.main import app


def test_health_reports_feature_flags():
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["features"]["crop"] is False
    assert body["features"]["audio"] is False
    assert body["features"]["webhook"] is False
    assert body["features"]["fixture"] is True


def test_unknown_job_is_json():
    with TestClient(app) as client:
        response = client.get("/api/jobs/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]
    assert body["hint"]


def test_unsupported_language_is_rejected():
    with TestClient(app) as client:
        response = client.post("/api/hits/missing/report?lang=fr")
    assert response.status_code == 400
    assert "hint" in response.json()


def test_investigation_flow():
    with TestClient(app) as client:
        started = client.post("/api/investigate", json={})
        assert started.status_code == 200
        job_id = started.json()["job_id"]
        body = {}
        for _ in range(50):
            body = client.get(f"/api/jobs/{job_id}").json()
            if body["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.05)
        assert body["status"] == "succeeded", body
        assert len(body["hits"]) >= 6
        segment_id = body["hits"][0]["segment_id"]
        hit = client.get(f"/api/hits/{segment_id}")
        assert hit.status_code == 200
        english = client.post(f"/api/hits/{segment_id}/report?lang=en")
        german = client.post(f"/api/hits/{segment_id}/report?lang=de")
        ticket = client.post(f"/api/hits/{segment_id}/ticket")
        assert english.status_code == 200
        assert segment_id in english.json()["text"]
        assert german.status_code == 200
        assert "Zur manuellen Prüfung; keine Rechtsberatung." in german.json()["text"]
        assert ticket.status_code == 200
        assert ticket.json()["assignee"] is None
        page = client.get("/")
        assert page.status_code == 200
        assert "Investigate" in page.text
