"""HTTP surface: nothing loads on its own, overrides do not leak, stream is valid."""
import json

import pytest
from conftest import CABINET
from fastapi.testclient import TestClient

from app.main import JOBS, app, find_sample

client = TestClient(app)

pytestmark = pytest.mark.skipif(CABINET is None, reason="cabinet STEP not present")


@pytest.fixture(scope="module")
def job():
    """Upload a known file rather than the sample -- which file counts as the
    sample depends on what is sitting in the project folder."""
    with open(CABINET, "rb") as fh:
        r = client.post("/api/upload",
                        files={"file": (CABINET.name, fh, "application/octet-stream")})
    assert r.status_code == 200
    return r.json()


def test_sample_is_offered_but_never_auto_loaded():
    info = client.get("/api/sample").json()
    assert info["available"] is True and info["name"].endswith(".step")
    # Asking about the sample must not create a job -- the user has to ask for it.
    before = set(JOBS)
    client.get("/api/sample")
    assert set(JOBS) == before


def test_sample_offers_the_most_recently_added_file():
    newest = find_sample()
    assert newest is not None
    assert client.get("/api/sample").json()["name"] == newest.name


def test_index_page_serves_without_loading_anything():
    body = client.get("/").text
    assert "No file loaded" in body
    assert "Choose STEP file" in body


def test_upload_round_trip(job):
    assert len(job["panels"]) == 21
    assert job["source"] == CABINET.name
    assert job["job_id"] in JOBS


def test_unknown_job_is_rejected():
    r = client.post("/api/layout", json={"job_id": "nope", "params": {}})
    assert r.status_code == 404


def test_layout_returns_sheets(job):
    r = client.post("/api/layout",
                    json={"job_id": job["job_id"], "params": {"effort": "fast"}})
    assert r.status_code == 200
    assert r.json()["stats"]["sheets"] >= 1


def test_overrides_do_not_leak_between_requests(job):
    body = {"job_id": job["job_id"], "params": {"effort": "fast"}}
    baseline = client.post("/api/layout", json=body).json()

    excluded = client.post("/api/layout", json={
        **body, "overrides": {"p0": {"included": False}, "p1": {"included": False}}}).json()
    assert len(excluded["bom"]) < len(baseline["bom"])

    again = client.post("/api/layout", json=body).json()
    assert len(again["bom"]) == len(baseline["bom"]), "state leaked into the next request"


def test_rename_override_reaches_the_layout(job):
    r = client.post("/api/layout", json={
        "job_id": job["job_id"], "params": {"effort": "fast"},
        "overrides": {"p5": {"label": "Top Drawer Back"}}}).json()
    labels = {p["label"] for s in r["sheets"] for p in s["placements"]}
    assert "Top Drawer Back" in labels


def test_grain_lock_prevents_all_rotation(job):
    overrides = {p["id"]: {"grain_locked": True} for p in job["panels"]}
    r = client.post("/api/layout", json={
        "job_id": job["job_id"], "params": {"effort": "fast"},
        "overrides": overrides}).json()
    assert not any(p["rotated"] for s in r["sheets"] for p in s["placements"])


def test_stream_is_valid_ndjson_ending_in_a_result(job):
    with client.stream("POST", "/api/layout/stream",
                       json={"job_id": job["job_id"],
                             "params": {"effort": "fast"}}) as r:
        assert r.status_code == 200
        assert "ndjson" in r.headers["content-type"]
        frames = [json.loads(line) for line in r.iter_lines() if line.strip()]

    assert frames[-1]["type"] == "result"
    assert all(f["type"] == "progress" for f in frames[:-1])
    # Every progress frame reports search telemetry; only some carry a drawing.
    for f in frames[:-1]:
        assert f["search"]["attempts"] > 0
        assert f["search"]["budget"] > 0
        if "sheets" in f:
            assert "stats" in f
    assert any("sheets" in f for f in frames[:-1]), "nothing was ever drawable"

    payload = frames[-1]["payload"]
    assert payload["stats"]["sheets"] >= 1
    assert len(payload["bom"]) > 0


def test_stream_reports_problems_as_a_frame_not_a_hang(job):
    """A bad parameter must arrive as a frame, not leave the client waiting."""
    with client.stream("POST", "/api/layout/stream",
                       json={"job_id": job["job_id"],
                             "params": {"effort": "fast", "sheet_width_mm": 0}}) as r:
        frames = [json.loads(line) for line in r.iter_lines() if line.strip()]
    assert frames[-1]["type"] in ("result", "error")
    if frames[-1]["type"] == "result":
        assert frames[-1]["payload"]["warnings"]
