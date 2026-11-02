"""Independent review, synthesis, discussion matching, and focused follow-up."""
from pathlib import Path
import shutil
import tempfile
from .common import ApiError, ROOT, locked, now, read_json
from . import cache, discussion, followup, providers, review_engine, validation
from .sessions import say
from .storage import atomic_bytes, hash_file, json_bytes, write_json


def render_synthesis(run):
    path, snapshot = run.path, run.manifest['snapshot']
    reports = cache.completed_slot_reports(run)
    slots = run.settings.get('reviewer_slots', [])
    if not reports and any(slot['id'] in run.manifest.get('phases', {}) for slot in slots):
        raise ApiError('incomplete_review', 'All configured reviewer slots must complete before synthesis')
    prompt = ('synthesis.txt' if run.dual else
              'single-review-with-report.txt' if reports else 'single-review.txt')
    content = (ROOT / 'prompts' / prompt).read_bytes()
    if run.settings['processing_provider'] == 'codex':
        content += (ROOT / f'prompts/synthesis-testing-{run.settings["synthesis_tests"]}.txt').read_bytes()
    metadata = dict(repository=run.repo.identity, pr=str(run.repo.pr), head=snapshot['head'], merge_base=snapshot['merge_base'],
                    context_directory=str(path / 'context'), codex_review=str(path / 'reviews/codex.md'), claude_review=str(path / 'reviews/claude-independent.md'),
                    context_files=['pr.json', 'pr-discussion.json', 'pr-reviews.json', 'pr-inline-comments.json', 'ci-actions-runs.json', 'ci-statuses.json', 'pr.diff', 'diffstat.txt'],
                    warnings=run.manifest['warnings'], reviewers=run.settings['reviewers'])
    if reports:
        metadata.pop('codex_review')
        metadata.pop('claude_review')
        metadata['reviewer_reports'] = [dict(slot=slot['id'], provider=slot['provider'], path=str(report))
                                        for slot, report in reports]
        content = content.replace(b'source (Codex, Claude, Both, New)', b'source (reviewer slot ID, both slots, or New)')
        if run.dual:
            content += b'\nAttribute each original finding to its reviewer slot ID, including when providers match.\n'
    elif not run.dual:
        metadata.pop('codex_review')
        metadata.pop('claude_review')
    content += json_bytes(metadata)
    reassessment = path / 'reviews/reassessment-input.json'
    if reassessment.is_file():
        content += b'\nFresh-discussion reassessment:\n' + reassessment.read_bytes()
    atomic_bytes(path / 'prompts/synthesis-prompt.txt', content)


def render_contributions(run):
    value = dict(findings=str(run.path / 'reviews/synthesis.json'), discussion=run.manifest['discussion']['path'],
                 head=run.manifest['snapshot']['head'], reassessment=run.manifest.get('reassessment'))
    atomic_bytes(run.path / 'prompts/contributions-prompt.txt', (ROOT / 'prompts/contributions.txt').read_bytes() + json_bytes(value))


def structured(run, stage):
    import json
    codex_output = run.path / f'reviews/{stage}.codex.tmp'
    codex_output.unlink(missing_ok=True)
    result, _ = run.model(stage, run.command(stage), (run.path / f'prompts/{stage}-prompt.txt').read_bytes())
    if run.settings['processing_provider'] == 'codex':
        value = read_json(codex_output)
    else:
        event = claude_result(run, stage, result)
        try:
            value = event.get('structured_output') or json.loads(event.get('result', ''))
        except ValueError as exc:
            raise ApiError('model_failed', 'No structured model result') from exc
    if not isinstance(value, dict):
        raise ApiError('model_failed', 'No structured model result')
    return value


claude_result = review_engine.claude_result


def protect(run, stage):
    if run.pending or run.manifest['phases'].get('contributions', {}).get('status') != 'completed':
        return
    run.begin()
    # A copied phase keeps the identity under which it actually ran. Rewriting
    # its fingerprint from current settings could certify stale work as new.
    run.save()


