from backend.candidates import find_candidates, merge_candidates
from backend.models import Candidate


def _candidate(segment_id: str, camera_id: str, score: float) -> Candidate:
    return Candidate(
        segment_id=segment_id,
        video_id=segment_id,
        camera_id=camera_id,
        location="loc",
        start=0,
        end=1,
        description="person close to a vehicle",
        detections=[],
        search_score=score,
        clip_url="api/fixture/sample.mp4",
    )


def test_dedupe_keeps_the_higher_score():
    merged = merge_candidates(
        [_candidate("s1", "cam", 0.2), _candidate("s1", "cam", 0.8)]
    )
    assert len(merged) == 1
    assert merged[0].search_score == 0.8


def test_per_camera_cap():
    items = [_candidate(f"s{i}", "cam", float(i)) for i in range(15)]
    merged = merge_candidates(items, per_camera_limit=12, overall_limit=40)
    assert len(merged) == 12
    assert merged[0].search_score == 14


def test_overall_cap_orders_by_score():
    items = []
    for camera in range(5):
        for index in range(10):
            items.append(_candidate(f"{camera}-{index}", f"c{camera}", float(index)))
    merged = merge_candidates(items, per_camera_limit=12, overall_limit=40)
    assert len(merged) == 40
    assert merged[0].search_score >= merged[-1].search_score


def test_unknown_camera_is_skipped():
    steps: list[str] = []
    found = find_candidates(
        ["person close to a moving vehicle"],
        ["wh-cam-1", "sf-cam"],
        5,
        on_step=steps.append,
    )
    assert any("sf-cam" in step and "not indexed" in step for step in steps)
    assert all(item.camera_id != "sf-cam" for item in found)
    assert found
