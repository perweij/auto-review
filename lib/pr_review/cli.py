"""Terminal arguments and presentation over the shared application service."""
import argparse
import json
import os
from pathlib import Path
import sys
from .common import ApiError, local_sessions
from .events import event_sink
from .processes import Cancelled


def parser():
    result = argparse.ArgumentParser(prog='pr-review', description='Review a GitHub PR with optional publication. For incoming feedback and fixes, use pr-review owner --help.',
        epilog='Environment: PR_REVIEW_CLAUDE_PERMISSION_MODE (auto/dontAsk/manual/plan), PR_REVIEW_MODEL_TIMEOUT (optional seconds), PR_REVIEW_CLAUDE_BUDGET (optional USD), PR_REVIEW_AUTH_TIMEOUT (default 60), PR_REVIEW_CODEX_PROBE_MODEL, XDG_STATE_HOME, VISUAL/EDITOR.')
    result.add_argument('pr', help='positive PR number or https://HOST/OWNER/REPO/pull/NUMBER')
    result.add_argument('-R', '--repo', dest='repository')
    result.add_argument('--no-publish', action='store_true', help='generate and retain reviews without opening the publication menu')
    result.add_argument('--auto-publish', action='store_true', help='analyse and publish COMMENT feedback without prompts; stop the whole batch if any finding needs attention')
    result.add_argument('--json', action='store_true', help='with --auto-publish, emit one JSON result on stdout and progress on stderr')
    result.add_argument('--resume', dest='session', help='explicit saved run, including an external state location')
    result.add_argument('--followup-thread', dest='followup_threads', action='append', default=[])
    for key in ('codex-model', 'claude-model', 'processing-provider', 'claude-effort', 'codex-effort', 'synthesis-tests'):
        result.add_argument('--' + key, default=argparse.SUPPRESS)
    result.add_argument('--reviewers', type=lambda value: value.split(','), default=argparse.SUPPRESS)
    result.add_argument('--trust-agent-config', dest='trusted_snapshot')
    return result


def main(argv=None):
    os.umask(0o077)
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == 'api':
        from .api import main as api
        api()
        return 0
    if argv and argv[0] == 'owner':
        from .owner_cli import main as owner
        return owner(argv[1:])
    if len(argv) == 2 and argv[0] == 'drafts':
        from .terminal import terminal
        try:
            with local_sessions():
                terminal(str(Path(argv[1]).resolve()))
            return 0
        except (ApiError, OSError, ValueError) as exc:
            print('Error: ' + str(exc), file=sys.stderr)
            return 1
        except (KeyboardInterrupt, EOFError):
            return 130
    request = vars(parser().parse_args(argv))
    if request['auto_publish'] and (request['no_publish'] or request['followup_threads']):
        parser().error('--auto-publish cannot be combined with --no-publish or --followup-thread')
    if request['json'] and not request['auto_publish']:
        parser().error('--json requires --auto-publish')
    if any(not k.startswith('thread:') or len(k) <= 7 for k in request['followup_threads']):
        parser().error('Use an actual thread: key for follow-up')
    request['followup_threads'] = sorted(set(request['followup_threads']))
    if request.get('synthesis_tests', 'focused') not in ('focused', 'full'):
        parser().error('--synthesis-tests must be focused or full')
    interactive = sys.stdin.isatty()
    def progress(kind, **fields):
        if kind == 'progress':
            print(fields['message'], file=sys.stderr if request['auto_publish'] else sys.stdout, flush=True)
    def trust(_paths):
        return input('Trust these configuration changes and run models? Type yes: ') == 'yes'
    try:
        from .reviews import execute
        with local_sessions(), event_sink(progress):
            if request['auto_publish']:
                from .automation import run
                result = run(request)
                if request['json']:
                    print(json.dumps(result))
                else:
                    print(result['outcome'] + ': ' + result['reason'])
                    for key in ('session', 'generation', 'log_path'):
                        if result.get(key):
                            print(key + ': ' + result[key])
                    for url in result.get('receipts', []):
                        print(url)
                    if result['exit_code']:
                        print(result['guidance'])
                return result['exit_code']
            try:
                session = execute(request, confirm_trust=trust if interactive else None)
            except ApiError as exc:
                if exc.code != 'uncertain_publication' or not interactive or request['no_publish']:
                    raise
                from .terminal import terminal
                print(str(exc))
                terminal(exc.details['session'])
                return 0
            if interactive and not request['no_publish']:
                from .terminal import terminal
                terminal(str(session.root))
        return 0
    except (ApiError, OSError, ValueError) as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        exit_code = exc.details.get('exit_code', 1) if isinstance(exc, Cancelled) else 1
        if request['auto_publish'] and request['json']:
            print(json.dumps(dict(outcome='interrupted' if isinstance(exc, Cancelled) else 'failed',
                                  exit_code=exit_code, reason=str(exc), affected=[], receipts=[])))
        return exit_code
    except (KeyboardInterrupt, EOFError):
        return 130
