"""An MSB-first bit reader with the variable-length integers of TS 102 366 and TS 103 420.

The bits are held as one Python integer, so a read of any width is a shift and
a mask instead of a loop over single bits.
"""

from __future__ import annotations


class BitstreamError(ValueError):
    """The bits do not form the structure being parsed."""


class BitReader:
    __slots__ = ("_limit", "_value", "end", "pos")

    def __init__(self, data: bytes, bit_pos: int = 0, bit_end: int | None = None) -> None:
        end = len(data) * 8 if bit_end is None else bit_end
        first, last = bit_pos >> 3, (end + 7) >> 3
        # positions stay absolute in `data`; only the bytes in [pos, end) are converted
        self._value = int.from_bytes(data[first:last], "big")
        self._limit = last * 8
        self.pos = bit_pos
        self.end = end

    def read(self, n: int) -> int:
        new = self.pos + n
        if new > self.end:
            raise BitstreamError("read past the end of the field")
        self.pos = new
        return (self._value >> (self._limit - new)) & ((1 << n) - 1)

    def peek(self, n: int) -> int:
        """The next n bits without consuming them, zero past the end of the data."""
        shift = self._limit - self.pos - n
        if shift >= 0:
            return (self._value >> shift) & ((1 << n) - 1)
        return (self._value << -shift) & ((1 << n) - 1)

    def flag(self) -> bool:
        return bool(self.read(1))

    def signed(self, n: int) -> int:
        """An n-bit two's complement integer."""
        value = self.read(n)
        return value - (1 << n) if value >> (n - 1) else value

    def skip(self, n: int) -> None:
        self.pos += n

    def read_bytes(self, count: int) -> bytes:
        return self.read(count * 8).to_bytes(count, "big")

    def variable_bits(self, n: int) -> int:
        """TS 102 366 clause H.2.1.2.1: groups of n bits, each followed by a read_more flag."""
        value = 0
        while True:
            value += self.read(n)
            if not self.flag():
                return value
            value = (value << n) + (1 << n)

    def variable_bits_max(self, n: int, max_groups: int) -> int:
        """TS 103 420 clause 5.5.1: variable_bits capped at `max_groups` groups."""
        value = self.read(n)
        groups = 1
        more = self.flag()
        if max_groups > groups:
            if more:
                value = (value << n) + (1 << n)
            while more:
                value += self.read(n)
                more = self.flag()
                if groups >= max_groups:
                    break
                if more:
                    value = (value << n) + (1 << n)
                    groups += 1
        return value


class Huffman:
    """A TS 103 420 Annex A.1 code tree with a lookup table for its first `width` bits.

    A tree entry is the next node, or `-value - 1` at a leaf.  Codes no longer
    than the table resolve in one lookup; longer ones continue bit by bit.
    """

    __slots__ = ("_table", "_tree", "_width")

    def __init__(self, tree: tuple[tuple[int, int], ...], width: int = 8) -> None:
        self._tree, self._width = tree, width
        self._table = [self._walk(prefix) for prefix in range(1 << width)]

    def _walk(self, prefix: int) -> tuple[bool, int, int]:
        node = 0
        for used in range(1, self._width + 1):
            node = self._tree[node][(prefix >> (self._width - used)) & 1]
            if node <= 0:
                return True, -node - 1, used
        return False, node, self._width

    def decode(self, bits: BitReader) -> int:
        leaf, value, used = self._table[bits.peek(self._width)]
        bits.skip(used)
        if bits.pos > bits.end:
            raise BitstreamError("read past the end of the field")
        node = value
        while not leaf:
            node = self._tree[node][bits.read(1)]
            leaf, value = node <= 0, -node - 1
        return value

    def decode_run(self, bits: BitReader, count: int) -> list[int]:
        """`count` consecutive codes; `decode` unrolled over the reader's state."""
        table, tree, width = self._table, self._tree, self._width
        value, limit, pos = bits._value, bits._limit, bits.pos
        mask = (1 << width) - 1
        out = []
        for _ in range(count):
            shift = limit - pos - width
            peek = (value >> shift if shift >= 0 else value << -shift) & mask
            leaf, symbol, used = table[peek]
            pos += used
            while not leaf:
                if pos >= limit:
                    raise BitstreamError("read past the end of the field")
                node = tree[symbol][(value >> (limit - pos - 1)) & 1]
                pos += 1
                leaf, symbol = node <= 0, (-node - 1 if node <= 0 else node)
            out.append(symbol)
        if pos > bits.end:
            raise BitstreamError("read past the end of the field")
        bits.pos = pos
        return out
