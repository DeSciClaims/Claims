"""Pinned budget tokenizer; provider-reported usage remains separate and authoritative."""
import hashlib
import importlib.metadata
import math
from pathlib import Path
from .store import canonical,sha


class BudgetTokenizer:
    def __init__(self):
        import tiktoken
        import tiktoken._tiktoken as core
        self.encoding=tiktoken.get_encoding('cl100k_base')
        vocab=hashlib.sha256()
        for token,rank in sorted(self.encoding._mergeable_ranks.items()):
            vocab.update(len(token).to_bytes(8,'big'));vocab.update(token);vocab.update(rank.to_bytes(8,'big'))
        self.id='cl100k_base@tiktoken-'+importlib.metadata.version('tiktoken')+'+image-patch16-upper-bound-v1'
        self.implementation_sha256=sha(canonical({'source':sha(Path(__file__).read_bytes()),'binary':sha(Path(core.__file__).read_bytes()),'vocabulary':vocab.hexdigest()}))

    def __call__(self,text):return len(self.encoding.encode(text,disallowed_special=()))

    def image_tokens(self,data):
        """Conservative image budget, frozen explicitly; never silently drops source pixels."""
        import io
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            width,height=image.size
        return math.ceil(width/16)*math.ceil(height/16)+256
