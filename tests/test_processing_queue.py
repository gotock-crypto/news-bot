from pathlib import Path

from newsbot.db import DB


def make_db(tmp_path: Path):
    return DB(tmp_path / "news.db")


def test_processing_job_is_unique_and_claimable(tmp_path):
    db = make_db(tmp_path)
    mid = db.insert_message(
        "test",
        100,
        "2026-09-02T12:00:00+00:00",
        "Concrete news event",
    )

    db.enqueue_processing(mid, "test")
    db.enqueue_processing(mid, "test")

    assert db.processing_job_counts() == {"pending": 1}

    job = db.claim_processing_job()
    assert job["message_id"] == mid
    assert job["status"] == "processing"
    assert job["attempts"] == 1


def test_processing_job_recovery(tmp_path):
    db = make_db(tmp_path)
    mid = db.insert_message(
        "test",
        101,
        "2026-09-02T12:00:00+00:00",
        "Concrete news event",
    )
    db.enqueue_processing(mid, "test")
    job = db.claim_processing_job()

    assert db.processing_job_counts() == {"processing": 1}
    assert db.recover_processing_jobs() == 1
    assert db.processing_job_counts() == {"pending": 1}

    recovered = db.claim_processing_job()
    db.complete_processing_job(recovered["id"])
    assert db.processing_job_counts() == {"done": 1}
