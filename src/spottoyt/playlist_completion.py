"""Fill reviewed source occurrences into existing private copies without guessing."""

from spottoyt.auth import PlaylistNotReady, SetupError


def ids(entries):
    return [entry['video_id'] for entry in entries]


def inspect_completion(state, operation, saved):
    desired = operation['occurrences']
    positions = [entry['source_position'] for entry in desired]
    if (any(type(position) is not int or position < 0 for position in positions) or
            positions != sorted(set(positions)) or ids(desired) != operation['video_ids']):
        raise SetupError('Invalid source occurrence manifest. No playlist changes allowed.')
    if state is not None and (state['privacy'] != 'PRIVATE' or operation['marker'] not in state['description'] or
                              state['title'] != operation['title']):
        raise SetupError('Destination playlist privacy, title or provenance changed. Resolve external edits before resuming.')
    if saved is None:
        if state is None or not state['video_ids']:
            entries = []
        elif state['video_ids'] == operation['video_ids']:
            # Upgrade an already complete legacy copy; never guess a partial alignment.
            entries = list(desired)
        else:
            raise SetupError('Existing partial playlist has no occurrence checkpoints. Reconcile its provenance before adding songs.')
        saved = {'source_signature': operation['source_signature'], 'destination_id': state['id'] if state else None,
                 'entries': entries, 'pending': None}
    if saved['source_signature'] != operation['source_signature'] or state and saved['destination_id'] != state['id']:
        raise SetupError('Source playlist order or destination mapping changed. No automatic replacement or duplication.')
    entries = saved['entries']
    pending = saved.get('pending')
    if state is not None:
        if pending and state['video_ids'] == ids(pending['after']):
            entries = pending['after']
        elif state['video_ids'] != ids(entries):
            raise SetupError('Destination contents differ from the checkpoint and pending insertion. No repair or duplicate insert attempted.')
    desired_by_position = {entry['source_position']: entry for entry in desired}
    existing_positions = [entry['source_position'] for entry in entries]
    if existing_positions != sorted(set(existing_positions)) or any(desired_by_position.get(entry['source_position']) != entry for entry in entries):
        raise SetupError('A transferred occurrence was removed or changed by later review. Existing items will not be deleted or replaced.')
    return {**saved, 'entries': entries, 'pending': None}


def complete_playlist(plan, operation, destination, ledger, account, state, max_inserts=None):
    progress = inspect_completion(state, operation, ledger.playlist_progress(account, operation['source_id']))
    ledger.save_playlist_progress(account, operation['source_id'], progress)
    inserted = 0
    for entry in operation['occurrences']:
        if entry in progress['entries']:
            continue
        # Re-read before every insertion so external edits cannot be silently merged.
        state = destination.playlist_state(progress['destination_id'])
        progress = inspect_completion(state, operation, progress)
        index = sum(item['source_position'] < entry['source_position'] for item in progress['entries'])
        after = progress['entries'][:index] + [entry] + progress['entries'][index:]
        pending = {**progress, 'pending': {'after': after}}
        ledger.save_playlist_progress(account, operation['source_id'], pending)
        ledger.mark(plan['plan_id'], operation['op_id'], 'in_flight')
        response = destination.call('insert_playlist_item', progress['destination_id'], entry['video_id'], index)
        if not isinstance(response, dict) or not str(response.get('status', '')).endswith('SUCCEEDED'):
            raise SetupError('Insertion outcome is uncertain. Resume the same plan to reconcile before another write.')
        for attempt in range(4):
            try:
                state = destination.playlist_state(progress['destination_id'])
            except PlaylistNotReady:
                if attempt == 3:
                    raise
            else:
                if state['video_ids'] == ids(after):
                    break
                if state['video_ids'] != ids(progress['entries']) or attempt == 3:
                    raise SetupError('Inserted playlist sequence was not verified. Stop and reconcile the same plan.')
            destination.pause(2 ** attempt)
        progress = inspect_completion(state, operation, pending)
        ledger.save_playlist_progress(account, operation['source_id'], progress)
        inserted += 1
        if max_inserts is not None and inserted >= max_inserts:
            return progress['entries'] == operation['occurrences']
    return True
