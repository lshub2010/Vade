"""Collection passes: TTL, orphan files and abandoned uploads (RFC-0004 §4.6-§4.7).

The clock is injected, so these are deterministic instead of `sleep(3600)`.
Two different sweeps are covered: expiry (rows past their deadline) and
reconciliation (bytes and rows that disagree because a process died).
"""

from __future__ import annotations

import hashlib

from vade.blob.ids import blob_path, orphan_dir, tmp_path

from .images import png


def _payload(env, data: bytes, **extra: object) -> dict[str, object]:
    payload = {
        "kind": "image",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mime": "image/png",
        "name": "a.png",
        "width": 8,
        "height": 8,
    }
    payload.update(extra)
    return payload


def test_a_blob_past_its_ttl_is_swept_off_the_disk(env):
    data = png()
    done = env.upload(data)
    assert done.status == 201, done.json
    file_id = done.json["file_id"]
    path = blob_path(env.service.root, done.json["sha256"])
    assert path.exists()

    env.clock.advance(int(env.service.settings.blob_ttl_seconds) + 1)
    summary = env.service.sweep()
    assert summary["deleted"] >= 1
    assert summary["bytes"] >= len(data)
    assert not path.exists()
    assert env.service.store.get_blob(file_id) is None

    env.token = env.service.issue_token("dev-1")["token"]  # the 600 s token died too
    gone = env.client.get(f"/ui/blob/{file_id}", headers=env.headers())
    assert gone.status == 404, gone.json
    assert gone.json["code"] == "NOT_FOUND"


def test_reconcile_moves_a_file_without_a_row_into_orphan(env):
    sha = "cd" * 32
    stray = blob_path(env.service.root, sha)
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"bytes nobody claimed")

    kept = env.upload(png())
    assert kept.status == 201, kept.json

    orphans, missing = env.service.reconcile()
    assert (orphans, missing) == (1, 0)
    assert not stray.exists()
    moved = list(orphan_dir(env.service.root).iterdir())
    assert len(moved) == 1
    assert moved[0].name.startswith(sha)  # kept for a postmortem, then dropped
    assert moved[0].read_bytes() == b"bytes nobody claimed"

    # The registered blob is untouched by the pass.
    assert blob_path(env.service.root, kept.json["sha256"]).exists()
    assert env.service.store.get_blob(kept.json["file_id"]).state == "ok"


def test_an_idle_upload_expires_and_its_temp_file_is_collected(env):
    data = png()
    payload = _payload(env, data)
    begun = env.begin(payload)
    assert begun.status == 201, begun.json
    upload_id = begun.json["upload_id"]
    assert env.put_part(upload_id, 0, data).status == 200
    temp = tmp_path(env.service.root, upload_id)
    assert temp.exists()

    env.clock.advance(int(env.service.settings.idle_seconds) + 1)  # 15 min idle
    summary = env.service.sweep()
    assert summary["deleted"] >= 1
    assert not temp.exists()
    assert env.service.store.get_upload(upload_id) is None

    env.token = env.service.issue_token("dev-1")["token"]
    head = env.client.head(f"/ui/blob/upload/{upload_id}", headers=env.headers())
    assert head.status == 404, head.json
    assert head.json["data"]["field"] == "upload_id"

    # Same payload afterwards is a new session, never a phantom resume.
    again = env.begin(payload)
    assert again.status == 201, again.json
    assert again.json["upload_id"] != upload_id
