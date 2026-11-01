"""Independent review, synthesis, discussion matching, and focused follow-up."""
from pathlib import Path
import shutil
import tempfile
from .common import ApiError, ROOT, locked, now, read_json
from . import cache, discussion, followup, providers, validation
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


def claude_result(run, stage, result):
    values = [e for e in result.events if e.get('type') == 'result']
    if not values or values[-1].get('is_error') is True or values[-1].get('subtype') != 'success':
        raise ApiError('model_failed', f'{stage} returned no successful Claude result event')
    value = values[-1]
    run.manifest['phases'][stage]['permission_denials'] = value.get('permission_denials', [])
    write_json(run.path / f'logs/{stage}-result.json', value)
    run.save()
    return value


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
    journal = run.manifest.get('slot_restart')
    if not journal:
        invalid = []
        for slot in slots:
            stage = slot['id']
            output = run.path / 'reviews' / (stage + '.md')
            if not cache.reusable(run, stage, output, cache.inputs(run, stage)):
                invalid.append(stage)
        phases = run.manifest['phases']
        if not invalid or not (any((run.path / 'reviews' / (stage + '.md')).exists() or stage in phases for stage in invalid)
                               or any(stage in phases for stage in ('synthesis', 'contributions', 'revision'))):
            return
        run.reset_publication()
        run.begin()
        history = run.path / 'restart-history'
        if history.is_symlink():
            raise ApiError('invalid_artifact', 'Invalid slot restart preservation directory')
        history.mkdir(exist_ok=True, mode=0o700)
        if history.resolve().parent != run.path.resolve():
            raise ApiError('invalid_artifact', 'Invalid slot restart preservation directory')
        archive = Path(tempfile.mkdtemp(prefix='slots-', dir=history))
        affected = invalid + ['synthesis', 'contributions', 'revision']
        files = []
        for folder in ('reviews', 'prompts', 'logs'):
            for path in (run.path / folder).iterdir():
                if path.is_file() and any(path.name.startswith(name + '.') or path.name.startswith(name + '-') for name in affected):
                    files.append(str(path.relative_to(run.path)))
        journal = dict(stages=affected, files=files, archive=str(archive))
        run.manifest['slot_restart'] = journal
        run.save()
    say('Restarting reviewer slots: ' + ', '.join(stage for stage in journal['stages'] if stage.startswith('reviewer-'))
        + f". Previous artifacts are preserved in: {journal['archive']}")
    archive = Path(journal['archive']).resolve()
    history = run.path / 'restart-history'
    if history.is_symlink() or history.resolve().parent != run.path.resolve() or archive.parent != history.resolve():
        raise ApiError('invalid_artifact', 'Invalid slot restart preservation path')
    if any(stage not in ('reviewer-1', 'reviewer-2', 'synthesis', 'contributions', 'revision') for stage in journal['stages']):
        raise ApiError('invalid_artifact', 'Invalid slot restart stage')
    for name in journal['files']:
        relative = Path(name)
        if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] not in ('reviews', 'prompts', 'logs'):
            raise ApiError('invalid_artifact', 'Invalid slot restart artifact')
        if not any(relative.name.startswith(stage + '.') or relative.name.startswith(stage + '-') for stage in journal['stages']):
            raise ApiError('invalid_artifact', 'Invalid slot restart artifact')
        source, destination = run.path / relative, archive / relative
        if (source.parent.is_symlink() or source.parent.resolve() != run.path.resolve() / relative.parts[0]
                or source.is_symlink() or destination.is_symlink()):
            raise ApiError('invalid_artifact', 'Symlink in slot restart artifacts')
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.parent.resolve() != archive / relative.parts[0]:
                raise ApiError('invalid_artifact', 'Invalid slot restart destination')
            if not destination.exists():
                atomic_bytes(destination, source.read_bytes())
            elif not destination.is_file() or hash_file(source) != hash_file(destination):
                raise ApiError('invalid_artifact', 'Slot restart artifact changed during preservation')
            source.unlink()
    for stage in journal['stages']:
        run.manifest['phases'].pop(stage, None)
    run.manifest.pop('reassessment', None)
    run.manifest.setdefault('review_history', []).append(dict(stages=journal['stages'], archive=str(archive), restarted_at=now()))
    run.manifest.pop('slot_restart')
    run.save()


def independent(run, stage):
    slot = next((item for item in run.settings.get('reviewer_slots', []) if item['id'] == stage), None)
    provider = providers.provider_for(run.settings, stage)
    output = run.path / (f'reviews/{stage}.md' if slot else
                         'reviews/claude-independent.md' if stage == 'claude' else 'reviews/codex.md')
    if cache.reusable(run, stage, output, cache.inputs(run, stage)):
        say(f'Reusing completed {stage.title()} review')
        return
    if stage == 'claude' and (run.path / 'reviews/codex.md').exists():
        raise ApiError('independence_failed', 'Independent Claude inputs/output changed after Codex ran; start a new run to preserve independence')
    output.with_suffix(output.suffix + '.tmp').unlink(missing_ok=True)
    say(f'[{stage}] Running independent {stage.title()} review')
    result, _ = run.model(stage, run.command(stage))
    if provider == 'claude':
        body = claude_result(run, stage, result).get('result')
        if not isinstance(body, str) or not body.strip():
            raise ApiError('model_failed', 'Claude returned an empty independent review')
        atomic_bytes(output, (body + '\n').encode())
    else:
        temp = output.with_suffix('.md.tmp')
        if not temp.is_file() or not temp.stat().st_size:
            raise ApiError('model_failed', 'Codex returned an empty review')
        atomic_bytes(output, temp.read_bytes())
        temp.unlink()
    run.complete(stage, output)


def synthesis(run):
    render_synthesis(run)
    output = run.path / 'reviews/synthesis.json'
    reused = cache.reusable(run, 'synthesis', output, cache.inputs(run, 'synthesis'))
    if reused:
        try:
            validation.synthesis(read_json(output))
        except ApiError:
            reused = False
    if reused:
        say('Reusing completed synthesis' if run.dual else 'Reusing completed structured review')
    else:
        protect(run, 'synthesis')
        run.reset_publication()
        render_synthesis(run)
        say(f'[synthesis] Running {"adversarial synthesis" if run.dual else "structured review (one reviewer)"} ({run.settings["processing_provider"]})')
        value = structured(run, 'synthesis')
        validation.synthesis(value, new=True)
        # A newly produced analysis gets a fresh discussion match, even when a
        # provider happened to return byte-identical findings to its predecessor.
        run.manifest['phases'].pop('contributions', None)
        output = run.path / 'reviews/synthesis.json'
        write_json(output, value)
        run.complete('synthesis', output)
    value = read_json(output)
    snapshot = run.manifest['snapshot']
    body = value['report_md'] + f"\n\n\n## Orchestrator coverage notes\n\nReviewed head `{snapshot['head']}`, merge-base `{snapshot['merge_base']}`.\n\nCI covers Actions runs and legacy statuses only. Third-party GitHub App check runs are not visible. Empty results do not mean CI passed.\n"
    body += ''.join('\n- ' + w + '\n' for w in run.manifest['warnings'])
    body += ''.join('\n- Retained generated file: ' + w + '\n' for w in run.manifest['generated_files'])
    atomic_bytes(run.path / 'reviews/synthesis.md', body.encode())


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
