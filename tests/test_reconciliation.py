"""Meaningful read-back regressions: detect drift without mutating account/journal."""

import hashlib
import json
from unittest.mock import Mock

import pytest

from test_transfers import reviewed, prepared, destination
from spottoyt.archive import private_root
from spottoyt.auth import SetupError
from spottoyt.importer import import_account
from spottoyt.reconciliation import download_inventory, verify_transfer
from spottoyt.transfers import execute, load_plan


def read(folder):
    return json.loads((folder / "report.json").read_text())


def test_readback_detects_like_and_playlist_drift_without_mutating(reviewed):
    folder, client = prepared(reviewed)
    execute(folder.name, confirmed=True, destination=destination(client))
    journal = private_root() / 'transfers' / 'journal.sqlite3'
    before = journal.read_bytes()
    writes = list(client.writes)
    good = read(verify_transfer(folder.name, destination=destination(client)))
    assert good['counts'] == {'verified': 3}
    assert good['state'] == 'verified_plan_with_gaps'
    assert good['complete_account_migration'] is False
    assert good['account_writes'] == 0
    client.likes.clear()
    next(iter(client.playlists.values()))['video_ids'].reverse()
    next(iter(client.playlists.values()))['privacy'] = 'PUBLIC'
    bad = read(verify_transfer(folder.name, destination=destination(client)))
    assert bad['counts'] == {'destination_differs': 3}
    assert client.writes == writes
    assert journal.read_bytes() == before


def test_wrong_account_blocks_before_item_reads(reviewed):
    folder, client = prepared(reviewed)
    client.handle = '@DifferentAccount'
    client.get_watch_playlist = Mock(side_effect=AssertionError('Do not inspect the wrong account'))
    result = read(verify_transfer(folder.name, destination=destination(client)))
    assert result['state'] == 'blocked_or_partial'
    assert result['unchecked_operations'] == 3
    client.get_watch_playlist.assert_not_called()
    assert not client.writes


def test_playlist_order_drift_is_detected_even_when_privacy_and_count_match(reviewed):
    folder, client = prepared(reviewed)
    execute(folder.name, confirmed=True, destination=destination(client))
    state = next(iter(client.playlists.values()))
    state['video_ids'][0], state['video_ids'][1] = state['video_ids'][1], state['video_ids'][0]
    result = read(verify_transfer(folder.name, destination=destination(client)))
    playlist = next(row for row in result['operations'] if row['action'] == 'playlist')
    assert playlist['private'] and playlist['provenance_matches']
    assert playlist['expected_entries'] == playlist['observed_entries']
    assert playlist['status'] == 'destination_differs' and not playlist['sequence_matches']


def test_saved_verification_screen_is_offline_and_rejects_modified_reports(reviewed, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from spottoyt.reconciliation import list_verifications
    from pathlib import Path
    folder, client = prepared(reviewed)
    output = verify_transfer(folder.name, destination=destination(client))
    reports, errors = list_verifications()
    assert len(reports) == 1 and errors == 0
    monkeypatch.setattr('spottoyt.auth.Vault', Mock(side_effect=AssertionError('No credential access')))
    monkeypatch.setattr('requests.Session.request', Mock(side_effect=AssertionError('No network access')))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'src' / 'spottoyt' / 'ui.py'), default_timeout=30).run()
    next(radio for radio in app.radio if radio.label == 'Workspace').set_value('Verification').run()
    assert not app.exception and not app.error
    assert any(metric.label == 'Verified operations' for metric in app.metric)
    (output / 'report.json').write_text('{}')
    assert list_verifications() == ([], 1)


def test_unexecuted_playlist_is_not_claimed_migrated(reviewed):
    folder, client = prepared(reviewed)
    result = read(verify_transfer(folder.name, destination=destination(client)))
    assert result['state'] == 'completed_with_differences'
    assert result['counts'] == {'destination_differs': 2, 'not_transferred': 1}
    assert not client.writes


def test_read_failure_is_partial_and_suppresses_provider_secrets(reviewed):
    folder, client = prepared(reviewed)
    client.get_watch_playlist = Mock(side_effect=RuntimeError('PRIVATE-SECRET-RESPONSE'))
    output = verify_transfer(folder.name, destination=destination(client))
    result = read(output)
    assert result['state'] == 'blocked_or_partial'
    assert result['unchecked_operations'] == 3
    assert 'PRIVATE-SECRET' not in (output / 'report.json').read_text()
    client.get_watch_playlist.assert_called_once()


