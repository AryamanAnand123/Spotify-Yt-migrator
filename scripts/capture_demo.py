"""Capture a complete synthetic browser walkthrough; never opens user profiles."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

from spottoyt.demo import DemoClient, IDS, demo_environment, seed_demo
from spottoyt.review import review_command

PROJECT = Path(__file__).resolve().parents[1]


def choose(page, label, index, expected=None):
    page.wait_for_timeout(350)
    item = page.get_by_role('combobox', name=label, exact=True)
    item.scroll_into_view_if_needed()
    item.focus()
    item.press('ArrowDown')
    item.press('Home')
    for _ in range(index):
        item.press('ArrowDown')
    item.press('Enter')
    if expected:
        expect(item).to_have_value(expected)
    page.wait_for_timeout(400)


def navigate(page, name):
    page.get_by_test_id('stRadio').get_by_text(name, exact=True).click()
    page.wait_for_timeout(250)


def capture(page, path):
    page.get_by_test_id('stMain').evaluate('element => element.scrollTo(0, 0)')
    page.wait_for_timeout(250)
    page.screenshot(path=str(path), full_page=True)


def approve(page):
    choose(page, 'Candidate to compare', 1)
    choose(page, 'Review action', 1, 'Approve selected match')
    checkbox = page.get_by_role('checkbox', name='I checked this recording or show and want to approve this target', exact=True)
    checkbox.focus()
    checkbox.press('Space')
    expect(checkbox).to_be_checked()
    page.get_by_role('button', name='Save decision', exact=True).click()
    page.get_by_text('Decision saved locally. No account changes were made.', exact=True).wait_for()


def transfer(page, count):
    navigate(page, 'Transfer plan')
    page.get_by_role('button', name='Prepare plan from selected reports', exact=True).click()
    page.get_by_role('button', name='Check destination (read-only)', exact=True).click()
    page.get_by_text('Destination check: Ready', exact=True).wait_for()
    page.wait_for_timeout(400)
    checkbox = page.get_by_role('checkbox', name='I authorize the exact planned likes, saves and private playlist copies shown above', exact=True)
    checkbox.focus()
    checkbox.press('Space')
    expect(checkbox).to_be_checked()
    page.get_by_role('button', name='Simulate this transfer', exact=True).click()
    page.get_by_text(f'Transfer verified: {count} operations.', exact=True).wait_for()


def main():
    output = PROJECT / 'docs' / 'screenshots'
    output.mkdir(exist_ok=True, parents=True)
    proof = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'spottoyt.cli', 'demo', '--walkthrough'],
                           cwd=PROJECT, capture_output=True, encoding='utf-8', check=True)
    parsed = json.loads(proof.stdout)
    assert parsed['synthetic_only'] and parsed['same_copy_reused'] and parsed['same_plan_replay_unchanged']
    (PROJECT / 'docs' / 'demo-proof.json').write_text(json.dumps(parsed, indent=2) + '\n', encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='spottoyt-demo-browser-') as temporary:
        root = Path(temporary) / 'private'
        with demo_environment(root):
            seed_demo(root)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            url = f'http://127.0.0.1:{port}'
            with (Path(temporary) / 'server.log').open('w', encoding='utf-8') as log:
                server = subprocess.Popen(review_command(port), cwd=PROJECT, env=os.environ.copy(), stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        try:
                            with urlopen(url + '/_stcore/health', timeout=1) as response:
                                if response.status == 200:
                                    break
                        except OSError:
                            time.sleep(.2)
                    else:
                        raise RuntimeError('Synthetic demo server did not start')
                    external = []
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel='msedge', headless=True)
                        page = browser.new_page(viewport={'width': 1440, 'height': 1900})
                        def guard(route):
                            address = urlsplit(route.request.url)
                            if (address.scheme == 'http' and address.hostname == '127.0.0.1' and address.port == port) or address.scheme in ('blob', 'data'):
                                route.continue_()
                            else:
                                external.append(address.hostname)
                                route.abort()
                        page.route('**/*', guard)
                        page.goto(url)
                        page.get_by_text('Your library, ready for review', exact=True).wait_for()
                        navigate(page, 'Music')
                        choose(page, 'Choose an item', 2, 'Northern Lights')
                        choose(page, 'Candidate to compare', 1)
                        expect(page.get_by_role('button', name='Listen on YouTube Music', exact=True)).to_be_disabled()
                        assert page.locator('a[href*="music.youtube.com"]').count() == 0
                        capture(page, output / 'demo-review.png')
                        page.get_by_role('button', name='Approve scores 85+ and skip missing', exact=True).click()
                        page.get_by_text('Bulk review saved: 2 qualifying matches approved, 1 missing matches skipped. Other items still need review. Nothing transferred.', exact=True).wait_for()
                        transfer(page, 3)
                        page.get_by_text('Approved playlist entries: 3 · Unresolved entries left out: 2', exact=True).wait_for()
                        capture(page, output / 'demo-partial.png')
                        initial = DemoClient(root).state()
                        initial_id = next(iter(initial['playlists']))
                        assert initial['playlists'][initial_id]['video_ids'] == [IDS[0], IDS[0], IDS[2]]
                        navigate(page, 'Music')
                        choose(page, 'Match status', 2, 'Needs review')
                        choose(page, 'Your decision', 1, 'Unreviewed')
                        choose(page, 'Choose an item', 1, 'Paper Planes (Live)')
                        approve(page)
                        choose(page, 'Choose an item', 0, 'Window Seat')
                        approve(page)
                        transfer(page, 4)
                        navigate(page, 'Verification')
                        page.get_by_role('button', name='Read destination and create verification report', exact=True).click()
                        page.get_by_text('This report covers the frozen plan. It does not claim a complete account migration.', exact=True).wait_for()
                        capture(page, output / 'demo-completed.png')
                        final = DemoClient(root).state()
                        assert list(final['playlists']) == [initial_id]
                        assert final['playlists'][initial_id]['video_ids'] == [IDS[0], IDS[1], IDS[0], IDS[2], IDS[3]]
                        assert len(final['likes']) == 3
                        page.set_viewport_size({'width': 390, 'height': 844})
                        page.wait_for_timeout(300)
                        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
                        capture(page, output / 'demo-mobile.png')
                        assert not external, 'The demo page attempted an external request'
                        browser.close()
                    print('Synthetic browser proof passed: 85-point bulk review, private partial copy, manual approvals, same-copy completion, duplicate order, verification and mobile layout.')
                    print('External page requests: 0. Screenshots and proof contain invented data only.')
                finally:
                    if server.poll() is None:
                        server.terminate()
                        try:
                            server.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            server.kill()
                            server.wait()


if __name__ == '__main__':
    main()
