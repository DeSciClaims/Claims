"""Install a separately attested reader while preserving a frozen judge runtime.

Only the named experimental advisory round may use this HOLD compatibility
reader. It never changes the generating runtime, receipt bytes or numeric scores.
"""
import hashlib
import importlib.util
import json
from pathlib import Path


def install(*, directory, manifest, round_id):
    from competition import scientific_lineage
    from competition.scientific_first_round import configured_policy
    from validator.scientific_result_v1.source_tasks.budgeted_validation import profile
    from validator.scientific_result_v1.source_tasks.contracts import SampledSilverReceipt

    directory = Path(directory)
    expected = json.loads(Path(manifest).read_text())
    modules = {}
    for filename in ['errors.py', 'replay.py']:
        path = directory / filename
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected[filename]
        name = 'validator.scientific_result_v1.source_tasks.compatibility_reader_' + path.stem
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[filename] = module
    original = scientific_lineage.replay
    registry = modules['errors.py']
    reader = modules['replay.py'].replay

    def replay(store, ref):
        receipt = store.load(ref, SampledSilverReceipt)
        if not any(error.code == registry.BUDGETED_HOLD_CODE for error in receipt.hold_reasons):
            return original(store, ref)
        if receipt.binding.round_id != round_id or receipt.state != 'HOLD':
            raise ValueError('Experimental replay reader is outside its authorized HOLD scope')
        backend = getattr(store, 'backend', None)
        if backend is None or not callable(getattr(backend, 'get_round', None)):
            raise ValueError('Experimental replay reader requires current round authorization')
        record = backend.get_round(round_id)
        configuration = record['rules_public']['configuration']
        scoring = configuration['scientific_scoring']
        if (configured_policy(configuration, round_id) is None
            or scoring.get('experimental_launch') is None
            or receipt.binding.policy_sha256 != record['configuration_sha256']):
            raise ValueError('Experimental replay reader requires the approved advisory policy')
        profile(scoring.get('optimized_validation'))
        return reader(store, ref, error_registry=registry)

    scientific_lineage.replay = replay
    return {'round_id': round_id, 'reader_files': expected, 'frozen_generation_runtime_preserved': True}