def test_historical_verification_survives_later_decision_changes(reviewed):
    from spottoyt.review import ReviewStore
    folder, client = prepared(reviewed)
    execute(folder.name, confirmed=True, destination=destination(client))
    store = ReviewStore()
    try:
        store.save(reviewed, reviewed['data']['results'][0]['source_key'], 'deferred')
    finally:
        store.close()
    result = read(verify_transfer(folder.name, destination=destination(client)))
    assert result['counts'] == {'verified': 3}
    assert result['gaps']['review_summary'][0]['decisions'] == {'approved': 2}


def test_history_is_checksum_verified_and_never_inferred_as_likes(reviewed, tmp_path):
    path = tmp_path / 'Streaming_History_Audio_2026.json'
    raw = b'[{"spotify_track_uri":"spotify:track:synthetic","ms_played":1234}]'
    path.write_bytes(raw)
    imported = import_account(path)
    inventory = download_inventory(imported.name, {'spotify:track:synthetic'})
    assert inventory['collections'] == {'history': 1}
    assert inventory['files'][0]['sha256'] == hashlib.sha256(raw).hexdigest()
    assert inventory['history_summary']['current_library_overlap_events'] == 1
    assert inventory['history_summary']['unique_history_tracks_outside_current_library'] == 0
    assert download_inventory(imported.name)['history_summary']['unique_history_tracks_outside_current_library'] == 1
    folder, client = prepared(reviewed)
    output = read(verify_transfer(folder.name, [imported.name], destination(client)))
    assert output['account_downloads'][0]['collections'] == {'history': 1}
    assert not client.writes
    import sqlite3
    with sqlite3.connect(imported / 'archive.sqlite3') as db:
        db.execute("UPDATE original_files SET content=?", (b'edited',))
    with pytest.raises(SetupError, match='checksum'):
        download_inventory(imported.name)


def test_tampered_preflight_is_refused_before_account_access(reviewed):
    folder, client = prepared(reviewed)
    (folder / 'preflight.json').write_text('{}')
    client.get_account_info = Mock(side_effect=AssertionError('No account access expected'))
    with pytest.raises(SetupError, match='checksum-verified'):
        verify_transfer(folder.name, destination=destination(client))
    client.get_account_info.assert_not_called()


def test_plan_counts_unsearched_rows_even_when_all_were_selected(reviewed):
    from spottoyt.review import load_report
    from spottoyt.transfers import create_plan
    path = private_root() / 'matching' / reviewed['run_id'] / 'report.json'
    data = json.loads(path.read_text())
    data['results'].append({'source_key': 'not-searched', 'status': 'pending',
                            'song': {'title': 'Pending'}, 'candidates': [], 'occurrences': []})
    path.write_text(json.dumps(data))
    updated = load_report('music', reviewed['run_id'])
    from spottoyt.review import ReviewStore
    store = ReviewStore()
    try:
        for row in updated['data']['results'][:2]:
            store.save(updated, row['source_key'], 'approved', candidate_id=row['candidates'][0]['video_id'], confirmed=True)
    finally:
        store.close()
    plan = load_plan(create_plan([updated]).name)
    assert plan['unsearched_music'] == 1


def test_held_only_targets_are_not_checked_as_planned_writes(reviewed):
    from spottoyt.transfers import create_plan
    from spottoyt.review import ReviewStore, load_report
    path = private_root() / 'matching' / reviewed['run_id'] / 'report.json'
    data = json.loads(path.read_text())
    # Keep the first approved song only in an unresolved playlist, not in likes.
    data['results'][0]['occurrences'] = [ref for ref in data['results'][0]['occurrences'] if ref['collection'] != 'liked_songs']
    import sqlite3
    from spottoyt.matching import choose_archive
    source = choose_archive()
    with sqlite3.connect(source / 'archive.sqlite3') as db:
        db.execute("DELETE FROM records WHERE collection='liked_songs' AND position=0")
    path.write_text(json.dumps(data))
    updated = load_report('music', reviewed['run_id'])
    store = ReviewStore()
    try:
        row = updated['data']['results'][0]
        store.save(updated, row['source_key'], 'approved', candidate_id=row['candidates'][0]['video_id'], confirmed=True)
    finally:
        store.close()
    plan = load_plan(create_plan([updated]).name)
    assert len(plan['held_playlists']) == 1
    assert plan['operations'] == []
    assert plan['targets'] == []
    assert len(plan['approved_targets_without_operations']) == 1
