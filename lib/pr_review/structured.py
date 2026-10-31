"""Structured provider responses shared by Owner and standalone issue planning."""
import json
from .common import ApiError, read_json


def response(choice, result, directory):
    if result.returncode:
        raise ApiError('model_failed', f'Provider failed; inspect {directory}.')
    if choice['provider'] == 'codex':
        if not any(e.get('type') == 'turn.completed' for e in result.events):
            raise ApiError('model_failed', 'Codex did not complete the operation.')
        value = read_json(directory / 'result.json')
    else:
        events = [e for e in result.events if e.get('type') == 'result']
        if not events or events[-1].get('is_error') or events[-1].get('subtype') != 'success':
            raise ApiError('model_failed', 'Claude did not complete the operation.')
        event = events[-1]
        try:
            value = event.get('structured_output') or json.loads(event.get('result', ''))
        except ValueError as exc:
            raise ApiError('model_failed', 'Provider returned invalid structured output.') from exc
    if not isinstance(value, dict):
        raise ApiError('model_failed', 'Provider returned no structured object.')
    return value
