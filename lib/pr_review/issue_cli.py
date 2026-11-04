"""Terminal adapter for issue coordination services."""
import argparse
import contextlib
import json
import os
import sys
from .application import dispatch
from .common import ApiError, local_sessions
from .events import event_sink
from .processes import cancellation_scope
from . import cli_settings


def parser():
    result = cli_settings.Parser(prog='fix-issue', description='Plan GitHub issues and delegate approved implementation to a coding harness.')
    target = result.add_mutually_exclusive_group(required=False)
    target.add_argument('-i', '--issue', type=int)
    target.add_argument('-p', '--pr', type=int)
    result.add_argument('-R', '--repo', dest='repository')
    result.add_argument('-f', '--force', action='store_true', help='override issue/PR assignment checks only')
    approval = result.add_mutually_exclusive_group()
    approval.add_argument('--yolo', action='store_true', help='skip plan approval for this invocation; questions still block')
    approval.add_argument('--approve', type=int, metavar='REV', help='record approval of this exact published plan revision')
    result.add_argument('--max-repair-cycles', type=int, default=argparse.SUPPRESS, help='positive repair-cycle allowance; new batches default to 3')
    result.add_argument('--retry-review-adjudication', action='store_true', help='retry only the saved unresolved adjudication once')
    result.add_argument('--adopt', action='store_true', help='adopt inspected partial Owner workspace changes and reverify')
    recovery = result.add_mutually_exclusive_group()
    recovery.add_argument('--inspect-writes', action='store_true', help='show saved comment/PR intents and fresh read-back; do not send')
    recovery.add_argument('--retry-absent', metavar='ACTION', help='confirm after inspection that this uncertain action was not sent; allow a later retry')
    result.add_argument('--min-weekly-quota-remaining', type=lambda v: None if v == 'none' else float(v), default=argparse.SUPPRESS, help='none disables; stop at this remaining account weekly percentage')
    result.add_argument('--max-weekly-quota-use', type=lambda v: None if v == 'none' else float(v), default=argparse.SUPPRESS, help='none disables; maximum observed weekly percentage-point increase during execution')
    result.add_argument('--max-runtime', type=lambda v: None if v == 'none' else v, default=argparse.SUPPRESS, help='cumulative active execution allowance, e.g. 30m or 2h; none disables')
    result.add_argument('--json', action='store_true')
    result.add_argument('--trust-agent-config', dest='trusted_snapshot')
    presets = result.add_mutually_exclusive_group()
    presets.add_argument('--economical', action='store_true')
    presets.add_argument('--expensive', action='store_true')
    cli_settings.add_arguments(result, issue=True)
    return result


def main(argv=None):
    os.umask(0o077)
    args = vars(parser().parse_args(argv))
    if args.get('generate_json_config') is not None:
        try:
            cli_settings.generate(args['generate_json_config'], issue=True)
            return 0
        except ApiError as exc:
            print('Error: ' + str(exc), file=sys.stderr)
            return 1
    if not args.get('issue') and not args.get('pr'):
        parser().error('An issue or PR target is required unless --generate-json-config is used.')
    if any(args.get(k) is not None and args[k] < 1 for k in ('issue', 'pr', 'approve', 'max_repair_cycles')):
        parser().error('Issue, PR, revision and repair allowance must be positive integers.')
    def progress(kind, **fields):
        if kind == 'progress':
            print(fields['message'], file=sys.stderr, flush=True)
    try:
        cli_settings.prepare(args, issue=True)
        with local_sessions(), cancellation_scope(), event_sink(progress), contextlib.redirect_stdout(sys.stderr):
            operation = 'issue-recover' if args['inspect_writes'] or args['retry_absent'] is not None else 'issue-run'
            result = dispatch({'command': operation, **args})
    except (ApiError, OSError, ValueError) as exc:
        result = {'outcome': 'failed', 'error': str(exc), 'exit_code': 130 if isinstance(exc, KeyboardInterrupt) else 1}
        if getattr(exc, 'usage', None):
            result['usage'] = exc.usage
        if isinstance(exc, ApiError):
            result.update(reason_code=exc.code, details=exc.details)
            if exc.details.get('usage'):
                result['usage'] = exc.details['usage']
            result['exit_code'] = 130 if exc.code == 'cancelled' else 3 if exc.code == 'busy' else exc.details.get('exit_code', 1)
    except KeyboardInterrupt as exc:
        result = {'outcome': 'interrupted', 'exit_code': 130}
        if getattr(exc, 'usage', None):
            result['usage'] = exc.usage
    if args['json']:
        print(json.dumps(result))
    else:
        print(result['outcome'])
        for key in ('issue_url', 'pr_url', 'session', 'worktree', 'next_action', 'error'):
            if result.get(key):
                print(f'{key}: {result[key]}')
        if 'actions' in result:
            print(json.dumps(result['actions'], indent=2))
    return result['exit_code']
