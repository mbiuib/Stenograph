"""Repository round-trip tests on a temporary database."""

from pathlib import Path

from stenograph.domain.models import Job, JobStatus, Segment
from stenograph.storage import JobRepository


def test_roundtrip(tmp_path: Path) -> None:
    """Save, load, update, list and delete a job."""
    repo = JobRepository(tmp_path / "test.db")
    job = Job(source_name="a.mp3", source_path=str(tmp_path / "a.mp3"))
    job.segments = [Segment(index=0, start=0.0, end=1.5, text="привет", speaker="SPEAKER_00")]
    job.text = "привет"
    job.status = JobStatus.DONE
    job.meta = {"duration": 1.5}
    repo.save(job)

    loaded = repo.get(job.id)
    assert loaded is not None
    assert loaded.text == "привет"
    assert loaded.segments[0].speaker == "SPEAKER_00"
    assert loaded.meta["duration"] == 1.5
    assert loaded.status == JobStatus.DONE

    loaded.progress = 100
    repo.save(loaded)
    again = repo.get(job.id)
    assert again is not None and again.progress == 100

    assert [j.id for j in repo.list()] == [job.id]
    assert repo.list(status=JobStatus.ERROR) == []

    repo.delete(job.id)
    assert repo.get(job.id) is None
    assert repo.list() == []
