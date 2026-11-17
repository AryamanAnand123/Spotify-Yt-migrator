"""Partial copies fill source holes across plans, preserving duplicate occurrences."""

import copy
import json

import pytest

from test_transfers import FakeYouTube, IDS, destination, reviewed
from spottoyt.auth import SetupError, TransferPaused
from spottoyt.review import ReviewStore, policy_action
from spottoyt.transfers import Ledger, create_plan, execute, load_plan, preflight


class PositionedYouTube(FakeYouTube):
    def __init__(self):
        super().__init__()
        self.insert_limit = None
        self.insert_count = 0
        self.insert_timeout = False

    def insert_playlist_item(self, playlist, video, position):
        if self.insert_limit is not None and self.insert_count >= self.insert_limit:
            raise TransferPaused('Synthetic batch limit; no request sent')
        self.playlists[playlist]['video_ids'].insert(position, video)
        self.writes.append(('insert', video, position))
        self.insert_count += 1
        if self.insert_timeout:
            self.insert_timeout = False
            raise TimeoutError('SECRET-RESPONSE')
        return {'status': 'STATUS_SUCCEEDED'}


def choose(report, key, action):
    store = ReviewStore()
    try:
        row = next(row for row in report['data']['results'] if row['source_key'] == key)
        store.save(report, key, action, candidate_id=row['candidates'][0]['video_id'] if action == 'approved' else None, confirmed=True)
    finally:
        store.close()


def run_plan(report, client):
    folder = create_plan([report], allow_partial_playlists=True)
    checked = preflight(folder.name, destination(client))
    assert checked['state'] == 'ready', checked.get('reason')
    return folder, execute(folder.name, confirmed=True, destination=destination(client))


def test_later_review_inserts_hole_in_original_order_without_duplicating_existing_entries(reviewed):
    choose(reviewed, 'spotify:track:1', 'clear')
    client = PositionedYouTube()
    folder, result = run_plan(reviewed, client)
    assert result['state'] == 'verified'
    playlist = next(iter(client.playlists.values()))
    assert playlist['video_ids'] == [IDS[0], IDS[0]]
    choose(reviewed, 'spotify:track:1', 'approved')
    second, result = run_plan(reviewed, client)
    assert result['state'] == 'verified'
    assert client.create_count == 1
    assert playlist['video_ids'] == [IDS[0], IDS[1], IDS[0]]
    assert [row for row in client.writes if row[0] == 'insert'] == [('insert', IDS[0], 0), ('insert', IDS[0], 1), ('insert', IDS[1], 1)]
    before = list(client.writes)
    execute(second.name, confirmed=True, destination=destination(client))
    assert client.writes == before


def test_uncertain_positioned_insert_reconciles_without_repeating_same_video_occurrence(reviewed):
    choose(reviewed, 'spotify:track:1', 'clear')
    client = PositionedYouTube()
    client.insert_timeout = True
    folder = create_plan([reviewed], allow_partial_playlists=True)
    assert preflight(folder.name, destination(client))['state'] == 'ready'
    with pytest.raises(SetupError, match='stopped safely'):
        execute(folder.name, confirmed=True, destination=destination(client))
    assert next(iter(client.playlists.values()))['video_ids'] == [IDS[0]]
    assert preflight(folder.name, destination(client))['state'] == 'ready'
    execute(folder.name, confirmed=True, destination=destination(client))
    assert next(iter(client.playlists.values()))['video_ids'] == [IDS[0], IDS[0]]
    assert client.insert_count == 2


def test_batch_pause_preserves_occurrences_and_same_plan_resumes_only_missing_entries(reviewed):
    choose(reviewed, 'spotify:track:1', 'clear')
    client = PositionedYouTube()
    client.insert_limit = 1
    folder, result = run_plan(reviewed, client)
    assert result['state'] == 'paused'
    assert next(iter(client.playlists.values()))['video_ids'] == [IDS[0]]
    client.insert_limit = None
    assert preflight(folder.name, destination(client))['state'] == 'ready'
    result = execute(folder.name, confirmed=True, destination=destination(client))
    assert result['state'] == 'verified'
    assert client.insert_count == 2


def test_external_changes_and_changed_review_of_transferred_occurrence_block_completion(reviewed):
    choose(reviewed, 'spotify:track:1', 'clear')
    client = PositionedYouTube()
    run_plan(reviewed, client)
    state = next(iter(client.playlists.values()))
    state['video_ids'].append('lmnopqrstuv')
    before = list(client.writes)
    choose(reviewed, 'spotify:track:1', 'approved')
    folder = create_plan([reviewed], allow_partial_playlists=True)
    assert preflight(folder.name, destination(client))['state'] == 'blocked'
    assert client.writes == before
    state['video_ids'].pop()
    choose(reviewed, 'spotify:track:0', 'skipped')
    folder = create_plan([reviewed], allow_partial_playlists=True)
    assert preflight(folder.name, destination(client))['state'] == 'blocked'
    assert client.writes == before


