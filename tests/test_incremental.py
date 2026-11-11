#
# Copyright (C) 2009-2020 the sqlparse authors and contributors
# <see AUTHORS file>
#
# This module is part of python-sqlparse and is released under
# the BSD License: https://opensource.org/licenses/BSD-3-Clause

"""Tests for the incremental (chunked) parsing entry points."""

import random

import pytest

import sqlparse
from sqlparse import lexer
from sqlparse import tokens as T


# Short scripts whose tricky boundaries must survive any chunk split.
SCRIPTS = [
    'select 1; select 2;',
    "select 'a''b'; select 2;",
    "select 'x\\'' as y;",
    'a -- c\nb;',
    'a # comm\nb;',
    '/* nested /* inner */ outer */ x;',
    '/*+ hint */ select 1;',
    '$$ body $$ x;',
    '$body$ body $body$;',
    'select 1\r\nselect 2;',
    'a-b;',
    "select 'unclosed",
    'select "unclosed',
    'select `unclosed',
    '$$ unclosed',
    '/* unclosed',
    'x ->> y;',
    'create or replace function f() return int as $$ begin $$;',
    'x /* a */ /* b */ y;',
    'x [abc] y;',
    'union all select 1;',
    'order by a;',
    'group by a;',
    'double precision;',
    'begin select 1; end;',
    'select case when a then b end from v;',
]


def _inc_tokens(chunks):
    inc = lexer.IncrementalLexer()
    out = []
    for chunk in chunks:
        out.extend(inc.feed(chunk))
    out.extend(inc.close())
    return out


def _all_cuts(text):
    for cut in range(len(text) + 1):
        yield [text[:cut], text[cut:]]


@pytest.mark.parametrize('script', SCRIPTS)
def test_single_cut_token_equivalence(script):
    # Every single cut point must produce the exact one-shot token stream.
    expected = list(lexer.tokenize(script))
    for chunks in _all_cuts(script):
        assert _inc_tokens(chunks) == expected


@pytest.mark.parametrize('script', SCRIPTS)
def test_single_cut_statement_equivalence(script):
    # The delivered, grouped Statement trees must match sqlparse.parse.
    expected = sqlparse.parse(script)
    for chunks in _all_cuts(script):
        got = tuple(sqlparse.parseiter(chunks))
        assert len(got) == len(expected)
        for stmt, exp in zip(got, expected):
            assert str(stmt) == str(exp)
            assert _shape(stmt) == _shape(exp)


def _shape(stmt):
    """Token tree without object identity (parent links, addresses)."""
    def walk(token):
        children = getattr(token, 'tokens', None)
        if children is None:
            return token.ttype, token.value
        return [walk(child) for child in children]
    return walk(stmt)


@pytest.mark.parametrize('script', SCRIPTS)
def test_fixed_seed_multi_cut(script):
    # Deterministic multi-cut comparison against the one-shot parse.
    rng = random.Random(20240517)
    expected = list(lexer.tokenize(script))
    expected_stmts = [str(s) for s in sqlparse.parse(script)]
    for _ in range(25):
        count = rng.randint(1, max(1, len(script)))
        points = sorted(rng.sample(range(len(script) + 1),
                                   min(count, len(script) + 1)))
        chunks, prev = [], 0
        for point in points:
            chunks.append(script[prev:point])
            prev = point
        chunks.append(script[prev:])
        assert _inc_tokens(chunks) == expected
        got_stmts = [str(s) for s in sqlparse.parseiter(iter(chunks))]
        assert got_stmts == expected_stmts


def test_on_demand_iterator_first_statement_without_eof():
    # The next chunk is produced only when requested: after the first
    # statement is delivered, exactly one extra chunk (needed to confirm
    # the statement boundary) has been pulled -- never the whole script.
    pulled = []

    def chunks():
        for chunk in ('select 1;', ' select 2;', ' select 3;'):
            pulled.append(chunk)
            yield chunk

    statements = sqlparse.parseiter(chunks())
    first = next(statements)
    assert str(first).strip() == 'select 1;'
    assert len(pulled) == 2
    assert pulled[0] == 'select 1;'
    statements.close()


def test_on_demand_iterator_holds_back_incomplete_token():
    # A chunk ending mid-dollar-quote must not emit the partial literal.
    pulled = []

    def chunks():
        for chunk in ('select $tag$ bo', 'dy $tag$;'):
            pulled.append(chunk)
            yield chunk

    statements = sqlparse.parseiter(chunks())
    next(statements)
    assert len(pulled) == 2


