#
# Copyright (C) 2009-2020 the sqlparse authors and contributors
# <see AUTHORS file>
#
# This module is part of python-sqlparse and is released under
# the BSD License: https://opensource.org/licenses/BSD-3-Clause

"""filter"""

from sqlparse import lexer, protected
from sqlparse.engine import grouping
from sqlparse.engine.statement_splitter import StatementSplitter
from sqlparse.exceptions import SQLParseError
from sqlparse.filters import StripTrailingSemicolonFilter


class FilterStack:
    def __init__(self, strip_semicolon=False, keep_fmt_regions=False):
        self.preprocess = []
        self.stmtprocess = []
        self.postprocess = []
        self._grouping = False
        self._keep_fmt_regions = keep_fmt_regions
        if strip_semicolon:
            self.stmtprocess.append(StripTrailingSemicolonFilter())

    def enable_grouping(self):
        self._grouping = True

    def run(self, sql, encoding=None):
        try:
            stream = lexer.tokenize(sql, encoding)

            # Annotate tokens that fall into a fmt: off / fmt: on region.
            # This happens on the token stream of the whole document, so
            # parentheses and statement context stay intact across a
            # protected region instead of the document being cut into
            # unrelated SQL chunks.
            has_regions = False
            if self._keep_fmt_regions and isinstance(sql, str):
                fmt_filter = protected.ProtectedStreamFilter(sql)
                has_regions = fmt_filter.has_regions
                stream = fmt_filter.process(stream)

            # Process token stream
            for filter_ in self.preprocess:
                stream = filter_.process(stream)

            stream = StatementSplitter().process(stream)

            # Output: Stream processed Statements
            for stmt in stream:
                if self._grouping:
                    stmt = grouping.group(stmt)

                if has_regions:
                    stmt.update_protected_state()

                for filter_ in self.stmtprocess:
                    filter_.process(stmt)

                for filter_ in self.postprocess:
                    stmt = filter_.process(stmt)

                yield stmt
        except RecursionError as err:
            raise SQLParseError('Maximum recursion depth exceeded') from err
