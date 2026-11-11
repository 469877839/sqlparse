#
# Copyright (C) 2009-2020 the sqlparse authors and contributors
# <see AUTHORS file>
#
# This module is part of python-sqlparse and is released under
# the BSD License: https://opensource.org/licenses/BSD-3-Clause

"""filter"""

from sqlparse import lexer
from sqlparse import tokens as T
from sqlparse.engine import grouping
from sqlparse.engine.postgresql import (
    PostgreSQLScriptSplitter, coerce_text, normalize_dialect)
from sqlparse.engine.statement_splitter import StatementSplitter
from sqlparse.exceptions import SQLParseError
from sqlparse.filters import StripTrailingSemicolonFilter


class FilterStack:
    def __init__(self, strip_semicolon=False, dialect=None):
        self.preprocess = []
        self.stmtprocess = []
        self.postprocess = []
        self._grouping = False
        self.dialect = normalize_dialect(dialect)
        if strip_semicolon:
            self.stmtprocess.append(StripTrailingSemicolonFilter())

    def enable_grouping(self):
        self._grouping = True

    def run(self, sql, encoding=None):
        try:
            if self.dialect == 'postgresql':
                stream = self._run_postgresql(sql, encoding)
            else:
                stream = self._run_default(sql, encoding)

            # Output: Stream processed Statements
            for stmt in stream:
                if is_copy_block(stmt):
                    # Raw COPY data must not be grouped, filtered or
                    # reformatted: it is not SQL and has to survive
                    # byte for byte, including its line endings.
                    yield stmt
                    continue

                if self._grouping:
                    stmt = grouping.group(stmt)

                for filter_ in self.stmtprocess:
                    filter_.process(stmt)

                for filter_ in self.postprocess:
                    stmt = filter_.process(stmt)

                yield stmt
        except RecursionError as err:
            raise SQLParseError('Maximum recursion depth exceeded') from err

    def _run_default(self, sql, encoding=None):
        stream = lexer.tokenize(sql, encoding)
        # Process token stream
        for filter_ in self.preprocess:
            stream = filter_.process(stream)
        yield from StatementSplitter().process(stream)

    def _run_postgresql(self, sql, encoding=None):
        yield from PostgreSQLScriptSplitter().process(
            coerce_text(sql, encoding))


def is_copy_block(stmt):
    """Return True for statements that wrap a raw COPY data block."""
    return any(token.ttype is T.Other for token in stmt.flatten())
