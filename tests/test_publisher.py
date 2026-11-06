from pathlib import Path
from app.quarantine import fingerprint

import pytest

from app.publisher import plan_retirement, finalize_torrent

H = "a" * 40


class StubJournal:
    def __init__(self, jobs):
        self.jobs = jobs

    def get(self, h, source):
        return self.jobs.get((h, source))


def fixture(tmp_path):
    root = tmp_path / "downloads"
    folder = root / "Series" / "Season 1"
    folder.mkdir(parents=True)
    source = folder / "Episode.mkv"
    output = folder / "Episode.mp4"
    source.write_bytes(b"original")
    output.write_bytes(b"verified")
    torrent = {"hash": H, "save_path": str(root)}
    entries = [{"name": "Series/Season 1/Episode.mkv", "priority": 1}]
    journal = StubJournal({(H, str(source)): {"source": str(source), "status": "validated", "action": "remux", "output": str(output), "metadata": {"original": list(fingerprint(source)), "verified": list(fingerprint(output))}}})
    return root, source, output, torrent, entries, journal


def test_plans_original_folder(tmp_path):
    root, source, output, torrent, entries, journal = fixture(tmp_path)
    plan = plan_retirement(torrent, entries, journal, root, root / "deleted")
    assert plan == [str(source)]
    assert source.exists() and output.exists()


def test_rejects_unfinished_episode_before_moving_anything(tmp_path):
    root, source, output, torrent, entries, journal = fixture(tmp_path)
    entries.append({"name": "Series/Season 1/Next.mkv", "priority": 1})
    with pytest.raises(ValueError, match="Incomplete media job"):
        plan_retirement(torrent, entries, journal, root, root / "deleted")
    assert source.exists() and output.exists()


def test_original_remains_for_compatible_mp4(tmp_path):
    root, source, output, torrent, entries, journal = fixture(tmp_path)
    entries = [{"name": "Series/Season 1/Episode.mp4", "priority": 1}]
    journal.jobs[(H, str(output))] = {"source": str(output), "status": "validated", "action": "copy", "output": str(output), "metadata": {"original": list(fingerprint(output)), "verified": list(fingerprint(output))}}
    plan = plan_retirement(torrent, entries, journal, root, root / "deleted")
    assert plan == [str(output)]


def test_rejects_other_folder_output(tmp_path):
    root, source, output, torrent, entries, journal = fixture(tmp_path)
    journal.jobs[(H, str(source))]["output"] = str(root / "unrelated.mp4")
    with pytest.raises(ValueError, match="Unexpected validated output"):
        plan_retirement(torrent, entries, journal, root, root / "deleted")


def test_retirement_disabled_by_default(tmp_path):
    root, source, output, torrent, entries, journal = fixture(tmp_path)
    with pytest.raises(PermissionError, match="disabled"):
        finalize_torrent(None, torrent, journal, root, root / "deleted")
    with pytest.raises(PermissionError, match="opt-in"):
        finalize_torrent(None, torrent, journal, root, root / "deleted", finalize_enabled=True)
    assert source.exists() and output.exists()


def test_summary_distinguishes_adoption_from_new_work():
    from app.publisher import completion_summary
    files = [dict(source=f"/downloads/Season 3/show.s03e0{n}.mp4", action="remux",
                  status="quarantined", metadata={"candidate": "legacy.mp4"}) for n in range(1, 4)]
    summary = completion_summary(files)
    assert "/downloads" not in summary and "show.s03e03.mp4" in summary
    assert "3 existing outputs reused" in summary and "3 originals archived for 7 days" in summary
