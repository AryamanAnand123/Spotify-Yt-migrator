"""Review decisions, stale evidence, private paths and UI behavior on synthetic data."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from spottoyt.auth import SetupError
from spottoyt.review import (ReviewStore, listen_url, list_reports, load_report,
                             manual_target, review_command)


@pytest.fixture
def review_data(monkeypatch, tmp_path):
    root = tmp_path / "private"
    monkeypatch.setenv("SPOTTOYT_DATA_DIR", str(root))
    run = "20260101T000000Z-aaaaaaaa"
    folder = root / "matching" / run
    folder.mkdir(parents=True)
    song = {"title": "Midnight Signals", "artists": ["Maya Vale"], "album": "Open Roads", "duration_ms": 180000, "explicit": False}
    candidate = {"video_id": "abcdefghijk", "title": "Midnight Signals", "artists": ["Maya Vale"], "album": "Open Roads",
                 "duration_seconds": 180, "explicit": False, "available": True, "score": 100, "reasons": [], "strong_metadata_match": True}
    result = {"source_key": "spotify:track:synthetic", "song": song, "status": "strong_suggestion", "candidates": [candidate],
              "occurrences": [{"collection": "liked_songs", "position": 0}]}
    report = {"schema_version": 1, "source_archive": "synthetic-archive", "state": "completed_selected_sample",
              "total_unique_songs": 2, "results": [result]}
    (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return root, folder, load_report("music", run)


def test_approval_requires_confirmation_and_does_not_edit_report(review_data):
    _, folder, report = review_data
    before = hashlib.sha256((folder / "report.json").read_bytes()).hexdigest()
    store = ReviewStore()
    for confirmation in (False, None, "false", 1):
        with pytest.raises(SetupError, match="Confirm"):
            store.save(report, "spotify:track:synthetic", "approved", candidate_id="abcdefghijk", confirmed=confirmation)
    store.save(report, "spotify:track:synthetic", "approved", candidate_id="abcdefghijk", confirmed=True)
    store.close()
    reopened = ReviewStore()
    assert reopened.decisions(report)["spotify:track:synthetic"]["status"] == "approved"
    assert len(reopened.preview([report])["approved_items"]) == 1
    assert hashlib.sha256((folder / "report.json").read_bytes()).hexdigest() == before
    reopened.close()


def test_skip_defer_clear_and_audit_history(review_data):
    report = review_data[2]
    store = ReviewStore()
    for action in ("skipped", "deferred"):
        store.save(report, "spotify:track:synthetic", action)
        assert not store.preview([report])["approved_items"]
    store.save(report, "spotify:track:synthetic", "clear")
    assert not store.decisions(report)
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
    store.close()


def test_stale_report_cannot_save_or_reuse_approval(review_data):
    _, folder, report = review_data
    store = ReviewStore()
    store.save(report, "spotify:track:synthetic", "approved", candidate_id="abcdefghijk", confirmed=True)
    current = copy.deepcopy(report["data"])
    current["results"][0]["song"]["title"] = "Changed Recording"
    (folder / "report.json").write_text(json.dumps(current))
    with pytest.raises(SetupError, match="changed"):
        store.save(report, "spotify:track:synthetic", "skipped")
    with pytest.raises(SetupError, match="changed"):
        store.preview([report])
    reloaded = load_report("music", report["run_id"])
    assert not store.decisions(reloaded)
    assert not store.preview([reloaded])["approved_items"]
    store.close()


@pytest.mark.parametrize("url", [
    "http://music.youtube.com/watch?v=abcdefghijk", "https://evil.example/watch?v=abcdefghijk",
    "https://music.youtube.com.evil.example/watch?v=abcdefghijk", "https://user:secret@music.youtube.com/watch?v=abcdefghijk",
    "https://music.youtube.com/watch?v=abcdefghijk&access_token=SECRET", "https://music.youtube.com/watch?v=abcdefghijk&v=zyxwvutsrqp",
    "https://music.youtube.com/watch?v=abcdefghijk#SECRET", "javascript:alert(1)",
])
def test_manual_links_reject_unsafe_origins_credentials_and_queries(url):
    with pytest.raises(SetupError) as failure:
        manual_target(url, "song")
    assert "SECRET" not in str(failure.value)
    assert "secret@" not in str(failure.value)


def test_manual_links_normalize_without_network(review_data):
    assert manual_target("https://youtu.be/abcdefghijk?si=tracking", "episode") == "abcdefghijk"
    assert manual_target("https://music.youtube.com/playlist?list=PLsynthetic123", "show") == "MPSPPLsynthetic123"
    report = review_data[2]
    store = ReviewStore()
    store.save(report, "spotify:track:synthetic", "approved", manual_url="https://music.youtube.com/watch?v=zyxwvutsrqp", confirmed=True)
    decision = store.decisions(report)["spotify:track:synthetic"]
    assert decision["origin"] == "manual_metadata_unverified"
    assert decision["target_id"] == "zyxwvutsrqp"
    store.close()


def test_candidate_must_belong_to_report_and_be_available(review_data):
    report = review_data[2]
    store = ReviewStore()
    with pytest.raises(SetupError, match="Select"):
        store.save(report, "spotify:track:synthetic", "approved", candidate_id="zyxwvutsrqp", confirmed=True)
    with pytest.raises(SetupError, match="1000"):
        store.save(report, "spotify:track:synthetic", "deferred", note="a" * 1001)
    store.close()


def test_report_paths_and_corrupted_reports(review_data):
    root, _, report = review_data
    with pytest.raises(SetupError):
        load_report("music", "../elsewhere")
    corrupt = root / "matching" / "20260102T000000Z-bbbbbbbb"
    corrupt.mkdir()
    (corrupt / "report.json").write_text("[]")
    reports, errors = list_reports("music")
    assert len(reports) == 1 and errors == 1


def test_launcher_is_loopback_and_protected():
    command = review_command()
    assert command[command.index("--server.address") + 1] == "127.0.0.1"
    assert command[command.index("--server.allowedHosts") + 1] == "127.0.0.1"
    assert command[command.index("--browser.gatherUsageStats") + 1] == "false"
    assert command[command.index("--server.enableXsrfProtection") + 1] == "true"
    assert command[command.index("--server.enableCORS") + 1] == "true"
    with pytest.raises(SetupError):
        review_command(80)


def element(elements, label):
    return next(item for item in elements if item.label == label)


def test_ui_can_review_synthetic_match_and_preview(review_data):
    ui = Path(__file__).resolve().parents[1] / "src" / "spottoyt" / "ui.py"
    app = AppTest.from_file(str(ui), default_timeout=30).run()
    assert not app.exception
    element(app.radio, "Workspace").set_value("Music").run()
    assert not app.exception
    element(app.selectbox, "Candidate to compare").set_value("0").run()
    element(app.selectbox, "Review action").set_value("Approve selected match")
    element(app.checkbox, "I checked this recording or show and want to approve this target").check()
    element(app.button, "Save decision").click().run()
    assert not app.exception
    assert any("Decision saved" in message.value for message in app.success)
    assert not element(app.checkbox, "I checked this recording or show and want to approve this target").value
    element(app.radio, "Workspace").set_value("Approved preview").run()
    assert not app.exception
    assert app.metric[0].value == "1"


def test_ui_empty_and_filtered_states(review_data):
    ui = Path(__file__).resolve().parents[1] / "src" / "spottoyt" / "ui.py"
    app = AppTest.from_file(str(ui), default_timeout=30).run()
    element(app.radio, "Workspace").set_value("Music").run()
    element(app.text_input, "Find an item").set_value("does-not-exist").run()
    assert not app.exception
    assert any("No items match" in message.value for message in app.info)
    element(app.radio, "Workspace").set_value("Podcasts").run()
    assert any("No matching report" in message.value for message in app.info)


def policy_report(review_data):
    _, folder, report = review_data
    base = report["data"]["results"][0]
    data = copy.deepcopy(report["data"])
    for index, status in enumerate(("needs_review", "unmatched", "pending", "not_cached"), 1):
        row = copy.deepcopy(base)
        row["source_key"] = f"spotify:track:synthetic{index}"
        row["status"] = status
        if status == "needs_review":
            row["candidates"][0].update(score=70, strong_metadata_match=False, reasons=["duration_differs"])
        else:
            row["candidates"] = []
        data["results"].append(row)
    (folder / "report.json").write_text(json.dumps(data))
    return load_report("music", report["run_id"])


def test_bulk_approves_strong_skips_missing_and_leaves_others(review_data):
    report = policy_report(review_data)
    store = ReviewStore()
    counts, _ = store.policy_plan(report)
    assert counts == {"approve": 1, "skip": 1, "review": 1, "pending": 2, "preserved": 0}
    outcome = store.apply_policy(report)
    assert outcome["approved"] == outcome["skipped"] == 1
    decisions = store.decisions(report)
    assert decisions["spotify:track:synthetic"]["origin"] == "bulk_score_85"
    assert decisions["spotify:track:synthetic2"]["status"] == "skipped"
    assert len(decisions) == 2
    assert len(store.preview([report])["approved_items"]) == 1
    repeated = store.apply_policy(report)
    assert repeated["approved"] == repeated["skipped"] == 0
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    store.close()


def test_bulk_preserves_explicit_existing_choices(review_data):
    report = policy_report(review_data)
    store = ReviewStore()
    store.save(report, "spotify:track:synthetic", "skipped", note="I prefer another version")
    before = copy.deepcopy(store.decisions(report)["spotify:track:synthetic"])
    store.apply_policy(report)
    assert store.decisions(report)["spotify:track:synthetic"] == before
    store.close()


def test_bulk_ignores_metadata_gate_but_rejects_stale_report(review_data):
    _, folder, report = review_data
    data = copy.deepcopy(report["data"])
    data["results"][0]["candidates"][0]["strong_metadata_match"] = False
    (folder / "report.json").write_text(json.dumps(data))
    store = ReviewStore()
    with pytest.raises(SetupError, match="changed"):
        store.apply_policy(report)
    current = load_report("music", report["run_id"])
    assert store.policy_plan(current)[0]["approve"] == 1
    assert store.apply_policy(current)["approved"] == 1
    store.close()


def test_bulk_failure_rolls_back_all_decisions(review_data):
    import sqlite3
    report = policy_report(review_data)
    store = ReviewStore()
    store.db.execute("CREATE TRIGGER fail_skip BEFORE INSERT ON events WHEN NEW.action='bulk_skipped' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.apply_policy(report)
    assert not store.decisions(report)
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    store.close()


def test_ui_bulk_button_leaves_uncertain_review_queue(review_data):
    report = policy_report(review_data)
    ui = Path(__file__).resolve().parents[1] / "src" / "spottoyt" / "ui.py"
    app = AppTest.from_file(str(ui), default_timeout=30).run()
    element(app.radio, "Workspace").set_value("Music").run()
    element(app.button, "Approve scores 85+ and skip missing").click().run()
    assert not app.exception and not app.error
    assert element(app.selectbox, "Match status").value == "needs_review"
    assert element(app.selectbox, "Your decision").value == "Unreviewed"
    element(app.radio, 'Workspace').set_value('Overview').run()
    assert next(metric for metric in app.metric if metric.label == 'Need a closer look').value == '1'
    store = ReviewStore()
    assert len(store.decisions(report)) == 2
    store.close()
