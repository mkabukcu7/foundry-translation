# SPDX-License-Identifier: MIT
import base64
import gzip
import hashlib
from functools import lru_cache
from pathlib import Path

import tiktoken


@lru_cache
def encoding():
    # Verified upstream MIT vocabulary is packaged so cold starts never fetch tokenizer data.
    data = gzip.decompress((Path(__file__).resolve().parents[1] / "tokenizer/cl100k_base.tiktoken.gz").read_bytes())
    if hashlib.sha256(data).hexdigest() != "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7":
        raise ValueError("Tokenizer vocabulary checksum mismatch")
    ranks = {base64.b64decode(token): int(rank) for token, rank in (line.split() for line in data.splitlines())}
    return tiktoken.Encoding(
        name="cl100k_base_local",
        pat_str=r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+",
        mergeable_ranks=ranks, special_tokens={},
    )


def chunks(text: str, size: int = 6000, overlap: int = 200) -> list[str]:
    if not 0 <= overlap < size:
        raise ValueError("Chunk overlap must be smaller than the chunk size")
    codec = encoding()
    tokens = codec.encode(text)
    return [codec.decode(tokens[start:start + size], errors="ignore")
            for start in range(0, len(tokens), size - overlap)]
