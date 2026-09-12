"""Convert UniFi API payloads into deterministic Indigo dynamic states.

UniFi returns a large, evolving JSON schema.  The original plugin exposed much of
that schema directly, but dropped valid falsy values and allowed punctuation that
can produce invalid or ambiguous Indigo state identifiers.  This module keeps the
legacy flattening behavior while making the boundary deterministic and testable.
"""

import hashlib
import json
import re
from collections import Counter


_INVALID_KEY_CHARACTER = re.compile(r'[^A-Za-z0-9_]')
_SUPPORTED_SCALARS = (bool, int, float, str)


def _key_base(path):
    """Return the closest Indigo-safe equivalent of the historical flat key."""
    raw_key = '_'.join(str(part).strip() for part in path).strip()

    # Empty keys occur in arbitrary API dictionaries.  The old safeKey() indexed
    # key[0] and crashed; use a stable name so one bad API field cannot stop polling.
    if not raw_key:
        raw_key = 'state'

    # Replace rather than remove punctuation.  Removing it would make unrelated
    # names such as "tx-rate" and "txrate" collide unnecessarily.
    safe_key = _INVALID_KEY_CHARACTER.sub('_', raw_key)

    # Preserve the legacy "sk" prefix convention for keys that do not begin with a
    # letter, minimizing state-name changes for existing Indigo installations.
    if not safe_key[0].isalpha():
        safe_key = f'sk{safe_key}'
    return safe_key


def _path_digest(path):
    """Create a stable suffix from the structural path, independent of dict order."""
    encoded_path = json.dumps(path, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha1(encoded_path.encode('utf-8')).hexdigest()


def _iter_scalar_values(value, path=()):
    """Yield structural paths and supported scalar values from a JSON-like value."""
    if isinstance(value, dict):
        # Sorting makes output stable even if the controller changes dictionary order.
        for key in sorted(value, key=lambda item: str(item)):
            yield from _iter_scalar_values(value[key], path + (str(key),))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_scalar_values(item, path + (str(index),))
    elif isinstance(value, _SUPPORTED_SCALARS):
        # False, zero, 0.0, and "" are meaningful UniFi values.  Check the type
        # instead of truthiness so they remain visible and update correctly.
        yield path, value
    # None and non-JSON object types are intentionally omitted.  Indigo cannot
    # create useful stable states for them.


def flatten_states(value, prefix='', reserved_keys=()):
    """Flatten a JSON-like value into unique, deterministic Indigo state records.

    Existing legal state names remain unchanged unless they collide with another
    API path or a reserved static Indigo state.  Collisions receive a repeatable
    digest suffix derived from the full structural path, so state-list generation
    and later value updates always select the same key.
    """
    prefix_path = (prefix.rstrip('_'),) if prefix.rstrip('_') else ()
    leaves = list(_iter_scalar_values(value, prefix_path))
    bases = [_key_base(path) for path, _value in leaves]
    base_counts = Counter(bases)
    used = set(reserved_keys)
    states = []

    for (path, scalar), base in zip(leaves, bases):
        key = base
        if base_counts[base] > 1 or key in used:
            digest = _path_digest(path)
            # Eight characters are normally ample; lengthen deterministically in
            # the exceptionally unlikely event of a digest-prefix collision.
            digest_length = 8
            key = f'{base}_{digest[:digest_length]}'
            while key in used and digest_length < len(digest):
                digest_length += 2
                key = f'{base}_{digest[:digest_length]}'
            if key in used:
                # A complete SHA-1 collision is extraordinarily unlikely, but a
                # reserved key could intentionally contain the digest.  A numeric
                # fallback guarantees termination and uniqueness in either case.
                collision_index = 2
                while f'{key}_{collision_index}' in used:
                    collision_index += 1
                key = f'{key}_{collision_index}'
        used.add(key)
        states.append({'key': key, 'value': scalar})

    return states


def dict_to_states(prefix, the_dict, states_list, reserved_keys=()):
    """Compatibility wrapper used by plugin.py's existing polling code."""
    states_list.extend(flatten_states(the_dict, prefix, reserved_keys))


def list_to_states(prefix, the_list, states_list, reserved_keys=()):
    """Compatibility wrapper for callers that start with a UniFi list payload."""
    states_list.extend(flatten_states(the_list, prefix, reserved_keys))
