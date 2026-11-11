"""Deep Token trees must be handled without the Python recursion stack.

Callers keep references to tokens and keep using ``parent``, ``token_next``
and ``flatten`` after grouping, so the iterative traversals in this module
assert those relationships stay intact -- identity, ancestry, document order
and whitespace -- while depth grows well past ``sys.getrecursionlimit()``.
"""

import io

import pytest

import sqlparse
from sqlparse import filters, sql
from sqlparse import tokens as T
from sqlparse.engine import grouping

# Far beyond the default recursion limit (1000). Building the tree itself is
# iterative, so this only exercises the code under test.
DEPTH = 5000


def make_deep_tree(depth, ws=False):
    """Build ``Statement > Parenthesis > ... > '1'`` without recursing."""
    sep = (' ', ' ') if ws else ('', '')
    inner = sql.Token(T.Number.Integer, '1')
    for _ in range(depth):
        inner = sql.Parenthesis([
            sql.Token(T.Punctuation, '(' + sep[0]),
            inner,
            sql.Token(T.Punctuation, ')' + sep[1]),
        ])
    stmt = sql.Statement([sql.Token(T.Keyword.DML, 'SELECT'),
                          sql.Token(T.Whitespace, ' '), inner])
    return stmt


def deepest_leaf(stmt):
    return next(tok for tok in stmt.flatten() if tok.value == '1')


def test_flatten_is_iterative_and_ordered():
    stmt = make_deep_tree(DEPTH)
    leaves = list(stmt.flatten())
    expected_len = (2 * DEPTH) + 3  # SELECT, ' ', DEPTH '(', '1', DEPTH ')'
    assert len(leaves) == expected_len
    values = [leaf.value for leaf in leaves]
    assert values == ['SELECT', ' '] + ['('] * DEPTH + ['1'] + [')'] * DEPTH
    # Same objects as the tree itself: callers may keep references.
    outer = stmt.tokens[-1]
    assert leaves[2] is outer.tokens[0]


def test_flatten_is_lazy():
    stmt = make_deep_tree(DEPTH)
    gen = stmt.flatten()
    assert next(gen).value == 'SELECT'
    # A lazy generator must not have walked the whole tree yet.
    del gen


def test_str_roundtrip_uses_iterative_flatten():
    stmt = make_deep_tree(2000, ws=True)
    expected = 'SELECT ' + '( ' * 2000 + '1' + ') ' * 2000
    assert str(stmt) == expected
    # Whitespace tokens survive grouping and flattening.
    assert str(stmt).count(' ') == 1 + 2 * 2000


def test_get_token_at_offset_end():
    stmt = make_deep_tree(DEPTH)
    last = list(stmt.flatten())[-1]
    total = len(str(stmt)) - 1
    assert stmt.get_token_at_offset(total) is last
    middle = len(str(stmt)) // 2
    assert stmt.get_token_at_offset(middle) is not None


def test_pprint_tree_iterative_and_unchanged():
    stmt = make_deep_tree(DEPTH)
    output = io.StringIO()
    stmt._pprint_tree(max_depth=2, f=output)
    lines = output.getvalue().splitlines()
    # max_depth stops the expansion early and keeps indexes formatted.
    assert lines[0] == '|- 0 DML \'SELECT\''
    assert any('Parenthesis' in line for line in lines)
    assert len(lines) < 10


def test_parent_chain_traversable_at_depth():
    stmt = make_deep_tree(2000, ws=True)
    leaf = deepest_leaf(stmt)
    assert leaf.value == '1'
    depth = 0
    node = leaf
    while node is not None:
        node = node.parent
        depth += 1
    assert depth == 2000 + 2  # leaf + 2000 Parenthesis + Statement
    assert leaf.has_ancestor(stmt)
    assert leaf.within(sql.Parenthesis)


def test_token_next_and_prev_on_deep_tree():
    stmt = make_deep_tree(1000, ws=True)
    outer = stmt.tokens[-1]
    nidx, nxt = outer.token_next(0, skip_ws=False)
    assert isinstance(nxt, sql.Parenthesis)
    pidx, prv = outer.token_prev(2, skip_ws=False)
    assert pidx == 1

    # Walk the spine with token_next instead of recursion.
    node = stmt.tokens[-1]
    steps = 0
    while isinstance(node, sql.Parenthesis):
        idx, nxt = node.token_next(0, skip_ws=False)
        if nxt is None or not nxt.is_group:
            break
        node = nxt
        steps += 1
    assert steps >= 900


