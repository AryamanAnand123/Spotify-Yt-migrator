"""Headless Edge QA against isolated synthetic data; never opens user profiles."""

import json
import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

from spottoyt.review import review_command

PROJECT = Path(__file__).resolve().parents[1]
MUSIC_RUN = "20260101T000000Z-aaaaaaaa"
PODCAST_RUN = "20260101T000000Z-bbbbbbbb"


def seed(root):
    from spottoyt.demo import mark_demo_root
    mark_demo_root(root)
    archive_id = "20260101T000000Z-cccccccc"
    song = {"title": "Midnight Signals", "artists": ["Maya Vale"], "album": "Open Roads", "duration_ms": 180000, "explicit": False}
    candidate = {"video_id": "abcdefghijk", "title": "Midnight Signals", "artists": ["Maya Vale"], "album": "Open Roads",
                 "duration_seconds": 180, "explicit": False, "available": True, "score": 100, "reasons": [], "strong_metadata_match": True}
    results = [
        {"source_key": "spotify:track:demo1", "song": song, "status": "strong_suggestion", "candidates": [candidate],
         "occurrences": [{"collection": "liked_songs", "position": 0}]},
        {"source_key": "spotify:track:demo2", "song": {**song, "title": "Paper Planes (Live)"}, "status": "needs_review",
         "candidates": [{**candidate, "video_id": "zyxwvutsrqp", "title": "Paper Planes", "score": 68,
                         "reasons": ["recording_version_differs", "duration_differs"]}],
         "occurrences": [{"collection": "playlist_items/demo", "position": 0}]},
        {"source_key": "spotify:track:demo3", "song": {**song, "title": "Uncharted"}, "status": "unmatched", "candidates": [],
         "occurrences": [{"collection": "liked_songs", "position": 1}]},
    ]
    results.append({"source_key": "spotify:track:demo4", "song": {**song, "title": "Northern Lights"},
                    "status": "strong_suggestion", "candidates": [{**candidate, "video_id": "lmnopqrstuv", "title": "Northern Lights"}],
                    "occurrences": [{"collection": "liked_songs", "position": 2}]})
    music = {"schema_version": 1, "source_archive": archive_id, "state": "completed_selected_sample", "total_unique_songs": 12, "results": results}
    show = {"kind": "show", "title": "Field Notes", "show_title": "Field Notes", "publisher": "Example Audio",
            "duration_ms": None, "release_date": None}
    podcasts = {"schema_version": 1, "source_archive": archive_id, "state": "completed_selected_items", "total_unique_items": 2,
                "results": [
                    {"source_key": "show:spotify:show:demo", "podcast": show, "status": "needs_review",
                     "candidates": [{"kind": "show", "target_id": "MPSPPLsynthetic123", "title": "Field Notes Archive",
                                     "show_title": "Field Notes Archive", "publisher": "Example Clips", "score": 64,
                                     "available": True, "reasons": ["publisher_missing_or_differs"]}],
                     "occurrences": [{"collection": "saved_shows", "position": 0}]},
                    {"source_key": "episode:spotify:episode:demo", "podcast": {**show, "kind": "episode", "title": "Episode 12: The long way home",
                                                                                            "duration_ms": 1200000, "release_date": "2026-01-01"},
                     "status": "unmatched", "candidates": [], "occurrences": [{"collection": "saved_episodes", "position": 0}]}]}
    for folder, run, data in (("matching", MUSIC_RUN, music), ("podcast-matching", PODCAST_RUN, podcasts)):
        output = root / folder / run
        output.mkdir(parents=True)
        (output / "report.json").write_text(json.dumps(data), encoding="utf-8")
    inventory = root / "archives" / archive_id
    inventory.mkdir(parents=True)
    (inventory / "inventory.json").write_text(json.dumps({"metadata": {"schema_version": 1, "source": "synthetic_demo", "state": "completed_with_gaps"},
        "coverage": [{"collection": "liked_songs", "captured": 12, "expected": 12, "status": "complete"},
                     {"collection": "history", "captured": 0, "expected": None, "status": "requires_account_download"}]}))


def choose(page, label, value):
    combo = page.get_by_role("combobox", name=label, exact=True)
    combo.scroll_into_view_if_needed()
    page.wait_for_timeout(200)
    combo.focus()
    try:
        combo.press("ArrowDown")
        combo.press("Home")
        combo.press("ArrowDown")
        combo.press("Enter")
        expect(combo).to_have_value(re.compile(re.escape(value)), timeout=6000)
    except Exception:
        page.screenshot(path=str(PROJECT / ".venv" / "qa-failure.png"), full_page=True)
        print("Synthetic QA options:", page.get_by_role("option").all_text_contents(), flush=True)
        print("Synthetic QA alerts:", page.get_by_test_id("stAlert").all_text_contents(), flush=True)
        raise


