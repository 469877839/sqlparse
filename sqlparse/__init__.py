#
# Copyright (C) 2009-2020 the sqlparse authors and contributors
# <see AUTHORS file>
#
# This module is part of python-sqlparse and is released under
# the BSD License: https://opensource.org/licenses/BSD-3-Clause

"""Parse SQL statements."""

# Setup namespace
from collections.abc import Generator, Iterable
from typing import IO, Any

from sqlparse import cli, engine, filters, formatter, sql, tokens

__version__ = "0.6.1"
__all__ = ["cli", "engine", "filters", "formatter", "sql", "tokens"]


def parse(
    sql: str, encoding: str | None = None
) -> tuple[sql.Statement, ...]:
    """Parse sql and return a list of statements.

    :param sql: A string containing one or more SQL statements.
    :param encoding: The encoding of the statement (optional).
    :returns: A tuple of :class:`~sqlparse.sql.Statement` instances.
    """
    return tuple(parsestream(sql, encoding))


def parsestream(
    stream: str | IO[str], encoding: str | None = None
) -> Generator[sql.Statement, None, None]:
    """Parses sql statements from file-like object.

    :param stream: A file-like object.
    :param encoding: The encoding of the stream contents (optional).
    :returns: A generator of :class:`~sqlparse.sql.Statement` instances.
    """
    stack = engine.FilterStack()
    stack.enable_grouping()
    return stack.run(stream, encoding)


def parseiter(
    chunks: Iterable[str | bytes], encoding: str | None = None
) -> Generator[sql.Statement, None, None]:
    """Parse SQL text that arrives piece by piece.

    `chunks` is an iterable yielding the script as text (or bytes) chunks
    in arrival order -- for example blocks read from an object store.
    Chunks are pulled lazily: as soon as a statement is complete (its
    terminating semicolon, ``GO`` or end of input), the corresponding
    :class:`~sqlparse.sql.Statement` is yielded, without waiting for the
    whole script. Chunk boundaries may fall inside escapes, two-character
    line comments, multiline comments or dollar-quoted literals; such
    fragments are buffered until they can be tokenized, so the result is
    identical to :meth:`parse` on the concatenated text. Only the current,
    not-yet-delivered statement is kept in memory.

    An empty chunks iterable yields nothing. Exhausting the iterable acts
    as end of input and flushes the final statement. Closing the returned
    generator early stops pulling further chunks and releases all buffered
    text.

    :param chunks: An iterable of ``str`` or ``bytes`` chunks.
    :param encoding: Encoding used to decode bytes chunks (optional).
    :returns: A generator of :class:`~sqlparse.sql.Statement` instances.
    """
    stack = engine.FilterStack()
    stack.enable_grouping()
    return stack.run_iter(chunks, encoding)


def format(sql: str, encoding: str | None = None, **options: Any) -> str:
    """Format *sql* according to *options*.

    Available options are documented in :ref:`formatting`.

    In addition to the formatting options this function accepts the
    keyword "encoding" which determines the encoding of the statement.

    :returns: The formatted SQL statement as string.
    """
    stack = engine.FilterStack()
    options = formatter.validate_options(options)
    stack = formatter.build_filter_stack(stack, options)
    stack.postprocess.append(filters.SerializerUnicode())
    return "".join(stack.run(sql, encoding))


def split(
    sql: str, encoding: str | None = None, strip_semicolon: bool = False
) -> list[str]:
    """Split *sql* into single statements.

    :param sql: A string containing one or more SQL statements.
    :param encoding: The encoding of the statement (optional).
    :param strip_semicolon: If True, remove trailing semicolons
        (default: False).
    :returns: A list of strings.
    """
    stack = engine.FilterStack(strip_semicolon=strip_semicolon)
    return [str(stmt).strip() for stmt in stack.run(sql, encoding)]