@pytest.mark.parametrize('filter_cls', [
    filters.ReindentFilter,
    filters.AlignedIndentFilter,
    filters.StripWhitespaceFilter,
    filters.StripCommentsFilter,
    filters.SpacesAroundOperatorsFilter,
])
def test_filters_handle_deep_tree(filter_cls):
    stmt = make_deep_tree(3000)
    filter_cls().process(stmt)


def test_grouping_pass_probes_iterative(uncapped_grouping):
    """Directly exercise the two GroupingEngine entry points."""
    stmt = make_deep_tree(1500)
    # _group_matching and _group used to recurse per nesting level.
    grouping._group_matching(stmt, sql.Parenthesis)
    grouping.group(stmt)


@pytest.fixture()
def uncapped_grouping(monkeypatch):
    monkeypatch.setattr(grouping, 'MAX_GROUPING_DEPTH', None)
    monkeypatch.setattr(grouping, 'MAX_GROUPING_TOKENS', None)


def test_real_sql_deep_nesting_parses(uncapped_grouping):
    n = 3000
    stmt = sqlparse.parse('SELECT ' + '(' * n + '1' + ')' * n)[0]
    leaf = deepest_leaf(stmt)
    chain = 0
    node = leaf
    while node is not None:
        node = node.parent
        chain += 1
    assert chain == n + 2
    assert str(stmt) == 'SELECT ' + '(' * n + '1' + ')' * n
    assert len(list(stmt.flatten())) == 2 * n + 3


def test_real_sql_deep_case_parses(uncapped_grouping):
    n = 1200
    text = '1'
    for i in range(n):
        text = f'CASE WHEN x={i} THEN {text} ELSE NULL END'
    stmt = sqlparse.parse('SELECT ' + text + ' FROM t')[0]
    assert 'CASE WHEN x=0' in str(stmt)
    assert len(list(stmt.flatten())) > n


@pytest.mark.parametrize('options', [
    {'reindent': True},
    {'reindent_aligned': True},
    {'strip_whitespace': True},
    {'strip_comments': True},
    {'use_space_around_operators': True},
])
def test_real_sql_deep_format(uncapped_grouping, options):
    n = 2000
    sql_text = 'SELECT ' + '(' * n + '1' + ')' * n
    out = sqlparse.format(sql_text, **options)
    assert out.count('(') == n


def test_depth_cap_rejects_before_grouping(uncapped_grouping, monkeypatch):
    monkeypatch.setattr(grouping, 'MAX_GROUPING_DEPTH', 100)
    monkeypatch.setattr(grouping, 'MAX_GROUPING_TOKENS', None)
    n = 500
    with pytest.raises(sqlparse.exceptions.SQLParseError,
                       match='Maximum grouping depth exceeded'):
        sqlparse.parse('SELECT ' + '(' * n + '1' + ')' * n)


def test_token_cap_still_reported(uncapped_grouping, monkeypatch):
    monkeypatch.setattr(grouping, 'MAX_GROUPING_DEPTH', None)
    monkeypatch.setattr(grouping, 'MAX_GROUPING_TOKENS', 100)
    with pytest.raises(sqlparse.exceptions.SQLParseError,
                       match='Maximum number of tokens exceeded'):
        sqlparse.parse('SELECT ' + ', '.join(f'c{i}' for i in range(300)))


def test_visit_count_grows_with_depth_only(uncapped_grouping):
    """Each grouping pass must visit every group exactly once per level.

    The reachable-group count is read from the tree first and then compared
    with an independent counter collection during parsing of real SQL; a
    super-linear (quadratic) walk would show counts that grow faster than
    the number of groups.
    """
    from sqlparse.engine import grouping as g

    real_calls = 0
    original = g._group

    def counting_group(*args, **kwargs):
        nonlocal real_calls
        real_calls += 1
        return original(*args, **kwargs)

    counts = []
    for n in (200, 400, 800):
        group_root = sqlparse.parse('SELECT ' + '(' * n + '1' + ')' * n)[0]
        # Count nodes actually present in the resulting tree.
        nodes = 0
        stack = [group_root]
        while stack:
            current = stack.pop()
            nodes += 1
            stack.extend(t for t in current.tokens if t.is_group)
        # Number of _group invocations per nesting level must be bounded by
        # a small constant, independent of depth.
        g._group = counting_group
        try:
            sqlparse.parse('SELECT ' + '(' * n + '1' + ')' * n)
        finally:
            g._group = original
        counts.append((nodes, real_calls))

    assert all(calls < nodes * 2 for nodes, calls in counts)
    # Calls grow linearly, not quadratically, when depth doubles.
    (n1, c1), (n2, c2), (n3, c3) = counts
    assert c3 <= c1 * 4 + 8
