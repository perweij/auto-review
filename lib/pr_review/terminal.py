"""Terminal draft UI over the same operations used by Emacs."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

from .common import ApiError, state_root
from .application import dispatch
from .processes import cancellation_scope


def call(request):
    with cancellation_scope():
        return dispatch(request)


def edit_text(body):
    """Keep a private recovery file until the optimistic save has succeeded."""
    directory = state_root() / 'editor-recovery'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='draft-', suffix='.md', dir=directory)
    path = Path(name)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
        stream.write(body)
    try:
        editor = shlex.split(os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vi')
        if not editor:
            raise ApiError('editor_failed', 'VISUAL/EDITOR contains no command.')
        subprocess.run([*editor, str(path)], check=True)
        # Preserve CRLF and trailing newlines exactly as the editor saved them.
        return path.read_bytes().decode('utf-8'), path
    except BaseException:
        print(f'Edited text retained for recovery: {path}')
        raise


def inspect(comment):
    print(f"\n{comment['title']} · {comment['placement']}\n{comment['body']}")
    for row in comment['context'].get('rows', []):
        print(f"{row['old'] or '':>5} {row['new'] or '':>5} {'>' if row['highlight'] else ' '} {row['kind']}{row['text']}")
    for entry in (comment['context'].get('thread') or {}).get('comments', []):
        print(f"\n@{entry['author']}: {entry['body']}")
    print('\n'.join(comment['blockers']))
    suggestion = comment.get('suggestion_preview')
    if suggestion:
        print('\nSuggested change: ' + str(suggestion['target']))
        print(suggestion['diff'])
        verification = suggestion['verification']
        print('Verification: ' + verification.get('status', 'required'))
        print(verification.get('evidence', ''))
        for check in verification.get('checks', []):
            print(check['command'] + ': ' + check['result'])
            if check.get('supersedes') is not None:
                print(f"  supersedes setup check {check['supersedes'] + 1}")
        if verification.get('inspection_only_reason'):
            print('No executable checks run: ' + verification['inspection_only_reason'])
        batch = suggestion.get('batch_verification', {})
        if batch:
            print('Combined verification: ' + batch.get('status', 'required') + '. ' + batch.get('evidence', ''))
    generation = comment.get('suggestion_generation', {})
    if generation.get('reason'):
        print('Suggestion: ' + generation['reason'])
    if comment.get('receipt'):
        print('Posted: ' + comment['receipt'].get('html_url', '(see saved receipt)'))


def terminal(path):
    selector = {'session': path}
    while True:
        review = call({**selector, 'command': 'read'})
        request = {**selector, 'generation': review['generation'], 'revision': review['revision']}
        for status in ('unstaged', 'staged', 'suppressed', 'published'):
            print(f'\n{status.title()}')
            for comment in review['comments']:
                if comment['status'] == status:
                    print(f"  {comment['id']}: {comment['title']}" + (' (needs attention)' if comment['blockers'] else ''))
        print(f"\nNext verdict: {review['verdict']}. Model suggestion: {review['recommendation'] or 'none (limited follow-up)'}")
        if review.get('coverage', {}).get('kind') == 'followup':
            print(f"Limited coverage: {len(review['coverage']['threads'])} selected conversations; not a full PR review.")
        print('s ID stage · u ID unstage · k ID suppress · r ID restore · e ID edit · i ID inspect')
        print('a ID place · ack ID acknowledge discussion · new create overall assessment · v VERDICT')
        print('se ID edit suggestion · sx ID remove suggestion · sv verify active suggestions')
        print('refresh · resolve uncertain publication · history · show GENERATION · current · docs · recover GENERATION ID')
        print('recover-doc GENERATION NAME · p preview/publish staged · q quit')
        recovery = None
        try:
            choice = input('> ').strip().split(maxsplit=1)
            if not choice or choice[0] in ('q', 'n'):
                return
            key, identity = choice[0], choice[1] if len(choice) > 1 else ''
            if key == 'history':
                for item in call({**selector, 'command': 'history'})['generations']:
                    print(f"{item['generation']} {'current' if item['current'] else 'historical'} · {item.get('head')} · {item.get('path', '')}")
                continue
            if key == 'show':
                call({**selector, 'generation': identity, 'command': 'read'})
                selector['generation'] = identity
                continue
            if key == 'current':
                selector.pop('generation', None)
                continue
            if key == 'docs':
                for document in review['documents']:
                    print(f"\n{document['name']}\n{document['text']}")
                continue
            if key in ('refresh', 'resolve'):
                call({**request, 'command': key})
                continue
            if key == 'sv':
                result = call({**request, 'command': 'verify-suggestions'})
                for comment in result['review']['comments']:
                    if comment.get('suggestion'):
                        inspect(comment)
                continue
            if key == 'p':
                prepared = call({**request, 'command': 'prepare'})
                if not prepared['ready']:
                    print(prepared['reason'])
                    continue
                for comment in prepared['review']['comments']:
                    if comment['status'] == 'staged':
                        inspect(comment)
                for warning in prepared.get('warnings', []):
                    print(warning)
                if input(f"Publish these {prepared['counts']} with {prepared['verdict'] or 'replies only'}? Type yes: ") == 'yes':
                    result = call({**request, 'revision': prepared['review']['revision'], 'command': 'publish', 'confirmation': prepared['confirmation']})
                    print('Published.' if result['complete'] else result['reason'])
                continue
            mutation = {**request, 'command': 'mutate'}
            if key == 'v':
                mutation.update(operation='verdict', verdict=identity.upper())
            elif key == 'new':
                body, recovery = edit_text('')
                mutation.update(operation='create', body=body, placement={'kind': 'review'}, title=identity or 'Overall assessment')
            elif key in ('recover', 'recover-doc'):
                source, item = identity.split(maxsplit=1)
                mutation.update(operation='recover', source_generation=source)
                mutation['source_comment' if key == 'recover' else 'document'] = item
            else:
                comment = next((c for c in review['comments'] if c['id'] == identity), None)
                if comment is None:
                    print('Choose a comment ID.')
                    continue
                mutation['comment'] = identity
                if key == 'i':
                    inspect(comment)
                    continue
                if key == 'se':
                    if not comment.get('suggestion'):
                        raise ValueError('Choose a generated suggestion.')
                    replacement, recovery = edit_text(comment['suggestion']['replacement'])
                    mutation.update(operation='suggestion-edit', replacement=replacement)
                elif key == 'sx':
                    mutation.update(operation='suggestion-remove')
                elif key == 'e':
                    body, recovery = edit_text(comment['body'])
                    mutation.update(operation='edit', body=body)
                elif key == 'a':
                    place = shlex.split(input("Placement: 'overall', 'auto', 'thread KEY', or 'PATH SIDE START END': "))
                    if place == ['auto']:
                        mutation.update(operation='automatic')
                    else:
                        if place == ['overall']:
                            target = {'kind': 'review'}
                        elif len(place) == 2 and place[0] == 'thread':
                            target = {'kind': 'reply', 'key': place[1]}
                        elif len(place) == 4:
                            target = {'kind': 'inline', 'path': place[0], 'side': place[1].upper(), 'start_line': int(place[2]), 'line': int(place[3])}
                        else:
                            print('Invalid placement.')
                            continue
                        mutation.update(operation='placement', placement=target)
                elif key in ('s', 'u', 'k', 'r', 'ack'):
                    mutation['operation'] = {'s': 'stage', 'u': 'unstage', 'k': 'suppress', 'r': 'restore', 'ack': 'acknowledge'}[key]
                else:
                    print('Unknown command.')
                    continue
            result = call(mutation)
            if recovery:
                recovery.unlink()
                recovery = None
            for skipped in result.get('skipped', []):
                print('; '.join(skipped['reasons']))
        except EOFError:
            return
        except (ApiError, ValueError, OSError, subprocess.CalledProcessError) as exc:
            print(str(exc))
        finally:
            if recovery:
                print(f'Edited text retained for recovery: {recovery}')
