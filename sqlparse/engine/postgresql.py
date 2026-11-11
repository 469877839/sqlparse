#
# Copyright (C) 2009-2020 the sqlparse authors and contributors
# <see AUTHORS file>
#
# This module is part of python-sqlparse and is released under
# the BSD License: https://opensource.org/licenses/BSD-3-Clause

"""Splitting of PostgreSQL scripts.

Scripts written by ``pg_dump`` interleave ordinary SQL statements with
``COPY ... FROM STDIN`` blocks.  The lines following such a COPY header
are raw data, not SQL: they may contain semicolons, quotes,
comment-looking text and escaped backslashes.  The regular statement
splitter would happily split them apart, so this module scans the raw
script text first and hands only the SQL parts to the regular splitter.

A COPY block -- header, data lines and the terminating ``\\.`` line --
is yielded as one statement.  Its content is not tokenized (the COPY
field values are not parsed), it is kept as a single ``Token.Other``
so that the original text, including line endings, is preserved.
"""

import re

from sqlparse import lexer
from sqlparse import sql
from sqlparse import tokens as T
from sqlparse.engine.statement_splitter import StatementSplitter

__all__ = ['PostgreSQLScriptSplitter', 'normalize_dialect']


# The last line of a COPY data section contains only this marker.
_COPY_END_MARKER = '\\.'

# Characters that may appear in the whitespace between a COPY header
# and the data that follows it.
_WHITESPACE = ' \t\f\v\r\n'

# A COPY keyword starts a statement only if it is not preceded by an
# ordinary identifier character (it may still follow ";", whitespace or
# a comment).
_COPY_RE = re.compile(r'(?<![\w$])COPY\b', re.IGNORECASE)
_FROM_RE = re.compile(r'FROM\b', re.IGNORECASE)
_STDIN_RE = re.compile(r'STDIN\b', re.IGNORECASE)
_LINE_END_RE = re.compile(r'\r\n|\r|\n')
_DOLLAR_QUOTE_RE = re.compile(
    r'(?<![\w"$])(\$(?:[_A-Za-zÀ-Ü]\w*)?\$)', re.IGNORECASE | re.UNICODE)


def normalize_dialect(dialect):
    """Map a dialect name to ``None`` (default SQL) or ``'postgresql'``."""
    if dialect is None:
        return None
    if not isinstance(dialect, str):
        raise TypeError('dialect must be a string or None')
    normalized = dialect.strip().lower()
    if normalized in ('', 'default', 'sql'):
        return None
    if normalized in ('postgres', 'postgresql'):
        return 'postgresql'
    raise ValueError(f'Unknown dialect: {dialect!r}')


def coerce_text(sql, encoding=None):
    """Convert str, bytes or a file-like object into a string."""
    if isinstance(sql, str):
        return sql
    if hasattr(sql, 'read'):
        return sql.read()
    if isinstance(sql, bytes):
        if encoding:
            return sql.decode(encoding)
        try:
            return sql.decode('utf-8')
        except UnicodeDecodeError:
            return sql.decode('unicode-escape')
    raise TypeError(f"Expected text or file-like object, got {type(sql)!r}")


class PostgreSQLScriptSplitter:
    """Split a PostgreSQL script into statements.

    ``COPY ... FROM STDIN`` statements are delivered together with their
    raw data lines and the terminating marker as a single statement.
    Everything else is split by the regular statement splitter.
    """

    def process(self, text):
        """Yield statements for the PostgreSQL script *text*."""
        pos = 0
        while True:
            block = self._find_copy_block(text, pos)
            if block is None:
                yield from self._split_sql(text[pos:])
                return

            block_start, block_end = block
            # Whitespace between the previous statement and the COPY header
            # would be dropped as trailing whitespace by the statement
            # splitter.  Attach it to the COPY statement instead so that
            # concatenating the results reproduces the input exactly.
            ws_start = block_start
            while ws_start > pos and text[ws_start - 1] in _WHITESPACE:
                ws_start -= 1

            yield from self._split_sql(text[pos:ws_start])

            tokens = []
            if ws_start < block_start:
                tokens.append(sql.Token(
                    T.Whitespace, text[ws_start:block_start]))
            tokens.append(sql.Token(T.Other, text[block_start:block_end]))
            yield sql.Statement(tokens)
            pos = block_end

    @staticmethod
    def _split_sql(text):
        """Split plain-SQL *text* with the regular statement splitter."""
        if not text:
            return
        yield from StatementSplitter().process(lexer.tokenize(text))

    @staticmethod
    def _find_copy_block(text, start):
        """Locate the next complete ``COPY ... FROM STDIN`` block.

        Returns ``(header_start, block_end)`` where *block_end* is the
        offset just behind the terminating marker line, or None if no
        complete block remains.  An incomplete COPY block (no data
        terminator) is not treated as a data section: its header is left
        to the regular splitter.
        """
        search = start
        while True:
            match = _COPY_RE.search(text, search)
            if match is None:
                return None

            header_start = match.start()
            header_end, is_stdin = _find_header_end(text, match.end())
            if header_end is None:
                # Unclosed string in the header: nothing well-formed can
                # follow at all, so leave the tail to the regular splitter.
                return None
            if not is_stdin:
                # e.g. COPY ... TO STDOUT: no data section follows.
                search = header_end
                continue

            data_end = _find_data_end(text, header_end)
            if data_end is None:
                # Unterminated data section: the header is left to the
                # regular splitter.  No terminating marker line exists in
                # the remainder of the text, so no complete COPY block
                # can follow either; stop searching.
                return None

            return header_start, data_end