def test_complete_legacy_copy_is_upgraded_without_recreation_or_writes(reviewed):
    client = PositionedYouTube()
    legacy = create_plan([reviewed])
    assert preflight(legacy.name, destination(client))['state'] == 'ready'
    execute(legacy.name, confirmed=True, destination=destination(client))
    before = list(client.writes)
    _, result = run_plan(reviewed, client)
    assert result['state'] == 'verified'
    assert client.writes == before


def test_source_order_change_blocks_completion(reviewed):
    from spottoyt.playlist_completion import inspect_completion
    choose(reviewed, 'spotify:track:1', 'clear')
    client = PositionedYouTube()
    folder, _ = run_plan(reviewed, client)
    op = next(op for op in load_plan(folder.name)['operations'] if op['action'] == 'playlist')
    ledger = Ledger()
    try:
        account = destination(client).account()['account_hash']
        saved = ledger.playlist_progress(account, op['source_id'])
        state = destination(client).playlist_state(saved['destination_id'])
        op['source_signature'] = 'different-order'
        with pytest.raises(SetupError, match='Source playlist order'):
            inspect_completion(state, op, saved)
    finally:
        ledger.close()


def test_round_robin_batch_reaches_each_partial_copy_before_filling_one(reviewed):
    from spottoyt.archive import Archive, private_root
    from spottoyt.matching import choose_archive, extract_songs
    from spottoyt.review import load_report
    source = choose_archive()
    archive = Archive(source)
    source_items = archive.items('playlist_items/source1')
    archive.append('playlists', [{'id': 'source2', 'name': 'Second playlist'}])
    archive.append('playlist_items/source2', source_items)
    archive.coverage('playlist_items/source2', 'complete', 3)
    archive.finish('completed_with_gaps')
    archive.close()
    rows, _ = extract_songs(source)
    data = copy.deepcopy(reviewed['data'])
    for result, row in zip(data['results'], rows):
        result['occurrences'] = row['occurrences']
    path = private_root() / 'matching' / reviewed['run_id'] / 'report.json'
    path.write_text(json.dumps(data))
    report = load_report('music', reviewed['run_id'])
    choose(report, 'spotify:track:0', 'approved')
    client = PositionedYouTube()
    client.insert_limit = 2
    _, result = run_plan(report, client)
    assert result['state'] == 'paused'
    assert client.create_count == 2
    assert [row['video_ids'] for row in client.playlists.values()] == [[IDS[0]], [IDS[0]]]


def test_uncertain_partial_copy_creation_recovers_existing_marker_once(reviewed):
    client = PositionedYouTube()
    client.create_timeout = True
    folder = create_plan([reviewed], allow_partial_playlists=True)
    assert preflight(folder.name, destination(client))['state'] == 'ready'
    with pytest.raises(SetupError, match='stopped safely'):
        execute(folder.name, confirmed=True, destination=destination(client))
    assert client.create_count == 1
    assert preflight(folder.name, destination(client))['state'] == 'ready'
    execute(folder.name, confirmed=True, destination=destination(client))
    assert client.create_count == 1
    assert next(iter(client.playlists.values()))['video_ids'] == [IDS[0], IDS[1], IDS[0]]


def test_new_copy_visibility_retries_reads_without_repeating_creation(reviewed):
    from spottoyt.auth import PlaylistNotReady
    client = PositionedYouTube()
    original = client.get_playlist
    reads = [0]
    def eventually_visible(identifier, limit=None):
        reads[0] += 1
        if reads[0] <= 2:
            raise PlaylistNotReady('Not propagated yet')
        return original(identifier, limit)
    client.get_playlist = eventually_visible
    _, result = run_plan(reviewed, client)
    assert result['state'] == 'verified'
    assert client.create_count == 1


def test_insert_visibility_lag_reads_until_expected_sequence_without_repeating_write(reviewed):
    client = PositionedYouTube()
    original_insert, original_get = client.insert_playlist_item, client.get_playlist
    hidden = {'remaining': 0, 'before': []}
    def insert(playlist, video, position):
        hidden['before'] = list(client.playlists[playlist]['video_ids'])
        result = original_insert(playlist, video, position)
        hidden['remaining'] = 2
        return result
    def get(playlist, limit=None):
        result = original_get(playlist, limit)
        if hidden['remaining']:
            hidden['remaining'] -= 1
            result['tracks'] = [{'videoId': item} for item in hidden['before']]
            result['trackCount'] = len(hidden['before'])
        return result
    client.insert_playlist_item, client.get_playlist = insert, get
    _, result = run_plan(reviewed, client)
    assert result['state'] == 'verified'
    assert client.insert_count == 3
    assert next(iter(client.playlists.values()))['video_ids'] == [IDS[0], IDS[1], IDS[0]]


