"""The shareable walkthrough must be unable to read credentials or call services."""

import json
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from spottoyt.auth import SetupError, TimedSession, Vault
from spottoyt.cli import app
from spottoyt.demo import (CATALOG, IDS, MUSIC_RUN, DemoClient, demo_environment,
                           require_demo_root, seed_demo, walkthrough)
from spottoyt.review import load_report


def test_full_demo_uses_real_pipeline_and_never_contacts_services_or_credentials(tmp_path, monkeypatch):
    network = Mock(side_effect=AssertionError('No network in a demo'))
    credentials = Mock(side_effect=AssertionError('No real credential access'))
    monkeypatch.setattr('requests.Session.request', network)
    monkeypatch.setattr('keyring.backends.Windows.WinVaultKeyring.get_password', credentials)
    root = tmp_path / 'private'
    with demo_environment(root):
        seed_demo(root)
        report = load_report('music', MUSIC_RUN)
        scores = {row['song']['title']: row['candidates'][0]['score'] for row in report['data']['results'] if row['candidates']}
        assert scores['Northern Lights'] == 88 and scores['Window Seat'] == 83
        result = walkthrough()
        assert result['synthetic_only'] and result['same_copy_reused'] and result['same_plan_replay_unchanged']
        assert result['final_likes'] == 3 and result['final_playlist_entries'] == 5
        assert result['policy']['approved'] == 2 and result['policy']['skipped'] == 1
        assert [len(phase['sequence']) for phase in result['phases']] == [3, 4, 5]
        assert result['phases'][-1]['sequence'] == [IDS[0], IDS[1], IDS[0], IDS[2], IDS[3]]
    network.assert_not_called()
    credentials.assert_not_called()


def test_demo_boundaries_block_vault_http_and_unknown_real_ids(tmp_path, monkeypatch):
    network = Mock()
    monkeypatch.setattr('requests.Session.request', network)
    with demo_environment(tmp_path / 'private'):
        seed_demo(tmp_path / 'private')
        with pytest.raises(SetupError, match='Credential access is disabled'):
            Vault()
        with pytest.raises(SetupError, match='External service requests are disabled'):
            TimedSession().get('https://music.youtube.com')
        with pytest.raises(SetupError, match='invented candidate IDs'):
            DemoClient(tmp_path / 'private').get_song('REALfake123')
    network.assert_not_called()


def test_demo_refuses_existing_data_and_restores_parent_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('SPOTTOYT_DATA_DIR', str(tmp_path / 'real'))
    monkeypatch.delenv('SPOTTOYT_DEMO', raising=False)
    import os
    with demo_environment(tmp_path / 'private'):
        seed_demo(tmp_path / 'private')
        with pytest.raises(SetupError, match='overwrite'):
            seed_demo(tmp_path / 'private')
    assert os.environ['SPOTTOYT_DATA_DIR'] == str(tmp_path / 'real')
    assert 'SPOTTOYT_DEMO' not in os.environ
    with pytest.raises(SetupError, match='isolated'):
        require_demo_root(tmp_path / 'real')


def test_demo_cli_produces_synthetic_json_and_discards_temporary_data(monkeypatch):
    import tempfile
    from pathlib import Path
    created = []
    original = tempfile.TemporaryDirectory
    def temporary(*args, **kwargs):
        result = original(*args, **kwargs)
        created.append(Path(result.name))
        return result
    monkeypatch.setattr(tempfile, 'TemporaryDirectory', temporary)
    monkeypatch.setattr('requests.Session.request', Mock(side_effect=AssertionError('No network')))
    result = CliRunner().invoke(app, ['demo', '--walkthrough'])
    assert result.exit_code == 0, result.output
    proof = json.loads(result.output)
    assert proof['synthetic_only'] and proof['same_plan_replay_unchanged']
    assert created and all(not path.exists() for path in created)


def test_demo_ui_refuses_unmarked_workspace_before_opening_real_reports(tmp_path, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('SPOTTOYT_DEMO', '1')
    monkeypatch.setenv('SPOTTOYT_DATA_DIR', str(tmp_path / 'real'))
    forbidden = Mock(side_effect=AssertionError('Do not inspect real reports under a demo badge'))
    monkeypatch.setattr('spottoyt.review.list_reports', forbidden)
    ui = Path(__file__).resolve().parents[1] / 'src' / 'spottoyt' / 'ui.py'
    page = AppTest.from_file(str(ui)).run()
    assert not page.exception
    assert any('isolated synthetic workspace' in item.value for item in page.error)
    forbidden.assert_not_called()


def test_demo_guard_also_blocks_preexisting_vault_instances(tmp_path):
    vault = object.__new__(Vault)
    vault.backend = Mock()
    with demo_environment(tmp_path / 'private'):
        with pytest.raises(SetupError, match='Credential access is disabled'):
            vault.get('youtube')
        with pytest.raises(SetupError, match='Credential access is disabled'):
            vault.put('youtube', {})
    vault.backend.get_password.assert_not_called()
    vault.backend.set_password.assert_not_called()


def test_demo_refuses_occupied_port_without_stopping_existing_listener():
    import socket
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = CliRunner().invoke(app, ['demo', '--port', str(port)])
        assert result.exit_code == 1 and 'port is unavailable' in result.output
        assert listener.getsockname()[1] == port


def test_demo_server_failure_is_reported_and_does_not_stop_another_process(monkeypatch):
    import socket
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    child = Mock()
    child.wait.return_value = 1
    child.poll.return_value = 1
    monkeypatch.setattr('subprocess.Popen', Mock(return_value=child))
    result = CliRunner().invoke(app, ['demo', '--port', str(port)])
    assert result.exit_code == 1 and 'server stopped unexpectedly' in result.output
    child.terminate.assert_not_called()
