"""Storage ceilings (RFC-0004 §4.6, A9).

Two limits, checked in this order at `begin`: the per-device quota
(`attach.quota_bytes`) and the temporary area, which is a quarter of it. The
distinction matters because the answers differ in `data.reason`, and because a
committed blob keeps counting against the quota after its temp file is gone.
"""

from __future__ import annotations

SHA_A = "a" * 64
SHA_B = "b" * 64


def _file_payload(size: int, sha: str) -> dict[str, object]:
    return {"kind": "file", "sha256": sha, "size": size, "mime": "application/pdf", "name": "a.pdf"}


def test_a_single_file_over_the_limit_is_413_at_begin(make_env):
    env = make_env(max_bytes=10_000, quota_bytes=10_000)
    response = env.begin(_file_payload(10_001, SHA_A))
    assert response.status == 413, response.json
    assert response.json["code"] == "PAYLOAD_TOO_LARGE"
    assert response.json["data"]["field"] == "size"
    assert response.json["data"]["limit"] == 10_000
    assert response.json["data"]["actual"] == 10_001
    assert env.service.store.open_uploads() == []  # no session, no reserved file


def test_the_temporary_area_hits_its_quarter_first(make_env):
    env = make_env(quota_bytes=10_000, max_bytes=10_000)  # tmp quota = 2 500

    reserved = env.begin(_file_payload(2_000, SHA_A))
    assert reserved.status == 201, reserved.json

    over = env.begin(_file_payload(3_000, SHA_B))
    assert over.status == 413, over.json
    assert over.json["data"]["reason"] == "tmp_quota"
    assert over.json["data"]["limit"] == 2_500
    assert over.json["data"]["used"] == 2_000


def test_the_device_quota_counts_committed_bytes(make_env):
    env = make_env(quota_bytes=10_000, max_bytes=10_000)
    first = env.upload(b"x" * 2_000, kind="file", mime="text/plain", name="a.txt")
    assert first.status == 201, first.json
    assert env.service.store.usage("dev-1", env.clock.now) == 2_000
    assert env.service.store.tmp_usage("dev-1") == 0  # the temp area is free again

    # 8 001 fits max_bytes and the temp area (a fresh session), but not the
    # device total: 2 000 already committed + 8 001 > 10 000.
    over = env.begin(_file_payload(8_001, SHA_B))
    assert over.status == 413, over.json
    assert over.json["data"]["reason"] == "quota"
    assert over.json["data"]["limit"] == 10_000
    assert over.json["data"]["used"] == 2_000
