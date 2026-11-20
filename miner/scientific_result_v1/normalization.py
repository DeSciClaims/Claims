"""Schema-guided omission of invalid optional absence markers, never scientific data."""
from copy import deepcopy

from jsonschema import Draft202012Validator


def normalize_optional_absence(value: dict, schema: dict) -> tuple[dict, list[dict]]:
    result = deepcopy(value)
    validator = Draft202012Validator(schema)
    changes = []
    def errors(document):
        return list(validator.iter_errors(document))
    def signature(error):
        return (tuple(error.absolute_path), error.validator, error.message)
    while True:
        current = errors(result)
        before = {signature(error) for error in current}
        changed = False
        for error in current:
            path = list(error.absolute_path)
            if (not path or not isinstance(path[-1], str) or path[-1] in {'id', 'value'}
                    or error.validator not in {'type', 'minItems', 'minProperties', 'minLength'}):
                continue
            parent = result
            for key in path[:-1]:
                parent = parent[key]
            if not isinstance(parent, dict):
                continue
            old = parent[path[-1]]
            if old is not None and old != [] and old != {} and old != '':
                continue
            trial = deepcopy(result)
            target = trial
            for key in path[:-1]:
                target = target[key]
            del target[path[-1]]
            # A required property, conditional requirement, or scientific invariant
            # would introduce another error. In that case leave it for the model.
            after = {signature(item) for item in errors(trial)}
            if not after < before:
                continue
            changes.append({'path': path, 'omitted_absence': old, 'reason': error.message})
            result = trial
            changed = True
            break
        if not changed:
            return result, changes