def test_empty_chunks_are_ignored():
    inc = lexer.IncrementalLexer()
    assert list(inc.feed('')) == []
    emitted = list(inc.feed('select 1;'))
    assert list(inc.feed('')) == []
    assert emitted + list(inc.close()) == \
        list(lexer.tokenize('select 1;'))


def test_empty_iterable_yields_nothing():
    assert list(sqlparse.parseiter([])) == []
    assert list(lexer.tokenize_iter(iter(()))) == []


def test_explicit_close_flushes_final_statement():
    inc = lexer.IncrementalLexer()
    partial = list(inc.feed('select 1'))
    assert partial == [(T.Keyword.DML, 'select'),
                       (T.Whitespace, ' ')]
    flushed = list(inc.close())
    assert flushed == [(T.Literal.Number.Integer, '1')]
    assert list(inc.close()) == []
    with pytest.raises(ValueError):
        list(inc.feed('x'))


def test_close_resolves_truncated_tail_like_one_shot():
    # Unclosed quote and comment at EOF get the same fallback tokens as
    # the one-shot lexer produces for a truncated tail.
    for script in ("select 'unclosed", '/* unclosed', '$$ unclosed'):
        inc = lexer.IncrementalLexer()
        got = list(inc.feed(script)) + list(inc.close())
        assert got == list(lexer.tokenize(script))


def test_consumer_early_close_stops_pulling():
    pulled = []

    def chunks():
        for chunk in ('select 1;', ' select 2;', ' select 3;'):
            pulled.append(chunk)
            yield chunk

    statements = sqlparse.parseiter(chunks())
    next(statements)
    statements.close()
    # No chunk after the one confirming the first boundary is pulled.
    assert pulled == ['select 1;', ' select 2;']


def test_tokenize_iter_early_close_releases_buffer():
    # GeneratorExit inside tokenize_iter drops the buffered statement;
    # exercise the same release path explicitly on an IncrementalLexer.
    inc = lexer.IncrementalLexer()
    list(inc.feed('select 1; select 2;'))
    inc._discard(len(inc._buffer))
    assert inc._buffer == ''
    assert inc._openers == {}

    def chunks():
        yield 'select 1; select 2;'

    stream = lexer.tokenize_iter(chunks())
    next(stream)
    stream.close()  # must not raise


def test_bytes_chunks_decoded():
    got = list(lexer.tokenize_iter([b'select ', b'1;']))
    assert got == list(lexer.tokenize('select 1;'))


def test_non_iterable_rejected():
    with pytest.raises(TypeError):
        list(sqlparse.parseiter('select 1;'))


def test_feed_rejects_non_string():
    inc = lexer.IncrementalLexer()
    with pytest.raises(TypeError):
        list(inc.feed(b'select 1;'))


def test_delivered_statements_are_not_retained():
    # A thousand statements stream through without accumulating; only the
    # unfinished tail of the last statement remains buffered.
    source = ('select %d; ' % i for i in range(1000))
    count = 0
    for _ in sqlparse.parseiter(source):
        count += 1
    assert count == 1000


def test_nested_comment_boundary_everywhere():
    script = '/* a /* b */ c */ select 1;'
    expected = list(lexer.tokenize(script))
    for cut in range(len(script) + 1):
        assert _inc_tokens([script[:cut], script[cut:]]) == expected


def test_escape_boundary_everywhere():
    script = "select 'a\\'b', 1;"
    expected = list(lexer.tokenize(script))
    for cut in range(len(script) + 1):
        assert _inc_tokens([script[:cut], script[cut:]]) == expected


def test_double_char_comment_boundary_everywhere():
    scripts = ['a--comment\nselect 1;', 'a# comment\nselect 1;']
    for script in scripts:
        expected = list(lexer.tokenize(script))
        for cut in range(len(script) + 1):
            assert _inc_tokens([script[:cut], script[cut:]]) == expected


def test_dollar_quote_boundary_everywhere():
    script = 'select $tag$body$tag$, 1;'
    expected = list(lexer.tokenize(script))
    for cut in range(len(script) + 1):
        assert _inc_tokens([script[:cut], script[cut:]]) == expected
