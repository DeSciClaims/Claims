"""Credential-free identities of every effective scientific scoring component."""
from pathlib import Path
from .store import canonical,sha
from .packages import SCHEMA_HASH
from ..engine import implementation_hashes


def runtime_descriptor():
    root=Path(__file__).parent
    competition=root.parents[2]/'competition'
    return {'implementation_digest':sha(canonical({'validator':implementation_hashes(),
                'competition':{p.name:sha(p.read_bytes()) for p in competition.glob('*.py')}})),
            'prompt_bundle_digest':sha(canonical({p.name:sha(p.read_bytes()) for p in root.glob('*.txt')})),
            'renderer_digest':sha((root/'packets.py').read_bytes()),
            'source_parser_digest':sha(canonical({name:sha((root/name).read_bytes()) for name in ('parsing.py','docling_frame.py')})),
            'normalization_digest':sha((root/'literals.py').read_bytes()),
            'extraction_schema_sha256':SCHEMA_HASH}
