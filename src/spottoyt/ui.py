"""Local workspace; destination access requires explicit transfer controls."""

import json
import hashlib
import os
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st

from spottoyt.auth import SetupError
from spottoyt.review import ReviewStore, inventories, listen_url, list_reports, source_kind

ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#17664f"/><path d="M8 18v-3a8 8 0 0 1 16 0v3M8 17h3v8H8zm13 0h3v8h-3z" fill="none" stroke="white" stroke-width="2" stroke-linejoin="round"/></svg>'
STATUS_LABELS = {"strong_suggestion": "Strong suggestion", "needs_review": "Needs review", "unmatched": "No candidate",
                 "pending": "Not searched", "not_cached": "Not in cache", "approved": "Approved", "skipped": "Skipped", "deferred": "Deferred"}


def duration_label(value):
    if not isinstance(value, (int, float)) or value < 0:
        return "Not supplied"
    seconds = round(value)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def listening_control(label, identifier, kind):
    if os.environ.get('SPOTTOYT_DEMO') == '1':
        st.button(label, disabled=True, help='Listening is disabled for invented demo records.')
    else:
        st.link_button(label, listen_url(identifier, kind))


def run_label(report):
    try:
        value = datetime.strptime(report["run_id"].split("-")[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        date = value.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %H:%M IST")
    except ValueError:
        date = report["run_id"]
    return f"{date} · {len(report['data']['results'])} items · {report['run_id'][-8:]}"


def metadata_panel(title, data, podcast=False):
    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.text(data.get("title", "No candidate selected"))
        if podcast:
            values = {"Show": data.get("show_title"), "Publisher": data.get("publisher"),
                      "Release date": data.get("release_date"), "Duration": duration_label(data.get("duration_seconds")),
                      "Duration precision": {"seconds": "Exact", "rounded": "Rounded", "exact": "Exact"}.get(data.get("duration_precision"), "Not supplied")}
        else:
            values = {"Artists": ", ".join(data.get("artists", [])), "Album": data.get("album"),
                      "Duration": duration_label(data.get("duration_seconds")),
                      "Content": "Explicit" if data.get("explicit") is True else "Clean" if data.get("explicit") is False else None}
        for label, value in values.items():
            st.caption(label)
            st.text("Not supplied" if value is None or value == "" else str(value))


def overview(reports, store):
    st.header("Your library, ready for review")
    st.write("Compare the suggested matches, listen when needed, and keep the choices you trust.")
    results = [row for report in reports for row in report["data"]["results"]]
    approved = len(store.preview(reports)["approved_items"])
    columns = st.columns(3)
    columns[0].metric("Items in selected reports", len(results))
    backlog = 0
    for report in reports:
        decisions = store.decisions(report)
        backlog += sum(decisions.get(row['source_key'], {}).get('status') not in ('approved', 'skipped')
                       and row['status'] not in ('pending', 'not_cached') for row in report['data']['results'])
    columns[1].metric("Need a closer look", backlog)
    columns[2].metric("Approved by you", approved)
    st.info("A suggestion is not an approval. Saving a decision here does not transfer anything.")
    archives = inventories()
    if not archives:
        st.info("No archive inventory yet. Create a Spotify archive from the terminal to get started.")
    else:
        labels = [f"{entry['id']} · {entry['data']['metadata'].get('source', 'archive')}" for entry in archives]
        selection = st.selectbox("Archive inventory", range(len(archives)), format_func=lambda index: labels[index])
        inventory = archives[selection]["data"]
        st.caption("Snapshot status: " + str(inventory["metadata"].get("state", "unknown")))
        st.dataframe([{ "Collection": row["collection"], "Captured": row["captured"],
                        "Expected": row.get("expected"), "Coverage": row["status"]} for row in inventory.get("coverage", [])],
                     hide_index=True, width="stretch")
    st.subheader("What stays separate")
    st.write("History and playback progress stay in the archive. Matches do not recreate them. Album saves and artist follows are not represented by the song-only sample.")


def review_queue(report, store):
    kind = report["kind"]
    st.header("Music review" if kind == "music" else "Podcast review")
    data = report["data"]
    results = data["results"]
    decisions = store.decisions(report)
    selected = len(results)
    total = data.get("total_unique_songs", data.get("total_unique_items", selected))
    st.caption(f"{selected} of {total} unique items in this report. Choices are saved locally to this report.")
    if total > selected:
        st.info(f"{total - selected} items have not been searched. This report covers a sample.")
    if data.get("state") not in ("completed_selected_sample", "completed_selected_items"):
        st.warning("This run is partial. Some candidates or metadata may not have been searched.")
    if not results:
        st.info("This report has no items. Create a matching report from an archive with saved music or podcasts.")
        return
    plan, _ = store.policy_plan(report)
    with st.container(border=True):
        st.subheader("Handle the clear cases together")
        st.write(f"{plan['approve']} qualifying matches to approve · {plan['skip']} missing matches to skip · {plan['review']} items left to review")
        if kind == 'music':
            st.caption('Music qualifies at a top score of 85/100 or higher, regardless of album, artist or other metadata warnings and close alternatives. The score is not a probability. Existing choices are preserved.')
        st.caption(f"Applies to this entire selected report, independent of filters. {plan['preserved']} existing choices are kept; {plan['pending']} unsearched or uncached items stay pending.")
        if st.button("Approve scores 85+ and skip missing" if kind == 'music' else "Approve strong matches and skip missing", type="primary", key="bulk-" + report["fingerprint"],
                     disabled=not (plan["approve"] or plan["skip"])):
            try:
                changed = store.apply_policy(report)
                st.session_state["review_flash"] = f"Bulk review saved: {changed['approved']} qualifying matches approved, {changed['skipped']} missing matches skipped. Other items still need review. Nothing transferred."
                st.session_state[f"match-filter-{kind}"] = "needs_review"
                st.session_state[f"choice-filter-{kind}"] = "Unreviewed"
                st.rerun()
            except SetupError as error:
                st.error(str(error))
    first, second, third = st.columns([2, 1, 1])
    search = first.text_input("Find an item", placeholder="Title, artist, show or publisher", key=f"find-{kind}")
    match_filter = second.selectbox("Match status", ["All", "strong_suggestion", "needs_review", "unmatched", "pending", "not_cached"], format_func=lambda value: STATUS_LABELS.get(value, value), key=f"match-filter-{kind}")
    choice_filter = third.selectbox("Your decision", ["All", "Unreviewed", "approved", "skipped", "deferred"], format_func=lambda value: STATUS_LABELS.get(value, value), key=f"choice-filter-{kind}")
    filtered = []
    for result in results:
        metadata = result.get("song", result.get("podcast"))
        haystack = " ".join(str(metadata.get(key, "")) for key in ("title", "artists", "show_title", "publisher")).casefold()
        decision = decisions.get(result["source_key"], {}).get("status", "Unreviewed")
        if search.casefold() in haystack and (match_filter == "All" or result["status"] == match_filter) and (choice_filter == "All" or decision == choice_filter):
            filtered.append(result)
    if not filtered:
        st.info("No items match these filters. Clear the search or change a filter.")
        return
    options = {row["source_key"]: row for row in filtered}
    item_key = f"item-{kind}-{report['fingerprint']}"
    if st.session_state.get(item_key) not in options:
        st.session_state[item_key] = next(iter(options))
    key = st.selectbox("Choose an item", list(options),
                       format_func=lambda value: options[value].get("song", options[value].get("podcast"))["title"],
                       key=item_key)
    result = options[key]
    saved = decisions.get(key)
    st.caption(f"Match: {STATUS_LABELS.get(result['status'], result['status'])} · Your decision: {STATUS_LABELS[saved['status']] if saved else 'Unreviewed'} · {len(result.get('occurrences', []))} source occurrences")
    candidates = result["candidates"]
    token = report["fingerprint"] + key
    choices = ["none"] + [str(index) for index in range(len(candidates))] + ["manual"]
    def option_label(value):
        if value == "none":
            return "Choose a candidate"
        if value == "manual":
            return "Enter a link manually"
        candidate = candidates[int(value)]
        return f"#{int(value)+1} · {candidate.get('title', 'Untitled')} · score {candidate.get('score', '?')}"
    chosen = st.selectbox("Candidate to compare", choices, format_func=option_label, key="candidate-" + token)
    candidate = candidates[int(chosen)] if chosen not in ("none", "manual") else {}
    left, right = st.columns(2)
    source = dict(result.get("song", result.get("podcast")))
    source["duration_seconds"] = round(source["duration_ms"] / 1000, 2) if isinstance(source.get("duration_ms"), (int, float)) else None
    source["duration_precision"] = "exact" if source.get("duration_ms") is not None else "unknown"
    with left:
        metadata_panel("Spotify source", source, kind == "podcasts")
    with right:
        metadata_panel("YouTube Music candidate", candidate, kind == "podcasts")
    target_kind = source_kind(report, result)
    candidate_id = candidate.get("video_id", candidate.get("target_id"))
    if candidate:
        reasons = candidate.get("reasons", [])
        if reasons:
            st.warning("Compare these differences before deciding:")
            st.text(" · ".join(reason.replace("_", " ").capitalize() for reason in reasons))
        else:
            st.caption("The supplied metadata agrees. Listen to confirm the recording or show.")
        listening_control('Listen on YouTube Music', candidate_id, target_kind)
    if not candidates:
        st.info("No usable candidate was found in the attempted searches. You can supply a checked link, skip, or defer.")
    manual = ""
    if chosen == "manual":
        manual = st.text_input("YouTube Music link", placeholder="https://music.youtube.com/…", key="manual-" + token)
        st.caption("Manual links are saved as unverified metadata. Check the show or recording yourself first.")
    if saved:
        st.caption("Saved decision: " + STATUS_LABELS[saved["status"]] + (" · manually entered target" if saved.get("origin") == "manual_metadata_unverified" else ""))
        if saved.get("target_id"):
            listening_control('Open saved target', saved['target_id'], saved['target_kind'])
        if saved.get("note"):
            st.text(saved["note"])
    revision = st.session_state.get("revision-" + token, 0)
    form_token = token + chosen + hashlib.sha256(manual.encode()).hexdigest()[:12] + str(revision)
    with st.form("decision-" + form_token):
        action = st.selectbox("Review action", ["Choose an action", "Approve selected match", "Skip this item", "Defer for later", "Clear current decision"])
        note = st.text_area("Review note (optional)", max_chars=1000, help="Do not enter passwords, cookies or account credentials.")
        confirmed = st.checkbox("I checked this recording or show and want to approve this target", key="confirm-" + form_token)
        submitted = st.form_submit_button("Save decision", type="primary")
    if submitted:
        actions = {"Approve selected match": "approved", "Skip this item": "skipped", "Defer for later": "deferred", "Clear current decision": "clear"}
        if action not in actions:
            st.error("Choose a review action before saving.")
        else:
            try:
                store.save(report, key, actions[action], candidate_id=candidate_id if chosen != "manual" else None,
                           manual_url=manual if chosen == "manual" else None, note=note, confirmed=confirmed)
                st.session_state["revision-" + token] = revision + 1
                st.session_state["review_flash"] = "Decision saved locally. No account changes were made."
                st.rerun()
            except SetupError as error:
                st.error(str(error))
    if kind == "podcasts":
        with st.expander("Podcast transfer limits"):
            st.write("The official adapter does not support show-library or saved-episode state. Matching and archives remain available; playback position, played state and history have no transfer mapping.")


def preview(reports, store):
    st.header("Approved preview")
    st.write("Only choices you explicitly approved appear here. This screen does not transfer anything.")
    plan = store.preview(reports)
    items = plan["approved_items"]
    if not items:
        st.info("No approved items in the selected reports yet. Open Music or Podcasts, compare a candidate, and record your choice.")
        return
    st.metric("Approved items", len(items))
    st.dataframe([{"Source": item["source_title"], "Type": item["target_kind"], "Source occurrences": len(item["occurrences"]),
                   "Choice": item["origin"], "Target": item['target_id'] if os.environ.get('SPOTTOYT_DEMO') == '1' else listen_url(item["target_id"], item["target_kind"])} for item in items],
                 column_config={"Target": st.column_config.TextColumn('Synthetic target') if os.environ.get('SPOTTOYT_DEMO') == '1' else st.column_config.LinkColumn("Target", display_text="Listen")}, hide_index=True, width="stretch")
    if any(item["origin"] == "manual_metadata_unverified" for item in items):
        st.warning("Some targets were entered manually and still need metadata/availability checks before any future transfer.")
    archives = {item["source_archive"] for item in items}
    if len(archives) > 1:
        st.warning("These reports use different source snapshots. Reconcile them before planning a transfer.")
    st.caption("The download contains private library metadata. Keep it outside source control and synced folders.")
    st.download_button("Download review plan", json.dumps(plan, ensure_ascii=False, indent=2),
                       file_name="spottoyt-review-plan.json", mime="application/json")


def transfer_workspace(reports):
    import time
    from spottoyt.transfers import (PREFLIGHT_SECONDS, create_plan, execute, file_digest, load_plan,
                                   operation_counts, plan_folder, preflight, transfer_root)

    st.header("Transfer plan")
    if os.environ.get('SPOTTOYT_DEMO') == '1':
        st.info('Simulation only. Likes and playlist changes affect the temporary invented destination, not a real account.')
    st.write("Prepare a fixed plan from your approvals, check the destination, then choose whether to execute it.")
    with st.expander("Plan options", expanded=False):
        st.checkbox("Separate YouTube Music Library saves (not supported by official API)", value=False, disabled=True)
        also_save = False
        partial = st.checkbox("Transfer partial playlists and fill reviewed gaps later", value=True)
        st.caption("Private copies keep source occurrence checkpoints. Later approvals are inserted into their original order, with duplicates preserved. Source changes or external playlist edits stop completion for review. Music-specific library and podcast saves remain unsupported.")
    if st.button("Prepare plan from selected reports"):
        try:
            folder = create_plan(reports, also_save_to_library=also_save, allow_partial_playlists=partial)
            st.session_state["selected-transfer-plan"] = folder.name
            st.session_state["review_flash"] = "Transfer plan prepared locally. No account changes."
            st.rerun()
        except SetupError as error:
            st.error(str(error))
    identifiers = sorted([path.name for path in transfer_root().iterdir() if path.is_dir()], reverse=True)
    if not identifiers:
        st.info("No frozen transfer plan yet. Prepare one after approving matches.")
        return
    if st.session_state.get("selected-transfer-plan") not in identifiers:
        st.session_state["selected-transfer-plan"] = identifiers[0]
    identifier = st.selectbox("Saved transfer plan", identifiers, key="selected-transfer-plan")
    try:
        plan = load_plan(identifier)
    except SetupError as error:
        st.warning(str(error))
        return
    counts = operation_counts(plan)
    st.dataframe([{"Action": action.replace("_", " ").capitalize(), "Planned": count} for action, count in counts.items()],
                 hide_index=True, width="stretch")
    st.caption(f"{len(plan['held_playlists'])} playlists held · {plan['unsearched_music']} songs outside the current matching report")
    if plan["held_playlists"]:
        with st.expander("Playlists held for later"):
            st.dataframe(plan["held_playlists"], hide_index=True, width="stretch")
    playlists = [operation for operation in plan['operations'] if operation['action'] == 'playlist']
    if playlists:
        st.caption(f"Approved playlist entries: {sum(len(operation['video_ids']) for operation in playlists)} · Unresolved entries left out: {sum(operation['unresolved_entries'] for operation in playlists)}")
    with st.expander("Exact planned items"):
        st.dataframe([{"Action": op["action"], "Source title": op["title"],
                       "Playlist entries": len(op.get("video_ids", [])), "Target": op.get("target_id", "Private playlist copy")}
                      for op in plan["operations"]], hide_index=True, width="stretch")
    st.info("This plan contains supported, approved operations. Album saves, artist follows, audiobooks, history and playback progress are not included.")
    folder = plan_folder(identifier)
    checked = None
    if (folder / "preflight.json").exists():
        checked = json.loads((folder / "preflight.json").read_text(encoding="utf-8"))
    ready = bool(checked and checked.get("state") == "ready" and 0 <= time.time() - checked.get('completed_at_epoch', checked.get("created_at_epoch", 0)) <= PREFLIGHT_SECONDS
                 and (folder / "preflight.sha256").exists()
                 and file_digest(folder / "preflight.json") == (folder / "preflight.sha256").read_text(encoding="ascii").strip())
    if checked:
        st.caption("Destination check: " + ("Ready" if ready else "Blocked or expired"))
        if checked.get("account"):
            st.text("Destination account: " + str(checked["account"].get("account_name", "")) + " " + str(checked["account"].get("channel_handle", "")))
        if checked.get("reason"):
            st.warning(checked["reason"])
    if st.button("Check destination (read-only)"):
        with st.status("Checking the account and approved targets…", expanded=True) as status:
            result = preflight(identifier, progress=status.write)
            status.update(label="Destination checks complete", state="complete" if result["state"] == "ready" else "error")
        st.session_state["transfer-revision-" + identifier] = st.session_state.get("transfer-revision-" + identifier, 0) + 1
        st.rerun()
    revision = st.session_state.get("transfer-revision-" + identifier, 0)
    with st.form("execute-" + identifier + str(revision)):
        confirmed = st.checkbox("I authorize the exact planned likes, saves and private playlist copies shown above")
        submitted = st.form_submit_button("Simulate this transfer" if os.environ.get('SPOTTOYT_DEMO') == '1' else "Execute this transfer", type="primary", disabled=not ready)
    if submitted:
        try:
            with st.status("Applying the approved plan…", expanded=True) as status:
                result = execute(identifier, confirmed=confirmed, progress=status.write)
                status.update(label="Transfer verified" if result['state'] == 'verified' else 'Transfer paused; progress saved', state="complete")
            st.session_state["transfer-revision-" + identifier] = revision + 1
            st.session_state["review_flash"] = (f"Transfer verified: {result['completed']} operations." if result['state'] == 'verified'
                                               else f"Transfer paused: {result.get('reason', 'Progress saved; resume the same plan.')}")
            st.rerun()
        except SetupError as error:
            st.error(str(error))
    if (folder / "execution.json").exists():
        result = json.loads((folder / "execution.json").read_text(encoding="utf-8"))
        st.caption(f"Last execution: {result['state']} · {result['completed']}/{result['total']} verified operations")
    st.caption("Resuming the same plan reads destination state first. No automatic deletion, undo, or blind write retries.")


def verification_workspace():
    from spottoyt.reconciliation import list_verifications, verify_transfer
    from spottoyt.review import RUN_ID
    from spottoyt.transfers import transfer_root

    st.header("Verification and gaps")
    st.write("Read back the planned destination state and see what still needs attention. Viewing saved reports stays offline.")
    identifiers = sorted([path.name for path in transfer_root().iterdir() if path.is_dir() and RUN_ID.fullmatch(path.name)], reverse=True)
    if identifiers:
        identifier = st.selectbox("Plan to verify", identifiers)
        downloads = [row for row in inventories() if row['data']['metadata'].get('source') == 'spotify_account_download']
        imported = st.multiselect("Include imported Spotify downloads", [row['id'] for row in downloads],
                                 help="Choose your real imports. Synthetic examples remain separate.")
        if st.button("Read destination and create verification report"):
            with st.status("Reading destination state…", expanded=True) as status:
                verify_transfer(identifier, imported, progress=status.write)
                status.update(label="Verification report saved", state="complete")
            st.rerun()
    else:
        st.info("No transfer plan yet. Prepare and review a plan before checking its destination.")
    reports, errors = list_verifications()
    if errors:
        st.warning("Some saved verification reports failed validation and were not displayed.")
    if not reports:
        st.info("No saved verification report yet.")
        return
    selected = st.selectbox("Saved verification report", range(len(reports)),
                            format_func=lambda index: reports[index]['run_id'])
    report = reports[selected]['data']
    st.caption(f"Plan: {report['plan_id']} · {report['created_at']} · {report['state']}")
    columns = st.columns(3)
    columns[0].metric("Verified operations", report['counts'].get('verified', 0))
    columns[1].metric("Unchecked operations", report['unchecked_operations'])
    columns[2].metric("Held playlists", len(report['gaps']['held_playlists']))
    st.info("This report covers the frozen plan. It does not claim a complete account migration.")
    if report.get('reason'):
        st.warning(report['reason'])
    if report['operations']:
        st.dataframe(report['operations'], hide_index=True, width="stretch")
    for summary in report['gaps'].get('review_summary', []):
        st.text(f"{summary['kind']} decisions at plan creation: {summary['decisions']}")
    st.caption(f"{len(report['gaps'].get('approved_targets_without_operations', []))} approved targets have no executable operation because their playlists remain held.")
    if report['gaps']['held_playlists']:
        with st.expander("Held playlists and unresolved entries"):
            st.dataframe(report['gaps']['held_playlists'], hide_index=True, width="stretch")
    st.write("History, playback progress and Spotify social follows stay in the archive. Album and artist entity mappings remain unsupported.")
    for download in report['account_downloads']:
        st.text(f"Imported {download['archive_id']}: {len(download['files'])} verified JSON files; {download['collections'].get('history', 0)} history events.")
    st.download_button("Download verification report", json.dumps(report, ensure_ascii=False, indent=2),
                       file_name="spottoyt-verification.json", mime="application/json")
    st.caption("Reports contain private library identifiers. Keep downloads outside source control and synced folders.")


def main():
    st.set_page_config(page_title="Spottoyt · Library review", page_icon=ICON, layout="wide", initial_sidebar_state="collapsed")
    st.markdown("""<style>
      .block-container {max-width:1180px;padding-top:4.5rem;padding-bottom:3rem;}
      h1 {letter-spacing:-.04em;} h2 {letter-spacing:-.025em;}
      [data-testid="stMetric"] {background:#edf1eb;border:1px solid #d9e2d8;border-radius:12px;padding:16px;}
      [data-testid="stForm"] {border-radius:14px;}
      @media(max-width:600px){.block-container{padding:4rem 1rem 2rem;} h1{font-size:2rem;}}
    </style>""", unsafe_allow_html=True)
    st.caption("SPOTTOYT / YOUR LIBRARY, YOUR CHOICES")
    if os.environ.get("SPOTTOYT_DEMO") == "1":
        from spottoyt.archive import private_root
        from spottoyt.demo import require_demo_root
        try:
            require_demo_root(private_root())
        except SetupError as error:
            st.error(str(error))
            return
        st.caption("DEMONSTRATION · SYNTHETIC DATA · OFFLINE SIMULATED DESTINATION")
        st.info('All records are invented. Account credentials and external service calls are disabled. Listening links are disabled. Demo changes are discarded when the demo stops.')
    st.title("Make the move yours.")
    st.write("Review your music and podcasts before they travel.")
    if "review_flash" in st.session_state:
        st.success(st.session_state.pop("review_flash"))
    page = st.radio("Workspace", ["Overview", "Music", "Podcasts", "Approved preview", "Transfer plan", "Verification"], horizontal=True, key="workspace")
    store = None
    try:
        with st.spinner("Opening your local reports…"):
            selected = {}
            for kind in ("music", "podcasts"):
                reports, errors = list_reports(kind)
                if errors:
                    st.warning(f"Some {kind} reports could not be opened. Regenerate damaged reports before reviewing them.")
                if reports:
                    with st.sidebar:
                        choice = st.selectbox("Music report" if kind == "music" else "Podcast report", range(len(reports)),
                                              format_func=lambda index, available=reports: run_label(available[index]), key="run-" + kind)
                    selected[kind] = reports[choice]
            store = ReviewStore()
        st.sidebar.caption("Decisions belong to the selected report. Older choices remain saved with their original report.")
        if page == "Overview":
            overview(list(selected.values()), store)
        elif page == "Approved preview":
            preview(list(selected.values()), store)
        elif page == "Transfer plan":
            transfer_workspace(list(selected.values()))
        elif page == "Verification":
            verification_workspace()
        else:
            kind = "music" if page == "Music" else "podcasts"
            if kind not in selected:
                st.info("No matching report yet. Run match-music or match-podcasts from the terminal, then reload this page.")
            else:
                review_queue(selected[kind], store)
        st.divider()
        st.caption("Local workspace · transfer controls require a checked plan and explicit confirmation · choices persist on this computer")
    except SetupError as error:
        st.error(str(error))
    except Exception:
        st.error("The local review workspace could not be opened. Check the private storage location and report files. Sensitive details were not displayed.")
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    main()
