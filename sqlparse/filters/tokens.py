#
# Copyright (C) 2009-2020 the sqlparse authors and contributors
# <see AUTHORS file>
#
# This module is part of python-sqlparse and is released under
# the BSD License: https://opensource.org/licenses/BSD-3-Clause

from sqlparse import tokens as T


def _unpack(item):
    """Split a stream item into ``(ttype, value, protected)``.

    Regular streams yield ``(ttype, value)`` pairs; a
    :class:`~sqlparse.protected.ProtectedStreamFilter` yields triples.
    """
    ttype, value = item[0], item[1]
    is_protected = len(item) > 2 and item[2]
    return ttype, value, is_protected


class _CaseFilter:
    ttype = None

    def __init__(self, case=None):
        case = case or 'upper'
        self.convert = getattr(str, case)

    def process(self, stream):
        for item in stream:
            ttype, value, is_protected = _unpack(item)
            if not is_protected and ttype in self.ttype:
                value = self.convert(value)
            yield ttype, value, is_protected


class KeywordCaseFilter(_CaseFilter):
    ttype = T.Keyword


class IdentifierCaseFilter(_CaseFilter):
    ttype = T.Name, T.String.Symbol

    def process(self, stream):
        for item in stream:
            ttype, value, is_protected = _unpack(item)
            if (not is_protected and ttype in self.ttype
                    and value.strip()[0] != '"'):
                value = self.convert(value)
            yield ttype, value, is_protected


class TruncateStringFilter:
    def __init__(self, width, char):
        self.width = width
        self.char = char

    def process(self, stream):
        for item in stream:
            ttype, value, is_protected = _unpack(item)
            if ttype != T.Literal.String.Single or is_protected:
                yield ttype, value, is_protected
                continue

            if value[:2] == "''":
                inner = value[2:-2]
                quote = "''"
            else:
                inner = value[1:-1]
                quote = "'"

            if len(inner) > self.width:
                value = ''.join((quote, inner[:self.width], self.char, quote))
            yield ttype, value, is_protected
