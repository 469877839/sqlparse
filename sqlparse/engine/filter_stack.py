#
# Copyright (C) 2009-2020 the sqlparse authors and contributors
# <see AUTHORS file>
#
# This module is part of python-sqlparse and is released under
# the BSD License: https://opensource.org/licenses/BSD-3-Clause

"""filter"""

from collections.abc import Iterable

from sqlparse import lexer
from sqlparse.engine import grouping
from sqlparse.engine.statement_splitter import StatementSplitter
from sqlparse.exceptions import SQLParseError
from sqlparse.filters import StripTrailingSemicolonFilter


class FilterStack:
    def __init__(self, strip_semicolon=False):
        self.preprocess = []
        self.stmtprocess = []
        self.postprocess = []
        self._grouping = False
        if strip_semicolon:
            self.stmtprocess.append(StripTrailingSemicolonFilter())

    def enable_grouping(self):
        self._grouping = True

    def run(self, sql, encoding=None):
        stream = lexer.tokenize(sql, encoding)
        yield from self._run_token_stream(stream)

    def run_iter(self, chunks, encoding=None):
        """Like :meth:`run`, but driven by an iterable of text chunks.

        Each chunk is pulled from `chunks` only after the previously
        buffered text has been consumed, so a completed statement is
        delivered as soon as its terminator arrives rather than at end of
        input. Only the currently unfinished statement is held in memory."""
        if isinstance(chunks, (str, bytes)):
            raise TypeError(
                'chunks must be an iterable of text chunks, not a single '
                f'{type(chunks).__name__}; use run() for whole-text input')
        if not isinstance(chunks, Iterable):
            raise TypeError(
                f'chunks must be an iterable, got {type(chunks).__name__}')
        stream = lexer.tokenize_iter(chunks, encoding)
        yield from self._run_token_stream(stream)

    def _run_token_stream(self, stream):
        try:
            # Process token stream
            for filter_ in self.preprocess:
                stream = filter_.process(stream)

            stream = StatementSplitter().process(stream)

            # Output: Stream processed Statements
            for stmt in stream:
                if self._grouping:
                    stmt = grouping.group(stmt)

                for filter_ in self.stmtprocess:
                    filter_.process(stmt)

                for filter_ in self.postprocess:
                    stmt = filter_.process(stmt)

                yield stmt
        except RecursionError as err:
            raise SQLParseError('Maximum recursion depth exceeded') from err