def navigate(page, name):
    page.get_by_test_id("stRadio").get_by_text(name, exact=True).click()
    page.wait_for_timeout(300)


def main():
    output = PROJECT / ".pytest_cache" / "ui-qa"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spottoyt-ui-qa-") as temporary:
        root = Path(temporary) / "private"
        seed(root)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {**os.environ, "SPOTTOYT_DATA_DIR": str(root), "SPOTTOYT_DEMO": "1"}
        log = (Path(temporary) / "server.log").open("w", encoding="utf-8")
        server = subprocess.Popen(review_command(port), cwd=PROJECT, env=env, stdout=log, stderr=log)
        try:
            url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    with urlopen(url + "/_stcore/health", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(.2)
            else:
                raise RuntimeError("Review server did not become ready")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1100})
                page = context.new_page()
                external = []
                def guard(route):
                    if route.request.url.startswith(url) or route.request.url.startswith(("data:", "blob:")):
                        route.continue_()
                    else:
                        external.append(route.request.url.split("?")[0])
                        route.abort()
                page.route("**/*", guard)
                page.goto(url)
                page.get_by_text("Your library, ready for review", exact=True).wait_for()
                navigate(page, "Music")
                page.get_by_text("Music review", exact=True).wait_for()
                choose(page, "Candidate to compare", "#1")
                expect(page.get_by_role("button", name="Listen on YouTube Music", exact=True)).to_be_disabled()
                choose(page, "Review action", "Approve selected match")
                page.get_by_role("button", name="Save decision", exact=True).click()
                page.get_by_text("Confirm that you checked the recording or show before approving it.", exact=True).wait_for()
                checkbox = page.get_by_role("checkbox", name="I checked this recording or show and want to approve this target", exact=True)
                checkbox.focus()
                checkbox.press("Space")
                expect(checkbox).to_be_checked()
                page.get_by_role("button", name="Save decision", exact=True).click()
                page.get_by_text("Decision saved locally. No account changes were made.", exact=True).wait_for()
                expect(page.get_by_role("checkbox", name="I checked this recording or show and want to approve this target")).not_to_be_checked()
                page.set_viewport_size({"width": 1440, "height": 1700})
                page.get_by_test_id("stMain").evaluate("element => element.scrollTo(0, 0)")
                page.screenshot(path=str(output / "review-desktop.png"), full_page=True)
                navigate(page, "Approved preview")
                page.get_by_text("Approved items", exact=True).wait_for()
                page.reload()
                page.get_by_text("Your library, ready for review", exact=True).wait_for()
                navigate(page, "Approved preview")
                page.get_by_text("Approved items", exact=True).wait_for()
                navigate(page, "Music")
                page.get_by_role("button", name="Approve scores 85+ and skip missing", exact=True).click()
                page.get_by_text("Bulk review saved: 1 qualifying matches approved, 1 missing matches skipped. Other items still need review. Nothing transferred.", exact=True).wait_for()
                expect(page.get_by_role("combobox", name="Match status", exact=True)).to_have_value("Needs review")
                expect(page.get_by_role("combobox", name="Choose an item", exact=True)).to_have_value("Paper Planes (Live)")
                navigate(page, "Approved preview")
                expect(page.get_by_test_id("stMetricValue")).to_have_text("2")
                navigate(page, "Music")
                page.get_by_role("textbox", name="Find an item", exact=True).fill("no-such-demo-item")
                page.get_by_role("textbox", name="Find an item", exact=True).press("Enter")
                page.get_by_text("No items match these filters. Clear the search or change a filter.", exact=True).wait_for()
                page.get_by_role("textbox", name="Find an item", exact=True).fill("")
                page.get_by_role("textbox", name="Find an item", exact=True).press("Enter")
                navigate(page, "Podcasts")
                page.get_by_text("Podcast review", exact=True).wait_for()
                navigate(page, "Transfer plan")
                page.get_by_text("No frozen transfer plan yet. Prepare one after approving matches.", exact=True).wait_for()
                navigate(page, "Verification")
                page.get_by_text("No saved verification report yet.", exact=True).wait_for()
                navigate(page, "Music")
                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_test_id("stMain").evaluate("element => element.scrollTo(0, 0)")
                page.wait_for_timeout(700)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
                page.screenshot(path=str(output / "review-mobile.png"), full_page=True)
                assert not external, f"Unexpected external requests: {external}"
                browser.close()
            print("Browser QA passed: individual and bulk approval, missing skip, uncertain review queue, confirmation reset, persistence, preview, filters, podcasts and responsive layout.")
            print("External requests: 0. Screenshots contain synthetic data only.")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            log.close()


if __name__ == "__main__":
    main()
