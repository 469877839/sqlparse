#
# Copyright (C) 2009-2020 the sqlparse authors and contributors
# <see AUTHORS file>
#
# This module is part of python-sqlparse and is released under
# the BSD License: https://opensource.org/licenses/BSD-3-Clause

"""Support for ``fmt: off`` / ``fmt: on`` protection markers.

External systems sometimes own fragments of a larger SQL script and rely on
their text being emitted byte for byte.  A standalone line comment containing
``fmt: off`` opens a protected region; a standalone line comment containing
``fmt: on`` closes it again.  Everything between the two markers, the markers
included, is kept verbatim while the rest of the document keeps following the
regular formatting options.

Only *standalone line comments* act as markers.  The same words inside a
string literal or a block comment have no effect because the lexer never
classifies them as single line comments.

Semantics:

* A marker comment occupies a line on its own: between the previous line
  break and the comment there may only be horizontal whitespace.  The
  comment token runs to the end of its line and includes that trailing
  line break, so the region covers the break of its ``fmt: on`` marker;
  the following line starts outside the region.
* ``fmt: off`` while a region is already open nests: it is remembered but
  does not start a second region, so the ``fmt: on`` that balances the
  outermost opener closes the region.  Inner ``fmt: on`` markers only leave
  nesting and never close the outer region early.
* An ``fmt: on`` without a pending opener is an ordinary comment and has no
  effect.
* An ``fmt: off`` without a matching ``fmt: on`` leaves the region open until
  the end of the input rather than raising.
* Regions may span several statements.  Marking works on the token stream of
  the whole document, so parentheses and statement context stay intact
  across a region.
"""

from sqlparse import lexer
from sqlparse import tokens as T

#: Directive text accepted after stripping the comment prefix and whitespace.
FMT_OFF = 'fmt: off'
FMT_ON = 'fmt: on'

_HORIZONTAL_WS = ' \t'


def _directive(token_type, value):
    """Return ``FMT_OFF``/``FMT_ON`` for a marker comment, else ``None``."""
    if token_type not in (T.Comment.Single, T.Comment.Single.Hint):
        return None
    stripped = value.lstrip()
    payload = stripped
    for prefix in ('--', '#'):
        if stripped.startswith(prefix):
            payload = stripped[len(prefix):]
            break
    # The lexer keeps the trailing line break as part of the comment token;
    # it must not participate in the directive comparison.
    payload = payload.rstrip('\r\n').strip()
    if payload == FMT_OFF:
        return FMT_OFF
    if payload == FMT_ON:
        return FMT_ON
    return None


def _is_standalone(text, start, end):
    """Check that the comment at ``[start, end)`` is alone on its line.

    Only horizontal whitespace may precede the comment on its line.  A line
    comment always runs to the end of its line (the lexer includes the
    trailing line break in the token), so nothing but a line break or EOF
    can follow it.
    """
    line_start = max(text.rfind('\n', 0, start),
                     text.rfind('\r', 0, start)) + 1
    if text[line_start:start].strip(_HORIZONTAL_WS):
        return False
    rest = text[end:]
    return (not rest
            or text[end - 1] in '\r\n'
            or rest[0] in '\r\n')


def scan_regions(text):
    """Return the protected regions of *text* as half-open offset triples.

    Each result is ``(start, end, balanced)`` where ``balanced`` is ``False``
    for an opener that never reaches an ``fmt: on`` (the region then runs to
    the end of *text*).
    """
    if FMT_OFF not in text and FMT_ON not in text:
        # Fast path: no directive text at all, so the extra tokenization
        # pass over the whole document can be skipped.
        return []

    regions = []
    start = None
    nesting = 0
    offset = 0
    for token_type, value in lexer.tokenize(text):
        end = offset + len(value)
        directive = _directive(token_type, value)
        if directive is not None and _is_standalone(text, offset, end):
            if directive == FMT_OFF:
                if start is None:
                    start = offset
                    nesting = 1
                else:
                    nesting += 1
            elif start is not None:
                nesting -= 1
                if nesting == 0:
                    regions.append((start, end, True))
                    start = None
        offset = end
    if start is not None:
        regions.append((start, len(text), False))
    return regions


class ProtectedStreamFilter:
    """Annotate the token stream with protected-region membership.

    Items are yielded as ``(ttype, value, protected)`` triples.  Downstream
    preprocess filters keep the third element and leave protected tokens
    untouched; the statement splitter discards it again when building
    :class:`~sqlparse.sql.Token` instances.
    """

    def __init__(self, text):
        self._bounds = [
            (start, end) for start, end, _ in scan_regions(text)]
        self.has_regions = bool(self._bounds)

    def process(self, stream):
        if not self.has_regions:
            for ttype, value in stream:
                yield ttype, value, False
            return

        bounds = iter(self._bounds)
        region_start, region_end = next(bounds)
        offset = 0
        for ttype, value in stream:
            end = offset + len(value)
            while offset >= region_end:
                try:
                    region_start, region_end = next(bounds)
                except StopIteration:
                    region_start = None
                    break
            protected = (region_start is not None
                         and region_start <= offset and end <= region_end)
            yield ttype, value, protected
            offset = end
