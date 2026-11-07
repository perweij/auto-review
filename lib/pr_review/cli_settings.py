"""Named terminal configuration, translated to the stable slot/Owner contracts.

Only terminal adapters opt in. Saved names are provenance; execution choices and
stopped issue transitions compare normalized roles. Interactive task batches
retain their frozen-settings rules.
"""
import argparse
import copy
import json
from pathlib import Path
import re
import sys

from .common import ApiError
from . import settings

NAME = re.compile(r'(claude|codex)-([1-9][0-9]*)\Z')
OPTION = re.compile(r'--((?:claude|codex)-[1-9][0-9]*)-(model|effort)\Z')
OBSOLETE = re.compile(r'--(?:(?:claude|codex)-(?:model|effort)|reviewer-[0-9]+-(?:provider|model|effort)|(?:processing|planning|fix)-(?:provider|model|effort))\Z')
LIMITS = ('max_runtime', 'min_weekly_quota_remaining', 'max_weekly_quota_use')


class Parser(argparse.ArgumentParser):
    """Accept arbitrary positive numbered names without limiting the namespace."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, allow_abbrev=False, **kwargs)

    def parse_known_args(self, args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        for token in args:
            flag = token.split('=', 1)[0]
            if OBSOLETE.fullmatch(flag):
                self.error(f'{flag} is obsolete. Use --reviewers codex-1,claude-1, '
                           '--processing codex-2 (or --planning/--fix NAME), '
                           'and --NAME-model / --NAME-effort.')
            if OPTION.fullmatch(flag) and flag not in self._option_string_actions:
                self.add_argument(flag, dest='named_' + flag[2:], default=argparse.SUPPRESS,
                                  help=argparse.SUPPRESS)
        return super().parse_known_args(args, namespace)


def add_arguments(parser, *, issue=False):
    built_in = defaults(issue=issue)
    roles = [('reviewers', ','.join(built_in['reviewers']))]
    roles += [(r, built_in[r]) for r in ('processing', 'planning', 'fix') if r in built_in]
    example = ('fix-issue -i 42 --reviewers codex-1,claude-1 --planning codex-1 --processing codex-1 --fix codex-2'
               if issue else 'pr-review 123 --reviewers codex-1 --processing codex-1')
    parser.epilog = (parser.epilog or '') + '\nDefaults: ' + '; '.join(f'{r}={n}' for r, n in roles) + '. ' + \
        '; '.join(f"{n}={c['provider']}/{c['model']}/{c['effort']}" for n, c in built_in['model_configs'].items()) + \
        f'. Examples: {parser.prog} --generate-json-config settings.json; ' + example + \
        ' --codex-1-effort medium. Complete files require every role, triplet, testing and execution field; ' + \
        'flags cannot repair partial files. Unused file definitions after role overrides must be removed. ' + \
        'Startup summaries use stderr, including in JSON mode.'
    parser.set_defaults(_cli_config=True)
    parser.add_argument('--settings-file', dest='cli_settings_file', default=argparse.SUPPRESS,
                        help='complete named JSON configuration; CLI flags override it in any order')
    parser.add_argument('--generate-json-config', metavar='PATH',
                        help='write complete built-in defaults without a target or repository; never overwrite')
    parser.add_argument('--reviewers', dest='cli_reviewers', type=lambda v: v.split(','),
                        default=argparse.SUPPRESS, metavar='NAME[,NAME]',
                        help='one or two distinct names in order, e.g. codex-1,claude-1')
    for role in ('processing', 'planning', 'fix') if issue else ('processing',):
        parser.add_argument('--' + role, dest='cli_' + role, default=argparse.SUPPRESS, metavar='NAME',
                            help=f'named configuration for {role}; shared references share model/effort')
    parser.add_argument('--synthesis-tests', choices=('focused', 'full'), default=argparse.SUPPRESS)
    group = parser.add_argument_group('named model choices',
        'Use --NAME-model and --NAME-effort for any positive numbered codex-N or claude-N. '
        'Every explicit definition must be referenced by a final role. Defaults are independent; '
        'precedence: defaults, preset, saved settings, complete file, explicit flags. '
        'Resume retains saved/frozen choices; forbidden changes fail before model work.')
    for name in ('codex-1', 'claude-1'):
        for field in ('model', 'effort'):
            group.add_argument(f'--{name}-{field}', dest=f'named_{name}-{field}',
                               default=argparse.SUPPRESS, metavar=field.upper(),
                               help=f'{field} for all roles referencing {name}')


def provider(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ApiError('invalid_request', f'Invalid configuration name {name!r}; use codex-1 or claude-1 (positive numbered names).')
    return NAME.fullmatch(name)[1]


def default_choice(name, values=None):
    p = provider(name)
    values = settings.DEFAULTS if values is None else values
    return {'provider': p, 'model': values[p + '_model'], 'effort': values[p + '_effort']}


def defaults(*, issue=False, request=None):
    values = settings.initial_settings(request or {})
    result = {'model_configs': {n: default_choice(n, values) for n in ('claude-1', 'codex-1', 'codex-2')},
              'reviewers': ['claude-1', 'codex-1'], 'processing': 'codex-2',
              'synthesis_tests': values['synthesis_tests']}
    if issue:
        for role, name in (('planning', 'codex-3'), ('fix', 'codex-4')):
            result[role] = name
            result['model_configs'][name] = default_choice(name)
        result.update(dict.fromkeys(LIMITS))
    else:
        result['suggested_changes'] = values['suggested_changes']
    return result


def references(value, *, issue=False):
    return [*value['reviewers'], *(value[r] for r in ('processing', 'planning', 'fix') if issue or r == 'processing')]


def validate(value, *, issue=False, explicit=()):
    required = set(defaults(issue=issue))
    if not isinstance(value, dict):
        raise ApiError('invalid_request', 'Settings file must be a complete JSON object.')
    missing, unknown = required - value.keys(), value.keys() - required
    if missing or unknown:
        raise ApiError('invalid_request', 'Complete settings required. ' +
                       ('Missing fields: ' + ', '.join(sorted(missing)) + '. ' if missing else '') +
                       ('Unknown fields: ' + ', '.join(sorted(unknown)) + '.' if unknown else ''))
    configs = value['model_configs']
    if not isinstance(configs, dict) or not configs:
        raise ApiError('invalid_request', 'model_configs must contain complete named provider/model/effort objects.')
    for name, choice in configs.items():
        p = provider(name)
        if not isinstance(choice, dict) or set(choice) != {'provider', 'model', 'effort'}:
            raise ApiError('invalid_request', f'{name} requires exactly provider, model, and effort.')
        if choice['provider'] != p:
            raise ApiError('invalid_request', f'{name}: provider must be {p}.')
        try:
            settings.validate({'processing': choice})
        except ApiError as exc:
            raise ApiError('invalid_request', f'{name}: {exc}') from exc
    reviewers = value['reviewers']
    if not isinstance(reviewers, list) or not 1 <= len(reviewers) <= 2:
        raise ApiError('invalid_request', 'reviewers must contain one or two distinct configuration names.')
    for name in references(value, issue=issue):
        provider(name)
        if name not in configs:
            raise ApiError('invalid_request', f'Undefined configuration {name}. Add its complete triplet to model_configs.')
    if len(set(reviewers)) != len(reviewers):
        raise ApiError('invalid_request', 'Duplicate reviewer names are not allowed.')
    unused = set(explicit) - set(references(value, issue=issue))
    if unused:
        raise ApiError('invalid_request', 'Unused explicit model configurations: ' + ', '.join(sorted(unused)) +
                       '. Remove their definitions or reference them from an active role.')
    settings.validate({k: value[k] for k in ('synthesis_tests', 'suggested_changes') if k in value})
    if issue:
        from .issue_resources import validate as validate_limits
        validate_limits(value)
    return copy.deepcopy(value)


def prepare(request, *, issue=False):
    """Validate a file on its own, before defaults, saved choices or flags exist."""
    path = request.get('cli_settings_file')
    if 'cli_settings_file' in request:
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('Duplicate JSON field: ' + key)
                    result[key] = value
                return result
            value = json.loads(Path(path).read_text(), object_pairs_hook=unique)
        except (OSError, ValueError) as exc:
            raise ApiError('invalid_request', f'Cannot read settings file {path}: {exc}') from exc
        request['_cli_file'] = validate(value, issue=issue)


def generate(path, *, issue=False):
    try:
        with Path(path).open('x', encoding='utf-8') as stream:
            json.dump(defaults(issue=issue), stream, indent=2)
            stream.write('\n')
    except OSError as exc:
        raise ApiError('invalid_request', f'Cannot generate configuration {path}: {exc}') from exc


def assign(value, role, choice, index=None):
    """Import a legacy/Owner choice, preserving identity when it still matches."""
    choice = {k: choice[k] for k in ('provider', 'model', 'effort')}
    current = (value['reviewers'][index] if index < len(value['reviewers']) else None) if index is not None else value.get(role)
    if value['model_configs'].get(current) == choice:
        name = current
    else:
        n = 1
        while f'{choice["provider"]}-{n}' in value['model_configs']:
            n += 1
        name = f'{choice["provider"]}-{n}'
        value['model_configs'][name] = choice
    if index is None:
        value[role] = name
    elif index < len(value['reviewers']):
        value['reviewers'][index] = name
    else:
        value['reviewers'].append(name)


def resolve(request, saved=None, *, issue=False, owner=None, resources=None):
    value = defaults(issue=issue, request=request)
    if saved:
        if saved.get('_named'):
            value = copy.deepcopy(saved['_named'])
            normalized = settings.validate(saved)
            for i, choice in enumerate(normalized['reviewer_slots']):
                assign(value, 'reviewers', choice, i)
            value['reviewers'] = value['reviewers'][:len(normalized['reviewer_slots'])]
            assign(value, 'processing', normalized['processing'])
            value['synthesis_tests'] = normalized['synthesis_tests']
            if issue:
                for role in ('planning', 'fix'):
                    if role in normalized:
                        assign(value, role, normalized[role])
            if not issue:
                value['suggested_changes'] = normalized['suggested_changes']
        else:
            normalized = settings.validate(saved)
            # Reserve separate names even when legacy role choices happen to match.
            value['model_configs'] = {}
            value['reviewers'] = []
            for role in ('processing', 'planning', 'fix'):
                value.pop(role, None)
            for i, choice in enumerate(normalized['reviewer_slots']):
                assign(value, 'reviewers', choice, i)
            assign(value, 'processing', normalized['processing'])
            value['synthesis_tests'] = normalized['synthesis_tests']
            if issue:
                assign(value, 'planning', saved.get('planning', normalized['processing']))
                # Old workflows derived implementation from processing. Give
                # that saved choice its own name; fresh defaults do not apply.
                assign(value, 'fix', saved.get('fix', normalized['processing']))
            else:
                value['suggested_changes'] = normalized['suggested_changes']
    if issue and owner:
        from .owner_state import effective_settings
        effective = effective_settings(owner)
        final = effective['final_review']
        for i, choice in enumerate(final['reviewer_slots']):
            assign(value, 'reviewers', choice, i)
        value['reviewers'] = value['reviewers'][:len(final['reviewer_slots'])]
        assign(value, 'processing', final['synthesis'])
        assign(value, 'fix', effective['fix'])
        value['synthesis_tests'] = final['synthesis_tests']
    if issue and resources:
        from .issue_resources import duration_text
        for key in LIMITS:
            v = resources.get(key)
            value[key] = duration_text(v) if key == 'max_runtime' and v is not None else v
    explicit = set()
    if '_cli_file' in request:
        value = copy.deepcopy(request['_cli_file'])
        explicit.update(value['model_configs'])
    for role in ('reviewers', 'processing', 'planning', 'fix') if issue else ('reviewers', 'processing'):
        if 'cli_' + role in request:
            value[role] = request['cli_' + role]
    # New reviewer/processing names use the preset, just like the built-in names.
    # Planning/fix-only names retain independent defaults. Existing definitions
    # (including saved/file choices and intentional sharing) keep their values.
    review_names = set(value['reviewers']) | {value['processing']}
    review_defaults = settings.initial_settings(request)
    for name in references(value, issue=issue):
        if name not in value['model_configs']:
            value['model_configs'][name] = default_choice(name, review_defaults if name in review_names else None)
    if not saved and not owner and '_cli_file' not in request and 'cli_processing' not in request and len(value['reviewers']) == 1:
        # Keep an independent processing reference with this provider's defaults.
        p = provider(value['reviewers'][0])
        name = value['processing']
        others = set(value['reviewers']) | {value[r] for r in ('planning', 'fix') if r in value}
        if provider(name) != p or name in others:
            n = 1
            while f'{p}-{n}' in value['model_configs']:
                n += 1
            name = f'{p}-{n}'
            value['model_configs'][name] = default_choice(name, review_defaults)
        value['processing'] = name
    for key, v in request.items():
        if key.startswith('named_'):
            name, field = key[6:].rsplit('-', 1)
            explicit.add(name)
            value['model_configs'].setdefault(name, default_choice(name))[field] = v
    for key in ('synthesis_tests', *LIMITS) if issue else ('synthesis_tests', 'suggested_changes'):
        if key in request:
            value[key] = request[key]
    validate(value, issue=issue, explicit=explicit)
    active = set(references(value, issue=issue))
    value['model_configs'] = {n: c for n, c in value['model_configs'].items() if n in active}
    configs = value['model_configs']
    result = settings.validate({**(saved or settings.initial_settings(request)),
        'reviewer_slots': [{'id': f'reviewer-{i}', **configs[n]} for i, n in enumerate(value['reviewers'], 1)],
        'processing': configs[value['processing']], 'synthesis_tests': value['synthesis_tests'],
        'suggested_changes': value.get('suggested_changes', False)})
    result['_named'] = value
    if issue:
        result.update({r: dict(configs[value[r]]) for r in ('planning', 'fix')})
    return result


def effective(configuration):
    """Ignore names and obsolete provider defaults for frozen-settings checks."""
    return {k: configuration[k] for k in ('reviewer_slots', 'processing', 'planning', 'fix', 'synthesis_tests', 'suggested_changes') if k in configuration}


def validate_models(configuration, catalogs=None, *, reviewers=True):
    """Validate active choices using metadata only, before any effective summary."""
    from . import models
    choices = [(slot['id'], slot) for slot in configuration['reviewer_slots']] if reviewers else []
    choices += [(role, configuration[role]) for role in ('processing', 'planning', 'fix') if role in configuration]
    if catalogs is None:
        catalogs = {p: models.discover({'provider': p}) for p in sorted({c['provider'] for _, c in choices})}
    for role, choice in choices:
        try:
            models.validate_selection({'processing': choice}, catalogs, reviewers=False)
        except ApiError as exc:
            raise ApiError(exc.code, f'{role}: {exc}') from exc
    return catalogs


def summary(configuration, runtime, *, request=None, resources=None):
    request = request or {}
    named = configuration['_named']
    configs = named['model_configs']
    followup = bool(request.get('followup_threads'))
    rows = [] if followup else [(f'reviewer-{i}', n) for i, n in enumerate(named['reviewers'], 1)]
    rows += [(r, named[r]) for r in ('processing', 'planning', 'fix') if r in named]
    lines = ['Effective configuration:']
    for role, name in rows:
        c = configs[name]
        lines.append(f"  {role}: {name} = {c['provider']} / {c['model']} / {c['effort']}")
    if followup:
        lines.append('  Follow-up tests: focused')
    else:
        lines += ['  Independent review tests: focused (60s per command, 300s cumulative; no full suite)',
                  '  Synthesis tests: ' + named['synthesis_tests']]
    if 'suggested_changes' in named and not followup:
        lines.append('  Suggested changes: ' + str(named['suggested_changes']).lower())
    if 'planning' in named:
        lines.append('  Approval: ' + ('yolo' if request.get('yolo') else 'explicit revision ' + str(request['approve']) if request.get('approve') else 'required'))
        for key in LIMITS:
            lines.append(f'  {key}: {named[key] if named[key] is not None else "disabled (unlimited)"}')
        lines.append(f'  Consumed active runtime: {(resources or {}).get("active_seconds", 0):g}s')
    active = {configs[name]['provider'] for _, name in rows}
    lines += [f'  Model timeout: {runtime.model_timeout or "unlimited"}', f'  Authentication timeout: {runtime.auth_timeout}s']
    if 'claude' in active:
        lines += [f'  Claude permission: {runtime.permission}; sandbox: disabled',
                  f'  Claude budget: {runtime.budget or "unlimited"}', '  Claude authentication probe: haiku / low / dontAsk']
    if 'codex' in active:
        lines += ['  Codex sandbox: danger-full-access; approval: never',
                  f'  Codex authentication probe: {runtime.probe_model} / low']
    print('\n'.join(lines), file=sys.stderr, flush=True)
