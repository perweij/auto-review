"""Evidence required before a standalone Owner review can authorize more work."""
import json

from .common import ApiError

POLICY = (
    "Use repair_scope as the original authority. Authorize a repair only for selected feedback, "
    "an explicit original PR or approved-plan requirement, or a regression in established supported use. "
    "For each actionable blocker supply exactly one repair_basis entry. In task checks, use an empty "
    "array for incomplete verification, including environmental limitations. Use blocker (finding ID in synthesis, exact "
    "blocking string in task review), kind, reference (feedback key, pr, plan, or full comparison revision), "
    "source (quote the original PR/plan for a requirement, or identify the specific caller or feedback), "
    "scenario and observed or source-traced impact. "
    "A new test, implementation choice or documentation promise made during repair cannot authorize itself. "
    "Report optional improvements as nonblocking observations. Use a clarification when a material consequence "
    "requires a product decision. If a necessary original reference is unavailable, identify the gap. "
    "Consider simplifying the repair that created the obligation."
)


def contract(batch):
    return batch.get('repair_scope', {'version': 1, 'baseline': batch['baseline']})


def remember_observations(batch, report):
    # A subsequent passing review need not repeat an earlier optional finding.
    if report.get('observations'):
        earlier = batch.get('scope_observations', [])
        batch['scope_observations'] = list(dict.fromkeys([*earlier, *report['observations']]))


def extend_schema(schema):
    text = {'type': 'string'}
    fields = {k: text for k in ('blocker', 'reference', 'source', 'scenario', 'impact')}
    fields['kind'] = {'type': 'string', 'enum': ['feedback', 'requirement', 'regression']}
    schema['properties']['repair_basis'] = {'type': 'array', 'items': {
        'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}}
    schema['required'].append('repair_basis')
    return schema


def validate(rows, blockers, scope, keys):
    def invalid(message):
        raise ApiError('invalid_result', 'Invalid repair_basis: ' + message, repair_scope=True)

    if not isinstance(blockers, list) or not all(isinstance(b, str) for b in blockers):
        invalid('expected a list of blocker identifiers')
    if not isinstance(rows, list) or len(rows) != len(blockers):
        invalid('supply one entry for each blocker')
    references = {'feedback': set(keys),
                  'regression': {scope.get(k) for k in ('baseline', 'base', 'head')},
                  'requirement': {k for k in ('pr', 'plan') if scope.get(k)}}
    seen = set()
    for row in rows:
        fields = ('kind', 'blocker', 'reference', 'source', 'scenario', 'impact')
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in fields):
            invalid('include the basis kind, blocker, reference, source, scenario and impact')
        if row['reference'] not in references.get(row['kind'], set()) or row['blocker'] in seen:
            invalid(f"unknown reference or duplicate blocker for {row['blocker']}")
        if row['kind'] == 'requirement':
            original = scope[row['reference']]
            text = original if isinstance(original, str) else json.dumps(original, ensure_ascii=False)
            if row['source'] not in text:
                invalid('quote the original requirement in source')
        seen.add(row['blocker'])
    if seen != set(blockers):
        invalid('entries must identify exactly the blocking findings')