def _find_header_end(text, pos):
    """Scan a COPY header starting behind the word COPY.

    Returns ``(end, is_stdin)`` with *end* being the offset just behind
    the terminating semicolon and *is_stdin* True for a
    ``COPY ... FROM STDIN ...;`` header.  Returns ``(None, False)`` for a
    malformed header (unclosed quoted string or missing semicolon).
    """
    length = len(text)
    saw_from = False
    saw_stdin = False
    while pos < length:
        char = text[pos]
        if char in ("'", '"', '`'):
            end = _find_quoted_end(text, pos, char)
            if end is None:
                return None, False
            pos = end
            continue
        if text.startswith('--', pos):
            pos = _find_line_end(text, pos)
            continue
        if text.startswith('/*', pos):
            close = text.find('*/', pos + 2)
            if close == -1:
                return None, False
            pos = close + 2
            continue
        if char == '$':
            end = _find_dollar_quote_end(text, pos)
            if end is None:
                return None, False
            pos = end
            continue
        if char == ';':
            return pos + 1, (saw_from and saw_stdin)

        if not saw_stdin and _FROM_RE.match(text, pos):
            saw_from = True
            pos = pos + 4
            continue
        if saw_from and not saw_stdin and _STDIN_RE.match(text, pos):
            saw_stdin = True
            pos = pos + 5
            continue
        pos += 1
    return None, False


def _find_dollar_quote_end(text, pos):
    """Return the offset behind a dollar-quoted string starting at *pos*.

    Returns ``pos + 1`` when the character at *pos* does not open a
    dollar-quoted string (e.g. a ``$1`` placeholder), None when an
    opened string is not closed.
    """
    match = _DOLLAR_QUOTE_RE.match(text, pos)
    if match is None:
        return pos + 1
    tag = match.group(1)
    close = text.find(tag, match.end())
    if close == -1:
        return None
    return close + len(tag)


def _find_quoted_end(text, pos, quote):
    """Return the offset behind the quoted string starting at *pos*.

    Quoted quotes are escaped by doubling (standard SQL); a backslash
    before the next character also prevents it from closing the string.
    Returns None when the string is not closed.
    """
    pos += 1
    length = len(text)
    while pos < length:
        char = text[pos]
        if char == '\\':
            pos += 2
            continue
        if char == quote:
            if pos + 1 < length and text[pos + 1] == quote:
                pos += 2
                continue
            return pos + 1
        pos += 1
    return None


def _find_data_end(text, pos):
    """Find the end of the data section behind a COPY header.

    Only a line consisting solely of the terminating marker ends the
    section.  Returns the offset behind the line ending that follows
    the marker (or behind the marker at end of input), None if no such
    line exists before the end of the text.
    """
    while True:
        content_end, line_end = _find_line_bounds(text, pos)
        if text[pos:content_end] == _COPY_END_MARKER:
            return line_end
        if line_end == len(text):
            return None
        pos = line_end


def _find_line_end(text, pos):
    """Return the offset behind the line ending of the line at *pos*."""
    return _find_line_bounds(text, pos)[1]


def _find_line_bounds(text, pos):
    """Return ``(content_end, line_end)`` for the line starting at *pos*.

    *content_end* is the offset where the line content ends (before the
    line ending), *line_end* the offset behind the line ending.
    """
    # Search with pos/m.endpos instead of slicing the remaining text so
    # that the scan stays linear for large data sections.
    match = _LINE_END_RE.search(text, pos)
    if match is None:
        return len(text), len(text)
    return match.start(), match.end()