def restart(run):
    journal = run.manifest.get('review_restart')
    if not journal:
        start = None
        for stage, file in [('claude', 'claude-independent.md'), ('codex', 'codex.md')]:
            output = run.path / 'reviews' / file
            if cache.reusable(run, stage, output, cache.inputs(run, stage)):
                continue
            if output.exists() or stage in run.manifest['phases'] or (stage == 'claude' and (run.path / 'reviews/codex.md').exists()):
                start = stage
            break
        if not start:
            return
        run.reset_publication()
        run.begin()
        files = [p for p in (run.path / 'reviews').iterdir() if p.is_file()] + [p for p in (run.path / 'prompts').iterdir() if p.is_file()]
        files += [p for p in (run.path / 'logs').iterdir() if p.is_file() and p.name.startswith(('claude-', 'claude.', 'codex.', 'synthesis', 'contributions', 'revision'))]
        if start == 'codex':
            files = [p for p in files if not p.name.startswith(('claude-', 'claude.'))]
        journal = dict(**{'from': start}, archive=run.manifest['attempt']['previous'], files=list(map(str, files)),
                       reason='saved model, prompt, output or review inputs changed', staged=True)
        run.manifest['review_restart'] = journal
        run.save()
    if journal.get('staged') and not journal.get('preserved'):
        # A pending candidate can contain reports that never existed in its
        # predecessor. Preserve those bytes inside the candidate before removal.
        history = run.path / 'restart-history'
        history.mkdir(exist_ok=True, mode=0o700)
        journal['preserved'] = tempfile.mkdtemp(prefix='restart-', dir=history)
        run.save()
    if journal.get('staged'):
        history = (run.path / 'restart-history').resolve()
        preserved = Path(journal['preserved']).resolve()
        if preserved.parent != history:
            raise ApiError('invalid_artifact', 'Invalid review restart preservation path')
    say(f"Restarting reviews from {journal['from']}: {journal.get('reason', 'finishing the previously requested restart')}.\nPrevious reports and drafts are preserved in: {journal['archive']}")
    for value in journal['files']:
        path = Path(value)
        # A journal may name only artifacts of its own candidate.
        if not path.is_relative_to(run.path) or path.parent.name not in ('reviews', 'prompts', 'logs'):
            raise ApiError('invalid_artifact', 'Invalid review restart journal path')
        if path.is_file():
            if journal.get('staged'):
                destination = Path(journal['preserved']) / path.relative_to(run.path)
                atomic_bytes(destination, path.read_bytes())
                path.unlink()
            else:
                destination = Path(journal['archive']) / path.relative_to(run.path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(path, destination)
    for stage in ('claude', 'codex', 'synthesis', 'contributions', 'revision'):
        if stage != 'claude' or journal['from'] == 'claude':
            run.manifest['phases'].pop(stage, None)
    run.manifest.pop('reassessment', None)
    run.manifest.setdefault('review_history', []).append(dict(**{'from': journal['from']}, archive=journal['archive'],
        preserved=journal.get('preserved'), restarted_at=now()))
    run.manifest.pop('review_restart')
    run.save()


def adopt_legacy_slots(run, slots):
    """Copy only legacy reports verified under their original phase rules."""
    if (run.manifest.get('legacy_review_artifacts') and not run.pending and
            any(run.manifest['phases'].get(stage, {}).get('status') == 'completed'
                for stage in ('synthesis', 'contributions'))):
        run.begin()
    for slot in slots:
        stage = slot['id']
        item = run.manifest.get('legacy_review_artifacts', {}).get(stage)
        if not item or stage in run.manifest['phases'] or (run.path / 'reviews' / (stage + '.md')).exists():
            continue
        if any(slot[key] != item[key] for key in ('provider', 'model', 'effort')):
            continue
        source, old_stage = run.path / item['output'], item['phase']
        if not cache.reusable(run, old_stage, source, cache.inputs(run, old_stage)):
            continue
        old = run.manifest['phases'][old_stage]
        report = run.path / 'reviews' / (stage + '.md')
        atomic_bytes(report, source.read_bytes())
        record = dict(status='completed', output_hash=hash_file(report), exit_status=0,
                      legacy_source=dict(phase=old_stage, output=item['output'],
                                         fingerprint=old.get('fingerprint'), output_hash=hash_file(source)))
        run.manifest['phases'][stage] = record
        record['inputs'] = cache.inputs(run, stage)
        record['fingerprint'] = cache.fingerprint(record['inputs'])
        run.save()


def prepare_slots(run, slots):
    """Preserve invalid slot and downstream artifacts without invalidating peers."""
    review_engine.prepare_slots(run, slots)


def independent(run, stage):
    review_engine.independent(run, stage)


def write_report(run, value):
    snapshot = run.manifest['snapshot']
    body = value['report_md'] + f"\n\n\n## Orchestrator coverage notes\n\nReviewed head `{snapshot['head']}`, merge-base `{snapshot['merge_base']}`.\n\nCI covers Actions runs and legacy statuses only. Third-party GitHub App check runs are not visible. Empty results do not mean CI passed.\n"
    body += ''.join('\n- ' + w + '\n' for w in run.manifest['warnings'])
    body += ''.join('\n- Retained generated file: ' + w + '\n' for w in run.manifest['generated_files'])
    atomic_bytes(run.path / 'reviews/synthesis.md', body.encode())


def synthesis(run):
    review_engine.synthesis(run)


def contributions(run):
    try:
        discussion.collect(run.repo, run.path)
    except ApiError as exc:
        run.reload()
        if exc.code == 'incomplete_discussion':
            return False
        raise
    run.reload()
    for attempt in range(2):
        render_contributions(run)
        output = run.path / 'reviews/contributions.json'
        findings, conversation = read_json(run.path / 'reviews/synthesis.json'), read_json(run.manifest['discussion']['path'])
        reused = cache.reusable(run, 'contributions', output, cache.inputs(run, 'contributions'))
        if reused:
            try:
                value = validation.contributions(read_json(output), findings, conversation)
            except ApiError:
                reused = False
        if not reused:
            previous = run.path
            protect(run, 'contributions')
            if run.path != previous:
                # A rematch creates fresh drafts. Earlier publication belongs
                # to history and becomes ordinary discussion for this analysis.
                # Establish that baseline before recording matching inputs.
                run.reset_publication()
                try:
                    discussion.collect(run.repo, run.path)
                except ApiError as exc:
                    run.reload()
                    if exc.code == 'incomplete_discussion':
                        return False
                    raise
                run.reload()
                conversation = read_json(run.manifest['discussion']['path'])
            render_contributions(run)
            say('[final] Matching findings against the complete discussion')
            value = validation.contributions(structured(run, 'contributions'), findings, conversation)
            output = run.path / 'reviews/contributions.json'
            write_json(output, value)
        if not value['validity_conflicts']:
            if not reused:
                run.complete('contributions', output)
            break
        run.manifest['phases']['contributions']['status'] = 'needs_reassessment'
        run.active = None
        if attempt or run.manifest.get('reassessment', {}).get('discussion_hash') == run.manifest['discussion']['hash']:
            run.manifest['publication']['status'] = 'paused'
            run.save()
            say('Warning: Fresh discussion still challenges finding validity after reassessment. Publication paused.')
            return False
        input_path = run.path / 'reviews/reassessment-input.json'
        write_json(input_path, dict(prior_findings=findings['findings'], validity_conflicts=value['validity_conflicts'], discussion=run.manifest['discussion']['path']))
        run.manifest['reassessment'] = dict(discussion_hash=run.manifest['discussion']['hash'], input=str(input_path), discussion=run.manifest['discussion']['path'])
        run.save()
        say('Fresh discussion challenges validity; reassessing with the selected processing provider.')
        synthesis(run)
    ledger_path = run.path / 'publication/contributions.json'
    old = read_json(ledger_path, {})
    if old:
        fd, history = tempfile.mkstemp(prefix='contributions-history-', suffix='.json', dir=ledger_path.parent)
        import os
        os.close(fd)
        write_json(history, old)
    from .receipts import actions as saved_actions
    ledger = validation.merge_contributions(old, value, conversation, saved_actions(run.path),
                run.manifest['discussion']['hash'], hash_file(run.path / 'reviews/synthesis.json'), run.repo.url)
    write_json(run.path / 'publication/contributions.previous.json', old)
    write_json(ledger_path, ledger)
    if run.manifest['publication'].get('status') == 'paused':
        run.manifest['publication']['status'] = 'partial' if run.manifest['publication'].get('published_actions') else 'not_published'
    run.save()
    discussion.collect_status(run.repo, run.path)
    body = '# Proposed contributions\n' + ''.join(f"\n## {p['id']}: {p['classification']}\n\n{p['basis']['explanation']}\n\nNew contribution: {p['basis']['delta']}\n\nDestination: {p['destination']['url']}\n\n{p['body_md']}\n" for p in ledger['proposals'])
    atomic_bytes(run.path / 'reviews/contributions.md', body.encode())
    session = run.session()
    with locked(session.lock, wait=True):
        state = session.load()
        session.save(state)
    run.reload()
    slots = [slot['id'] for slot in run.settings.get('reviewer_slots', [])]
    if any(slot in run.manifest.get('phases', {}) for slot in slots):
        if not cache.completed_slot_reports(run):
            raise ApiError('incomplete_review', 'Reviewer reports changed before promotion')
        review_stages = slots
    else:
        review_stages = ['claude', 'codex'] if run.dual else []
    run.promote(review_stages + ['synthesis', 'contributions'])
    return True


def focused(run):
    discussion.collect(run.repo, run.path)
    run.reload()
    import json
    input_bytes = (json.dumps(followup.prepare(run.session()), ensure_ascii=True, indent=2) + '\n').encode()
    def prompt():
        return ((ROOT / 'prompts/followup.txt').read_bytes() + (ROOT / 'prompts/synthesis-testing-focused.txt').read_bytes()
                + json_bytes(dict(input=str(run.path / 'reviews/followup-input.json')), compact=True))
    output = run.path / 'reviews/followup.json'
    if cache.reusable(run, 'followup', output, cache.inputs(run, 'followup', followup_input=input_bytes, followup_prompt=prompt())):
        followup.validate(run.session(), read_json(output))
        say('Reusing completed follow-up assessment')
    else:
        if run.manifest['phases'].get('followup', {}).get('status') == 'completed':
            run.begin()
        atomic_bytes(run.path / 'reviews/followup-input.json', input_bytes)
        atomic_bytes(run.path / 'prompts/followup-prompt.txt', prompt())
        say(f'[followup] Checking selected conversations ({run.settings["processing_provider"]})')
        value = followup.validate(run.session(), structured(run, 'followup'))
        output = run.path / 'reviews/followup.json'
        write_json(output, value)
        run.complete('followup', output)
    run.repo.metadata(run.path / 'publication/pr-latest.json')
    discussion.collect(run.repo, run.path)
    discussion.collect_status(run.repo, run.path)
    run.reload()
    followup.adopt(run.session())
    run.reload()
    run.promote(['followup'])