def test_known_pause_before_creation_can_resume_without_uncertain_creation_block(reviewed):
    client = PositionedYouTube()
    original = client.create_playlist
    client.create_playlist = lambda *args, **kwargs: (_ for _ in ()).throw(TransferPaused('No write sent'))
    folder, result = run_plan(reviewed, client)
    assert result['state'] == 'paused' and client.create_count == 0
    client.create_playlist = original
    assert preflight(folder.name, destination(client))['state'] == 'ready'
    assert execute(folder.name, confirmed=True, destination=destination(client))['state'] == 'verified'
    assert client.create_count == 1


def test_partial_verification_checks_progress_separately_from_finished_plan(reviewed):
    from spottoyt.reconciliation import verify_transfer
    choose(reviewed, 'spotify:track:1', 'clear')
    client = PositionedYouTube()
    client.insert_limit = 1
    folder, _ = run_plan(reviewed, client)
    report = json.loads((verify_transfer(folder.name, destination=destination(client)) / 'report.json').read_text())
    assert report['state'] == 'verified_progress_with_gaps'
    assert report['counts'] == {'verified': 1, 'partial_verified': 1}
    partial = next(row for row in report['operations'] if row['action'] == 'playlist')
    assert partial['checkpoint_matches'] and partial['remaining_entries'] == 1


def test_preflight_resume_reuses_only_same_bound_account_and_unexpired_read_prefix(reviewed):
    from unittest.mock import Mock
    client = PositionedYouTube()
    folder = create_plan([reviewed])
    dest = destination(client)
    original = dest.target
    count = 0
    def fail_second(target):
        nonlocal count
        count += 1
        if count == 2:
            raise TimeoutError('SECRET')
        return original(target)
    dest.target = fail_second
    checked = preflight(folder.name, dest)
    assert checked['state'] == 'blocked' and len(checked['targets']) == 1
    resumed = destination(client)
    resumed.target = Mock(wraps=resumed.target)
    assert preflight(folder.name, resumed, resume_reads=True)['state'] == 'ready'
    assert resumed.target.call_count == 1
    client.handle = '@DifferentAccount'
    changed = destination(client)
    changed.target = Mock(wraps=changed.target)
    assert preflight(folder.name, changed, resume_reads=True)['state'] == 'ready'
    assert changed.target.call_count == 2


def test_reviewed_id_policy_keeps_title_aliases_informational_but_rejects_wrong_video_ids(reviewed):
    client = PositionedYouTube()
    original = client.get_song
    def alias(identifier):
        data = original(identifier)
        data['videoDetails']['title'] = 'Different title alias'
        data['videoDetails']['lengthSeconds'] = '220'
        return data
    client.get_song = alias
    folder = create_plan([reviewed])
    checked = preflight(folder.name, destination(client))
    assert checked['state'] == 'ready'
    assert len(checked['targets'][0]['metadata_warnings']) == 2
    execute(folder.name, confirmed=True, destination=destination(client))
    def wrong(identifier):
        data = alias(identifier)
        data['videoDetails']['videoId'] = 'lmnopqrstuv'
        return data
    client.get_song = wrong
    assert preflight(folder.name, destination(client))['state'] == 'blocked'


def test_long_preflight_execution_window_starts_when_checks_finish(reviewed, monkeypatch):
    from spottoyt import transfers
    clock = [1000.0]
    monkeypatch.setattr(transfers.time, 'time', lambda: clock[0])
    client = PositionedYouTube()
    dest = destination(client)
    original = dest.target
    def slow_target(target):
        clock[0] += 1000
        return original(target)
    dest.target = slow_target
    folder = create_plan([reviewed])
    checked = preflight(folder.name, dest)
    assert checked['created_at_epoch'] == 1000
    assert checked['completed_at_epoch'] == 3000
    assert execute(folder.name, confirmed=True, destination=destination(client))['state'] == 'verified'


@pytest.mark.parametrize('score,eligible', [(84.99, False), (85, True), (95.1, True), (float('nan'), False)])
def test_threshold_alone_ignores_metadata_warnings_and_close_alternatives(score, eligible):
    result = {'song': {}, 'status': 'needs_review', 'candidates': [
        {'video_id': IDS[0], 'score': score, 'available': True, 'strong_metadata_match': False,
         'reasons': ['album_missing_or_differs', 'artist_missing_or_differs', 'duration_missing']},
        {'video_id': IDS[1], 'score': score}]}
    assert (policy_action(result)[0] == 'approved') is eligible
    result['candidates'][0]['available'] = False
    assert policy_action(result)[0] is None
