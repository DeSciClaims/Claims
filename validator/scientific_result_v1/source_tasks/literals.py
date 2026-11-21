"""Lossless JSON index: RFC 6901 pointers and exact original value literals."""

from __future__ import annotations
from decimal import Decimal
import json


def escape(key):
    return str(key).replace("~", "~0").replace("/", "~1")


def exact_decimal(literal):
    value = Decimal(literal)
    # JSON Schema's integer type is mathematical; 2.0 is an integer. The
    # original literal remains in spans, including exponent/trailing zeroes.
    if value.is_finite() and value.adjusted() < 1000 and value == value.to_integral_value():
        return int(value)
    return value


class LiteralDocument:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.text = raw.decode("utf-8", errors="strict")
        self.spans = {}
        self.decoder = json.JSONDecoder(
            parse_float=exact_decimal,
            parse_int=int,
            parse_constant=lambda s: (_ for _ in ()).throw(
                ValueError("Nonfinite JSON")
            ),
        )
        self.value, end = self._read(0, "")
        if self.text[end:].strip():
            raise ValueError("Trailing JSON data")

    def _space(self, i):
        while i < len(self.text) and self.text[i] in " \n\r\t":
            i += 1
        return i

    def _read(self, i, pointer):
        i = self._space(i)
        start = i
        if i >= len(self.text):
            raise ValueError("Missing JSON value")
        c = self.text[i]
        if c in "{[":
            obj = {} if c == "{" else []
            i = self._space(i + 1)
            close = "}" if c == "{" else "]"
            if i < len(self.text) and self.text[i] == close:
                i += 1
            else:
                while True:
                    if c == "{":
                        key, j = self.decoder.raw_decode(self.text, i)
                        if not isinstance(key, str) or key in obj:
                            raise ValueError("Invalid or duplicate JSON key")
                        key.encode("utf-8")
                        i = self._space(j)
                        if self.text[i : i + 1] != ":":
                            raise ValueError("Missing colon")
                        val, i = self._read(i + 1, pointer + "/" + escape(key))
                        obj[key] = val
                    else:
                        val, i = self._read(i, pointer + "/" + str(len(obj)))
                        obj.append(val)
                    i = self._space(i)
                    if self.text[i : i + 1] == close:
                        i += 1
                        break
                    if self.text[i : i + 1] != ",":
                        raise ValueError("Missing comma")
                    i = self._space(i + 1)
        else:
            obj, i = self.decoder.raw_decode(self.text, i)
            if isinstance(obj, str):
                obj.encode("utf-8")
            if isinstance(obj, Decimal) and not obj.is_finite():
                raise ValueError("Nonfinite JSON")
        self.spans[pointer] = (start, i)
        return obj, i

    def literal(self, pointer):
        a, b = self.spans[pointer]
        return self.text[a:b]

    def resolve(self, pointer):
        if pointer not in self.spans:
            raise ValueError("Unknown original JSON pointer")
        value = self.value
        for token in pointer.split("/")[1:]:
            token = token.replace("~1", "/").replace("~0", "~")
            value = value[int(token)] if isinstance(value, list) else value[token]
        return value

    def nodes(self):
        return [
            (p, self.resolve(p))
            for p in sorted(self.spans, key=lambda s: s.encode("utf-8"))
        ]
