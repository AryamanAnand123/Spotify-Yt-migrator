"""Invented fixtures and a persistent, local-only demonstration destination."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from spottoyt.auth import SetupError

MUSIC_RUN = '20260101T000000Z-aaaaaaaa'
PODCAST_RUN = '20260101T000000Z-bbbbbbbb'
IDS = ('abcdefghijk', 'zyxwvutsrqp', 'lmnopqrstuv', 'vwxyzabcdef')
CATALOG = dict(zip(IDS, ('Midnight Signals', 'Paper Planes', 'Northern Lights', 'Window Seat')))
DURATIONS = dict(zip(IDS, (180, 180, 184, 194)))


def mark_demo_root(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / 'synthetic-demo.json').write_text(json.dumps({'schema_version': 1, 'synthetic_only': True}), encoding='utf-8')


def require_demo_root(root):
    try:
        marker = json.loads((Path(root) / 'synthetic-demo.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise SetupError('Demo mode needs its isolated synthetic workspace. Start spottoyt demo.') from None
    if marker != {'schema_version': 1, 'synthetic_only': True}:
        raise SetupError('Invalid synthetic demo workspace marker.')


def seed_demo(root):
    """Create a fresh dataset, refusing an existing dataset or inherited real path."""
    from spottoyt.archive import Archive, private_root
    from spottoyt.matching import extract_songs, rank

    root = Path(root).resolve()
    if os.environ.get('SPOTTOYT_DEMO') != '1' or private_root() != root:
        raise SetupError('Seed only the explicitly selected isolated demo workspace.')
    if root.exists() and any(root.iterdir()):
        raise SetupError('Demo seeding refuses to overwrite an existing workspace.')
    mark_demo_root(root)
    archive = Archive.create('spotify_api')
    try:
        with archive.db:
            archive.db.execute('INSERT INTO metadata VALUES(?,?)', ('synthetic_only', 'true'))
        tracks = [{'type': 'track', 'uri': f'spotify:track:demo{index + 1}', 'name': name,
                   'artists': [{'name': 'Maya Vale'}], 'album': {'name': 'Open Roads'},
                   'duration_ms': 180000, 'explicit': False}
                  for index, name in enumerate(('Midnight Signals', 'Paper Planes (Live)', 'Uncharted', 'Northern Lights', 'Window Seat'))]
        archive.append('liked_songs', [{'track': tracks[index]} for index in (0, 2, 3, 4)])
        archive.append('playlists', [{'id': 'demoplaylist', 'name': 'Open Roads — synthetic demo'}])
        archive.append('playlist_items/demoplaylist', [{'item': tracks[index]} for index in (0, 1, 0, 3, 4)])
        archive.coverage('liked_songs', 'complete', 4)
        archive.coverage('playlist_items/demoplaylist', 'complete', 5)
        show = {'type': 'show', 'uri': 'spotify:show:demo', 'name': 'Field Notes', 'publisher': 'Example Audio'}
        episode = {'type': 'episode', 'uri': 'spotify:episode:demo', 'name': 'Episode 12: The long way home',
                   'duration_ms': 1200000, 'release_date': '2026-01-01', 'show': show}
        archive.append('saved_shows', [{'show': show}])
        archive.append('saved_episodes', [{'episode': episode}])
        archive.finish('completed_with_gaps')
        source_id = archive.path.name
    finally:
        archive.close()
    songs, _ = extract_songs(root / 'archives' / source_id)
    results = []
    for row in songs:
        index = int(row['source_key'].removeprefix('spotify:track:demo')) - 1
        target = {0: IDS[0], 1: IDS[1], 3: IDS[2], 4: IDS[3]}.get(index)
        candidates = [] if target is None else [{'video_id': target, 'title': CATALOG[target],
            'artists': ['Maya Vale'], 'album': '' if index in (3, 4) else 'Open Roads',
            'duration_seconds': DURATIONS[target], 'explicit': False, 'available': True}]
        results.append({**row, **rank(row['song'], candidates)})
    music = {'schema_version': 1, 'source_archive': source_id, 'state': 'completed_selected_sample',
             'total_unique_songs': 5, 'selected_songs': 5, 'unselected_songs': 0, 'results': results}
    podcast = {'kind': 'show', 'title': 'Field Notes', 'show_title': 'Field Notes', 'publisher': 'Example Audio',
               'duration_ms': None, 'release_date': None}
    podcasts = {'schema_version': 1, 'source_archive': source_id, 'state': 'completed_selected_items', 'total_unique_items': 2,
                'results': [
                    {'source_key': 'show:spotify:show:demo', 'podcast': podcast, 'status': 'needs_review',
                     'candidates': [{'kind': 'show', 'target_id': 'MPSPPLsynthetic123', 'title': 'Field Notes Archive',
                                    'publisher': 'Example Clips', 'score': 64, 'available': True,
                                    'reasons': ['publisher_missing_or_differs']}],
                     'occurrences': [{'collection': 'saved_shows', 'position': 0}]},
                    {'source_key': 'episode:spotify:episode:demo', 'podcast': {**podcast, 'kind': 'episode',
                        'title': 'Episode 12: The long way home', 'duration_ms': 1200000, 'release_date': '2026-01-01'},
                     'status': 'unmatched', 'candidates': [], 'occurrences': [{'collection': 'saved_episodes', 'position': 0}]}]}
    for directory, run, report in (('matching', MUSIC_RUN, music), ('podcast-matching', PODCAST_RUN, podcasts)):
        output = root / directory / run
        output.mkdir(parents=True)
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'music_run': MUSIC_RUN, 'podcast_run': PODCAST_RUN, 'source_archive': source_id}


@contextmanager
def demo_environment(root):
    previous = {key: os.environ.get(key) for key in ('SPOTTOYT_DEMO', 'SPOTTOYT_DATA_DIR')}
    os.environ.update(SPOTTOYT_DEMO='1', SPOTTOYT_DATA_DIR=str(Path(root).resolve()))
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def walkthrough():
    """Run the real review/plan/journal/verifier against only the invented provider."""
    from spottoyt.archive import private_root
    from spottoyt.importer import import_account
    from spottoyt.reconciliation import verify_transfer
    from spottoyt.review import ReviewStore, load_report
    from spottoyt.transfers import Destination, create_plan, execute, preflight

    require_demo_root(private_root())
    music, podcasts = load_report('music', MUSIC_RUN), load_report('podcasts', PODCAST_RUN)
    imported = import_account(Path(__file__).resolve().parents[2] / 'examples' / 'account-data')
    store = ReviewStore()
    phases = []
    try:
        policy = store.apply_policy(music)
        store.apply_policy(podcasts)
        last_plan = None
        for phase, source, target in (('Partial copy', None, None), ('Fill the middle gap', 'spotify:track:demo2', IDS[1]),
                                      ('Finish reviewed entries', 'spotify:track:demo5', IDS[3])):
            if source:
                store.save(music, source, 'approved', candidate_id=target, confirmed=True, note='Synthetic walkthrough decision; no real listening claimed.')
            folder = create_plan([music, podcasts], allow_partial_playlists=True)
            if preflight(folder.name)['state'] != 'ready':
                raise SetupError('Synthetic preflight failed.')
            execution = execute(folder.name, confirmed=True)
            if execution['state'] != 'verified':
                raise SetupError('Synthetic execution did not finish.')
            report_folder = verify_transfer(folder.name, [imported.name])
            report = json.loads((report_folder / 'report.json').read_text(encoding='utf-8'))
            if report['state'] != 'verified_plan_with_gaps':
                raise SetupError('Synthetic verification failed.')
            state = Destination().client.state()
            playlist_id, playlist = next(iter(state['playlists'].items()))
            phases.append({'phase': phase, 'playlist_id': playlist_id, 'sequence': playlist['video_ids'],
                           'verification': report['state'], 'verified_operations': report['counts']['verified']})
            last_plan = folder.name
        before = Destination().client.state()
        execute(last_plan, confirmed=True)
        if before != Destination().client.state():
            raise SetupError('Synthetic same-plan replay changed destination state.')
        if (len(before['playlists']) != 1 or phases[0]['sequence'] != [IDS[0], IDS[0], IDS[2]] or
                phases[1]['sequence'] != [IDS[0], IDS[1], IDS[0], IDS[2]] or
                phases[2]['sequence'] != [IDS[0], IDS[1], IDS[0], IDS[2], IDS[3]]):
            raise SetupError('Synthetic occurrence-order proof failed.')
        return {'schema_version': 1, 'synthetic_only': True, 'external_service_access': 'disabled',
                'credential_access': 'disabled', 'policy': policy, 'phases': phases,
                'same_copy_reused': len({phase['playlist_id'] for phase in phases}) == 1,
                'same_plan_replay_unchanged': True, 'final_likes': len(before['likes']),
                'final_playlist_entries': len(next(iter(before['playlists'].values()))['video_ids']),
                'account_download_fixture_files': len(report['account_downloads'][0]['files'])}
    finally:
        store.close()


def run_demo(port=8502, automated=False, echo=print):
    import socket
    import subprocess
    import tempfile
    from spottoyt.review import review_command

    command = review_command(port)  # Validate before creating any data.
    if not automated:
        with socket.socket() as listener:
            try:
                listener.bind(('127.0.0.1', port))
            except OSError:
                raise SetupError('The demo port is unavailable. Choose another --port; existing services were not stopped.') from None
    with tempfile.TemporaryDirectory(prefix='spottoyt-synthetic-demo-') as temporary:
        root = Path(temporary) / 'private'
        with demo_environment(root):
            seed_demo(root)
            if automated:
                echo(json.dumps(walkthrough(), indent=2))
                return
            echo(f'SYNTHETIC OFFLINE DEMO: http://127.0.0.1:{port}')
            echo('No accounts or credentials. Changes affect invented local data only. Ctrl+C stops and discards this demo.')
            server = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2], env=os.environ.copy())
            interrupted = False
            try:
                code = server.wait()
            except KeyboardInterrupt:
                interrupted = True
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
            if not interrupted and code != 0:
                raise SetupError('The synthetic demo server stopped unexpectedly. Check the local environment and selected port.')



class DemoClient:
    """No requests, tokens or real IDs: changes affect this temporary database only."""

    def __init__(self, root):
        self.root = Path(root)
        require_demo_root(self.root)
        self.path = self.root / 'demo-destination.sqlite3'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO state VALUES(1,?)', (json.dumps({'likes': [], 'playlists': {}, 'next_id': 1}),))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def state(self):
        with self.db() as db:
            return json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])

    def mutate(self, change):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
            result = change(state)
            db.execute('UPDATE state SET payload=? WHERE id=1', (json.dumps(state),))
            return result

    def known(self, identifier):
        if identifier not in CATALOG:
            raise SetupError('The synthetic demo accepts only its invented candidate IDs; real links are not contacted.')

    def get_account_info(self):
        return {'accountName': 'Spottoyt synthetic account', 'channelHandle': '@SpottoytDemo'}

    def get_song(self, identifier):
        self.known(identifier)
        return {'playabilityStatus': {'status': 'OK'}, 'videoDetails': {'videoId': identifier, 'title': CATALOG[identifier], 'lengthSeconds': str(DURATIONS[identifier])}}

    def get_watch_playlist(self, videoId, limit=1):
        self.known(videoId)
        return {'tracks': [{'videoId': videoId, 'likeStatus': 'LIKE' if videoId in self.state()['likes'] else 'INDIFFERENT'}]}

    def rate_song(self, identifier, rating):
        self.known(identifier)
        if rating != 'LIKE':
            raise SetupError('The demo supports additive likes only.')
        def change(state):
            if identifier not in state['likes']:
                state['likes'].append(identifier)
        self.mutate(change)
        return {}

    def get_library_playlists(self, limit=None):
        rows = [{'playlistId': key, 'title': value['title'], 'owned': True} for key, value in self.state()['playlists'].items()]
        return rows if limit is None else rows[:limit]

    def get_playlist(self, identifier, limit=None):
        row = self.state()['playlists'].get(identifier)
        if row is None:
            raise SetupError('Synthetic playlist not found.')
        return {**row, 'trackCount': len(row['video_ids']), 'tracks': [{'videoId': value} for value in row['video_ids']]}

    def create_playlist(self, title, description, privacy_status='PRIVATE'):
        if privacy_status != 'PRIVATE':
            raise SetupError('Demo copies must be private.')
        def change(state):
            identifier = f"PLdemo{state['next_id']:08d}"
            state['next_id'] += 1
            state['playlists'][identifier] = {'title': title, 'description': description, 'privacy': 'PRIVATE', 'video_ids': []}
            return identifier
        return self.mutate(change)

    def insert_playlist_item(self, playlistId, videoId, position):
        self.known(videoId)
        def change(state):
            items = state['playlists'][playlistId]['video_ids']
            if type(position) is not int or not 0 <= position <= len(items):
                raise SetupError('Invalid synthetic insertion position.')
            items.insert(position, videoId)
        self.mutate(change)
        return {'status': 'STATUS_SUCCEEDED'}

    def add_playlist_items(self, playlistId, videoIds, duplicates=False):
        for identifier in videoIds:
            self.known(identifier)
        def change(state):
            state['playlists'][playlistId]['video_ids'].extend(videoIds)
        self.mutate(change)
        return {'status': 'STATUS_SUCCEEDED'}

    def get_podcast(self, *args, **kwargs):
        raise SetupError('Podcast library saves are unsupported; the demo simulates only likes and playlist copies.')

    get_episode = get_podcast
