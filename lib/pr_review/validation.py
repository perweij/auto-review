"""Semantic validation of model artifacts (schema shape alone is insufficient)."""
import re
from .common import ApiError


def text(value):
    return isinstance(value, str) and bool(value.strip())


def integer(value, minimum=0):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= minimum and int(value) == value


def location(value):
    return (isinstance(value, dict) and set(value) == {'path', 'side', 'line', 'start_line'}
            and text(value['path']) and value['side'] in ('LEFT', 'RIGHT')
            and integer(value['line'], 1) and integer(value['start_line'], 1)
            and value['start_line'] <= value['line'])


def synthesis(value, *, new=False):
    valid = isinstance(value, dict) and set(value) == {'recommendation', 'recommendation_rationale', 'blocking_findings', 'report_md', 'review_body_md', 'findings'}
    if valid:
        valid = (value['recommendation'] in ('COMMENT', 'REQUEST_CHANGES', 'APPROVE')
                 and integer(value['blocking_findings']) and all(text(value[k]) for k in ('recommendation_rationale', 'report_md', 'review_body_md'))
                 and isinstance(value['findings'], list))
    ids = []
    if valid:
        for finding in value['findings']:
            if not isinstance(finding, dict):
                valid = False
                break
            ids.append(finding.get('id'))
            valid = (set(finding) - {'locations', 'primary_location', 'severity'} == {'id', 'title', 'validity', 'evidence', 'blocking', 'body_md'}
                     and isinstance(finding['id'], str) and re.fullmatch(r'[A-Za-z0-9_-]+', finding['id'])
                     and all(text(finding[k]) for k in ('title', 'evidence', 'body_md'))
                     and finding['validity'] in ('CONFIRMED', 'MODIFIED', 'NEW', 'UNVERIFIED', 'REJECTED')
                     and isinstance(finding['blocking'], bool) and not (finding['validity'] == 'REJECTED' and finding['blocking'])
                     and (finding.get('severity') in ('Critical', 'High', 'Medium', 'Low') if 'severity' in finding else not new)
                     and (finding.get('primary_location') is None or location(finding['primary_location']))
                     and ('locations' not in finding or isinstance(finding['locations'], list) and all(map(location, finding['locations']))))
            if not valid:
                break
    if not valid or len(ids) != len(set(ids)) or value['blocking_findings'] != sum(f['blocking'] for f in value['findings']):
        raise ApiError('invalid_artifact', 'Synthesis violates the JSON schema; resume to retry')
    return value


def contributions(value, findings, discussion):
    eligible = {f['id']: f for f in findings['findings'] if f['validity'] != 'REJECTED'}
    items = {i['key']: i for i in discussion['items']}
    valid = isinstance(value, dict) and set(value) == {'contributions', 'validity_conflicts'} and all(isinstance(value[k], list) for k in value)
    covered = []
    if valid:
        for c in value['contributions']:
            if not isinstance(c, dict) or set(c) != {'finding_ids', 'classification', 'discussion_keys', 'target_key', 'explanation', 'delta', 'body_md'}:
                valid = False
                break
            ids, keys, kind, target = c['finding_ids'], c['discussion_keys'], c['classification'], c['target_key']
            valid = (isinstance(ids, list) and bool(ids) and all(isinstance(i, str) and i in eligible for i in ids)
                     and isinstance(keys, list) and all(isinstance(k, str) and k in items for k in keys) and len(keys) == len(set(keys))
                     and kind in ('NEW', 'ADDITION', 'REVISIT', 'DUPLICATE', 'UNCERTAIN') and text(c['explanation'])
                     and isinstance(c['delta'], str) and isinstance(c['body_md'], str)
                     and (c['delta'] == c['body_md'] == '' if kind == 'DUPLICATE' else text(c['body_md']))
                     and (kind != 'NEW' or target is None and not keys)
                     and (kind not in ('ADDITION', 'REVISIT', 'DUPLICATE') or bool(keys))
                     and (kind not in ('ADDITION', 'REVISIT') or text(c['delta']))
                     and ((kind not in ('ADDITION', 'REVISIT') or not any(k.startswith('thread:') for k in keys)) if target is None
                          else isinstance(target, str) and target in keys and items[target]['kind'] == 'thread')
                     and (kind == 'UNCERTAIN' or not any(eligible[i]['validity'] == 'UNVERIFIED' for i in ids)))
            if not valid:
                break
            covered.extend(ids)
        if valid:
            for c in value['validity_conflicts']:
                valid = (isinstance(c, dict) and set(c) == {'evidence_keys', 'finding_id', 'reason'}
                         and isinstance(c['finding_id'], str) and c['finding_id'] in eligible and text(c['reason'])
                         and isinstance(c['evidence_keys'], list) and bool(c['evidence_keys'])
                         and all(isinstance(k, str) and k in items for k in c['evidence_keys']))
                if not valid:
                    break
    if not valid or sorted(covered) != sorted(eligible):
        raise ApiError('invalid_artifact', 'Invalid contribution schema, finding coverage, or discussion destination')
    return value


def merge_contributions(old, model, discussion, actions, discussion_hash, findings_hash, url):
    ledger = dict(format_version=1, discussion_hash=discussion_hash, findings_hash=findings_hash,
                  next_id=old.get('next_id', 1), proposals=[], main_signature=old.get('main_signature'), main_generated_hash=old.get('main_generated_hash'))
    for candidate in model['contributions']:
        basis = {**candidate, 'finding_ids': sorted(candidate['finding_ids']), 'discussion_keys': sorted(candidate['discussion_keys'])}
        prior = next((p for p in old.get('proposals', []) if p['basis']['finding_ids'] == basis['finding_ids'] and p['basis']['target_key'] == basis['target_key']), None)
        matched = [i for i in discussion['items'] if i['key'] in basis['discussion_keys']]
        target = next((i for i in matched if i['key'] == basis['target_key']), None)
        kind = basis['classification']
        if kind not in ('DUPLICATE', 'UNCERTAIN') and any(i.get('is_resolved') for i in matched):
            kind = 'REVISIT'
        published = any(a['status'] == 'published' and set(a.get('finding_ids', [])) & set(basis['finding_ids']) for a in actions)
        available = target is None or target.get('can_reply') is True
        identity = prior['id'] if prior else 'p' + str(ledger['next_id'])
        if prior is None:
            ledger['next_id'] += 1
        ledger['proposals'].append(dict(id=identity, basis=basis, classification=kind, published=published,
            destination=dict(kind='reply', key=target['key'], root_id=target['root_id'], url=target['url']) if target else dict(kind='review', key=None, root_id=None, url=url),
            available=available, selected=False,
            body_md=prior['body_md'] if prior and prior['basis'] == basis else basis['body_md'],
            references=[dict(key=i['key'], url=i.get('url') or i['comments'][0]['url']) for i in matched]))
    return ledger
