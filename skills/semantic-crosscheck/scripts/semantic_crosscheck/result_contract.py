"""Structural validation of saved evidence, without adding evidence authority.

Unversioned legacy Result envelopes and explicit schema_version 1 share this
contract. Optional legacy fields may be absent; when present their types are
checked. In particular, absent timestamps still belong to freshness checks.
Unknown metadata is retained, but every check must be usable and unambiguous.
Validation does not authenticate a rewritten result or verify its claims.
"""

import json
import math

STATUSES = ('pass', 'fail', 'error', 'not_run')
SCHEMA_VERSION = 1


class ResultContractError(ValueError):
    """Never include a saved key, value, path or parser message in the error."""

    def __init__(self):
        super().__init__('invalid saved result')


def validate_result(value, expected_stage=None):
    """Return the unchanged valid envelope, or raise a sanitized ValueError."""
    if not isinstance(value, dict) or not isinstance(value.get('checks'), list):
        raise ResultContractError()
    if 'schema_version' in value and (type(value['schema_version']) is not int
                                      or value['schema_version'] != SCHEMA_VERSION):
        raise ResultContractError()
    for name in ('stage', 'next_safe_action', 'started_at', 'finished_at'):
        if name in value and not isinstance(value[name], str):
            raise ResultContractError()
    if 'stage' in value and (not value['stage'].strip()
                             or (expected_stage is not None and value['stage'] != expected_stage)):
        raise ResultContractError()
    if 'status' in value and (not isinstance(value['status'], str) or value['status'] not in STATUSES):
        raise ResultContractError()
    if 'not_proven' in value and (not isinstance(value['not_proven'], list)
                                  or any(not isinstance(item, str) for item in value['not_proven'])):
        raise ResultContractError()
    for name in ('inputs', 'provenance', 'runtime_evidence'):
        if name in value and not isinstance(value[name], dict):
            raise ResultContractError()
    seen = set()
    for check in value['checks']:
        if not isinstance(check, dict):
            raise ResultContractError()
        check_id, status = check.get('id'), check.get('status')
        if (not isinstance(check_id, str) or not check_id.strip() or check_id in seen
                or not isinstance(status, str) or status not in STATUSES):
            raise ResultContractError()
        seen.add(check_id)
        if 'detail' in check and not isinstance(check['detail'], dict):
            raise ResultContractError()
        if 'config_ref' in check and not isinstance(check['config_ref'], str):
            raise ResultContractError()
    return value


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ResultContractError()
        value[key] = item
    return value


def _constant(_text):
    raise ResultContractError()


def _number(text):
    value = float(text)
    if not math.isfinite(value):
        raise ResultContractError()
    return value


def decode_result(raw, expected_stage=None):
    """Strict JSON at the persistence boundary, preserving ordinary JSON types."""
    try:
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant, parse_float=_number)
        return validate_result(value, expected_stage)
    except (TypeError, ValueError, RecursionError):
        raise ResultContractError() from None


def invalid_result():
    """One fixed error, rather than the passing subset of an invalid artifact."""
    return {
        'checks': [{'id': 'result_file', 'status': 'error', 'config_ref': '',
                    'detail': {'reason': 'unreadable'}}],
    }
