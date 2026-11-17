"""Prevent common credentials and account exports from entering Git candidates."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which('git') is None, reason='Git is needed to check repository exclusions')
def test_private_artifacts_are_ignored_without_hiding_known_synthetic_fixtures():
    root = Path(__file__).resolve().parents[1]
    if not (root / '.git').exists():
        pytest.skip('Git checkout metadata is needed to check repository exclusions')
    private = {
        '.env', 'client_secret_example.json', 'auth.json', 'headers_auth.json',
        'cookies.json', 'session.har', 'spotify-data.zip', 'debug.log',
        'private-key.pem', 'service.key', 'archives/example/snapshot.json',
        'transfers/example/plan.json', 'Streaming_History_Audio.json',
        'YourLibrary.json', 'Playlist1.json', 'Follow.json',
        'examples/account-data/client_secret_example.json',
        'examples/account-data/auth.json',
    }
    public = {
        '.env.example', 'README.md', 'docs/demo-proof.json',
        'examples/account-data/YourLibrary.json', 'examples/account-data/Playlist1.json',
        'examples/account-data/StreamingHistory0.json', 'examples/account-data/Follow.json',
    }
    result = subprocess.run(['git', 'check-ignore', '--no-index', '--stdin', '-z'], cwd=root,
                            input=b'\0'.join(path.encode() for path in sorted(private | public)) + b'\0',
                            capture_output=True, check=False)
    assert result.returncode in (0, 1), result.stderr
    ignored = {path.decode() for path in result.stdout.split(b'\0') if path}
    assert private <= ignored
    assert not public & ignored
