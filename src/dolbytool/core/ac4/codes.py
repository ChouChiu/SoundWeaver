"""The Huffman codebooks of the AC-4 tables, as decoders over a BitReader."""

from __future__ import annotations

from functools import cache

from dolbytool.core.atmos.bitstream import Huffman

from .tables import CODEBOOKS


def tree(lengths: tuple[int, ...], codewords: tuple[int, ...]) -> tuple[tuple[int, int], ...]:
    """The code tree of a codebook: a node is its two children, a leaf `-value - 1`."""
    nodes: list[list[int | None]] = [[None, None]]
    for value, (length, code) in enumerate(zip(lengths, codewords, strict=True)):
        node = 0
        for i in range(length - 1, -1, -1):
            bit = (code >> i) & 1
            if i == 0:
                nodes[node][bit] = -value - 1
            else:
                child = nodes[node][bit]
                if child is None:
                    child = len(nodes)
                    nodes.append([None, None])
                    nodes[node][bit] = child
                node = child  # type: ignore[assignment]
    # an incomplete code leaves holes; they decode to value 0 and never occur in a valid stream
    return tuple(
        (left if left is not None else -1, right if right is not None else -1)
        for left, right in nodes
    )


@cache
def huffman(name: str) -> Huffman:
    return Huffman(tree(*CODEBOOKS[name]))
