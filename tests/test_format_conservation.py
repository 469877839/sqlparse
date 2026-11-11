"""Content-conservation checks for ``sqlparse.format``.

The formatter is allowed to change *layout*, never *content*.  These tests
re-lex both the original and the formatted SQL and compare the stream of
semantic tokens instead of doing a whitespace-insensitive string compare
(which cannot detect a merged operator, a rewritten literal or a shuffled
placeholder).

Allowed differences (everything else must be identical, in order)
------------------------------------------------------------------
* Whitespace / newlines: arbitrary insertion, removal or replacement.
* Keyword letter case: rewritten by ``keyword_case``
  (``upper`` / ``lower`` / ``capitalize``); compared case-insensitively.
* Identifier letter case: ``identifier_case`` rewrites bare names,
  ``backtick`` names and ``[bracket]`` names.  Double-quoted identifiers
  (``"..."``) are case-sensitive in SQL and the formatter never touches
  them, so they are compared verbatim.
* ``strip_comments=True`` may remove comments; otherwise comment text is
  conserved (only surrounding whitespace may move).
* Comments are compared as their own category so a mutated comment is
  reported separately rather than hidden behind whitespace normalization.

Explicitly out of scope (documented, not asserted here)
-------------------------------------------------------
* ``truncate_strings``: a deliberately content-destructive option.
* ``output_format`` (python/php escaping) and ``strip_semicolon``:
  non-SQL serialization / token removal, not layout changes.
* Dialect syntax the generic lexer does not model (see
  ``UNCOVERED_DIALECT_SYNTAX`` at the bottom of this module).

A failed case writes a ready-to-paste regression example -- input, options
and a token-level unified diff -- into
``tests/files/conservation_failures/``.
"""

import difflib
import hashlib
import json
import random
from pathlib import Path

import pytest

import sqlparse
from sqlparse import tokens as T
from sqlparse.formatter import build_filter_stack

FAILURE_DIR = Path(__file__).parent / 'files' / 'conservation_failures'
SEED = 20261111


# --- semantic token extraction --------------------------------------------
#
# A semantic token is every leaf token that carries meaning: keywords,
# names, literals, placeholders, operators, punctuation, wildcards and
# comments.  Whitespace is skipped by definition, so layout-only changes
# cannot show up as a difference.

SEMANTIC_ROOT_TYPES = (
    T.Keyword,
    T.Name,
    T.Literal,
    T.Operator,
    T.Punctuation,
    T.Wildcard,
    T.Comment,
    T.Error,
    T.Other,
)

# Independent check categories required by the task.  Every semantic token
# belongs to exactly one category; each category is compared on its own so a
# failure points at the damaged construct instead of at a generic string.
CATEGORY_STRING = 'string_literal'
CATEGORY_QUOTED_ID = 'quoted_identifier'
CATEGORY_PLACEHOLDER = 'placeholder'
CATEGORY_COMMENT = 'comment'
CATEGORY_OPERATOR = 'operator'
CATEGORY_OTHER = 'other_semantic_token'


def _is_semantic(ttype):
    return any(ttype in root for root in SEMANTIC_ROOT_TYPES)


def _category_of(ttype):
    if ttype in T.Literal.String.Symbol:
        return CATEGORY_QUOTED_ID
    if ttype in T.Literal.String:
        return CATEGORY_STRING
    if ttype in T.Name.Placeholder:
        return CATEGORY_PLACEHOLDER
    if ttype in T.Comment:
        return CATEGORY_COMMENT
    if ttype in T.Operator or ttype in T.Wildcard:
        # Wildcard '*' is an operator-boundary token: "a.*b" vs "a. * b"
        # must keep '*' glued to the same neighboring tokens.
        return CATEGORY_OPERATOR
    return CATEGORY_OTHER


def _canonical_value(ttype, value):
    """Normalize only the changes the formatter is permitted to make."""
    if ttype in T.Keyword:
        return value.lower()
    if ttype in T.Name:
        # "double quoted" identifiers are case-sensitive and the
        # IdentifierCaseFilter skips them; bare, `backtick` and [bracket]
        # names are case-rewritten, so compare those case-insensitively.
        if value[:1] == '"':
            return value
        return value.lower()
    return value


def semantic_tokens(sql):
    """Return ``(category, ttype, canonical_value, raw_value)`` tuples.

    Whitespace is excluded.  Values are canonicalized for the letter-case
    changes the format options legitimately perform; string literals,
    quoted identifiers, placeholders, comments and operators keep their
    raw text (the canonicalizer only changes keywords / unquoted names).
    """
    out = []
    for stmt in sqlparse.parse(sql):
        for token in stmt.flatten():
            ttype = token.ttype
            if not _is_semantic(ttype):
                continue
            raw = token.value
            out.append((
                _category_of(ttype),
                str(ttype),
                _canonical_value(ttype, raw),
                raw,
            ))
    return out


def _strip_comments(records):
    return [rec for rec in records if rec[0] != CATEGORY_COMMENT]


def expected_tokens(sql, options):
    """Semantic tokens the formatter is expected to preserve."""
    records = semantic_tokens(sql)
    if options.get('strip_comments'):
        records = _strip_comments(records)
    return records


def actual_tokens(formatted_sql, options):
    records = semantic_tokens(formatted_sql)
    if options.get('strip_comments'):
        records = _strip_comments(records)
    return records


# --- comparison, token diff and failure artifacts -------------------------

def _render_record(rec):
    category, ttype, value, raw = rec
    if value != raw:
        return f'{category} {ttype} {value!r} (raw {raw!r})'
    return f'{category} {ttype} {value!r}'


def token_diff(before, after):
    """Unified diff over semantic token lines (no whitespace involved)."""
    a = [_render_record(rec) for rec in before]
    b = [_render_record(rec) for rec in after]
    return list(difflib.unified_diff(a, b, fromfile='tokens(before)',
                                     tofile='tokens(after)', lineterm=''))


def _boundary_signatures(records):
    """Adjacency signatures for operator-boundary conservation.

    Returns the ordered neighbors (previous, next semantic category/value)
    around every operator / wildcard token.  A formatter that splits
    ``->>`` into ``- >`` or merges ``a > = b`` changes these signatures even
    when the per-token category lists happen to stay the same length.
    """
    sigs = []
    for idx, rec in enumerate(records):
        if rec[0] != CATEGORY_OPERATOR:
            continue
        prev_rec = records[idx - 1] if idx > 0 else None
        next_rec = records[idx + 1] if idx + 1 < len(records) else None
        sigs.append((
            rec[2],
            None if prev_rec is None else (prev_rec[0], prev_rec[2]),
            None if next_rec is None else (next_rec[0], next_rec[2]),
        ))
    return sigs


def _by_category(records):
    buckets = {}
    for rec in records:
        buckets.setdefault(rec[0], []).append(rec)
    return buckets


def _key(rec):
    """Equality key: category, token type and canonical value.

    The raw value is rendering-only: legitimate keyword/identifier case
    changes alter it without altering content.
    """
    return rec[:3]


def conservation_errors(before, after):
    """Return a list of human-readable conservation violations."""
    errors = []

    # 1. Full ordered stream (catches lost / inserted / reordered tokens).
    if [_key(rec) for rec in before] != [_key(rec) for rec in after]:
        errors.append('ordered semantic token stream differs:\n'
                      + '\n'.join(token_diff(before, after)))

    # 2. Per-category independent checks (strings, quoted identifiers,
    #    placeholders, comments, operators each compared in isolation).
    before_cat, after_cat = _by_category(before), _by_category(after)
    for category in sorted(set(before_cat) | set(after_cat)):
        left = [_key(rec) for rec in before_cat.get(category, [])]
        right = [_key(rec) for rec in after_cat.get(category, [])]
        if left != right:
            errors.append(
                f'category {category!r} differs:\n'
                + '\n'.join(token_diff(before_cat.get(category, []),
                                      after_cat.get(category, []))))

    # 3. Operator boundary adjacency.
    left_sigs = _boundary_signatures(before)
    right_sigs = _boundary_signatures(after)
    if left_sigs != right_sigs:
        errors.append(
            'operator boundaries differ:\n'
            f'  before={left_sigs!r}\n'
            f'  after ={right_sigs!r}')
    return errors


def dump_failure(sql, options, before, after, errors):
    """Persist a regression-ready example and return its JSON path.

    The filename is a content hash so parallel test runs cannot collide and
    the same failure never produces duplicate files.
    """
    FAILURE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(
        (json.dumps(options, sort_keys=True) + '\x00' + sql).encode('utf-8')
    ).hexdigest()[:12]
    payload = {
        'sql': sql,
        'options': options,
        'errors': errors,
        'tokens_before': [list(rec) for rec in before],
        'tokens_after': [list(rec) for rec in after],
        'token_diff': token_diff(before, after),
        'formatted': sqlparse.format(sql, **options),
    }
    path = FAILURE_DIR / f'failure_{digest}.json'
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding='utf-8')
    return path


# --- corpus: handcrafted cases + fixed-seed generator ---------------------

HANDCRAFTED = [
    # string literals with tricky quoting / escapes
    "select 'it''s a ''quote''', 'semi;colon', '--not a comment' from t",
    "select 'multi\nline', 'tab\there', '' from t",
    # quoted identifiers (case-sensitive content)
    'select "Weird Col", "a""b", [bracket name], `bt name` from "My Tbl"',
    # placeholders of several styles
    "select :1, :name, ?, %s, %(named)s, $1, $var from t where id = :id",
    # operator boundaries: multi-char operators must stay glued
    "select a||b, c->>d, e>=f, g<>h, i!=j, k<=l, m~n, o&p|q from t",
    # semantic comments between clauses
    "select a -- keep me\n, b /* also keep */ from t where x = 1 -- tail\n",
    "/* head */ select /* mid */ a from t /* tail */",
    # wildcards and qualified stars
    "select *, t.*, count(*) from t join u on t.id = u.id",
    # numbers in several forms
    "select 1, 1.5, .5, 1e3, 1.5e-3, 0x1F, 1_000 from t",
    # case/keywords mix
    "SeLeCt A, b FrOm T WhErE x LiKe 'y%' AnD z In (1, 2, 3)",
    # nested parens, functions, CASE
    "select f(g(a), (b + c) * 2), case when x then 'y' else 'z' end from t",
    # multi-statement
    "select 1; select 2; -- c\nselect 3;",
    # insert / update / delete with comments
    "insert into t (a, b) values (1, 'x'); -- done\nupdate t set a = 2",
    # comparison chains touching operator boundaries
    "select a from t where x>=1 and y<=2 and z<>3 and w!=4",
    # unicode content in strings and quoted identifiers
    "select 'héllo wörld', \"ünïcödé\" from té where ü = 'ü'",
]


def _generate_seeded_cases(seed, count):
    """Deterministic short inputs from a fixed seed.

    The generator composes fragments that specifically stress token
    boundaries: strings containing punctuation that looks like SQL,
    placeholders glued to operators, and comments embedded between clauses.
    """
    rng = random.Random(seed)
    tables = ['t', 'users', 'order_items', '"Quoted Tbl"', '`bt_tbl`']
    columns = ['a', 'b_c', '"Quoted Col"', '`bt_col`', '[br col]', 't.*', '*']
    strings = ["'plain'", "'it''s'", "'-- tricky'", "'/* tricky */'",
               "'a,b(c)'", "'%s'", "''", "'semi;colon'"]
    placeholders = [':p', ':1', '?', '%s', '%(k)s', '$1']
    operators = ['=', '!=', '<>', '>=', '<=', '||', '+', '-', '*', '/', '%']
    comments = ['-- tail comment\n', '/* block */', '-- x; y\n']

    cases = []
    for _ in range(count):
        cols = ', '.join(rng.choice(columns) for _ in range(rng.randint(1, 4)))
        stmt = f'select {cols} from {rng.choice(tables)}'
        if rng.random() < 0.7:
            left = rng.choice(columns)
            op = rng.choice(operators)
            right = rng.choice(strings + placeholders + ['1', '2.5'])
            stmt += f' where {left}{op}{right}'
        if rng.random() < 0.5:
            stmt += ' ' + rng.choice(comments)
        if rng.random() < 0.3:
            stmt += ' order by ' + rng.choice(columns)
        cases.append(stmt)
    return cases


SEEDED_CASES = _generate_seeded_cases(SEED, 40)
CORPUS = HANDCRAFTED + SEEDED_CASES


# --- option combinations under test ---------------------------------------

OPTION_SETS = [
    pytest.param({}, id='defaults'),
    pytest.param({'keyword_case': 'upper'}, id='keyword_upper'),
    pytest.param({'keyword_case': 'lower'}, id='keyword_lower'),
    pytest.param({'identifier_case': 'upper'}, id='identifier_upper'),
    pytest.param({'identifier_case': 'lower'}, id='identifier_lower'),
    pytest.param({'strip_whitespace': True}, id='strip_whitespace'),
    pytest.param({'reindent': True}, id='reindent'),
    pytest.param({'reindent': True, 'indent_width': 4, 'indent_tabs': True},
                 id='reindent_tabs'),
    pytest.param({'reindent_aligned': True}, id='reindent_aligned'),
    pytest.param({'reindent': True, 'indent_columns': True},
                 id='indent_columns'),
    pytest.param({'reindent': True, 'comma_first': True}, id='comma_first'),
    pytest.param({'reindent': True, 'compact': True}, id='compact'),
    pytest.param({'reindent': True, 'indent_after_first': True},
                 id='indent_after_first'),
    pytest.param({'reindent': True, 'wrap_after': 2}, id='wrap_after'),
    pytest.param({'use_space_around_operators': True}, id='space_operators'),
    pytest.param({'strip_comments': True}, id='strip_comments'),
    # NOTE: right_margin is validated but its filter raises
    # NotImplementedError, so it cannot be part of the exercised surface.
    pytest.param({'keyword_case': 'upper', 'identifier_case': 'lower',
                  'reindent': True, 'use_space_around_operators': True},
                 id='kitchen_sink'),
    pytest.param({'keyword_case': 'lower', 'reindent_aligned': True,
                  'strip_comments': True}, id='aligned_strip_comments'),
]


def _case_id(sql):
    return f'case{CORPUS.index(sql):03d}'


def _is_multistatement(sql):
    return len(sqlparse.parse(sql)) > 1


def _idempotency_xfail(sql, options):
    """Known, pre-existing formatter idempotency defects.

    Marked with strict xfail so a fix turns the test into an explicit
    XPASS failure (reminding us to drop the marker), while the content
    conservation checks above keep asserting unconditionally -- these
    layout bugs never corrupt semantic tokens.
    """
    if options.get('reindent') and _is_multistatement(sql):
        return pytest.mark.xfail(
            strict=True,
            reason='ReindentFilter accumulates an extra blank line between '
                   'statements on every formatting pass')
    if (options.get('reindent_aligned') and options.get('strip_comments')
            and sql == HANDCRAFTED[5]):
        return pytest.mark.xfail(
            strict=True,
            reason='AlignedIndentFilter leaves a space before a stripped '
                   'comment that sat before a comma; a second pass removes it')
    return None


class TestContentConservation:
    """Formatting must never alter semantic content."""

    @pytest.mark.parametrize('options', OPTION_SETS)
    @pytest.mark.parametrize('sql', CORPUS, ids=_case_id)
    def test_semantic_tokens_conserved(self, sql, options):
        before = expected_tokens(sql, options)
        formatted = sqlparse.format(sql, **options)
        after = actual_tokens(formatted, options)
        errors = conservation_errors(before, after)
        if errors:
            path = dump_failure(sql, options, before, after, errors)
            pytest.fail(
                'formatting changed semantic content '
                f'(regression example dumped to {path}):\n'
                + '\n\n'.join(errors))

    @pytest.mark.parametrize('options', OPTION_SETS)
    @pytest.mark.parametrize('sql', CORPUS, ids=_case_id)
    def test_formatting_is_idempotent(self, sql, options, request):
        marker = _idempotency_xfail(sql, options)
        if marker is not None:
            request.node.add_marker(marker)
        once = sqlparse.format(sql, **options)
        twice = sqlparse.format(once, **options)
        assert once == twice, (
            'formatting is not idempotent:\n'
            f'  first : {once!r}\n'
            f'  second: {twice!r}')


# --- mutation tests: prove the checks actually alarm ----------------------
#
# Each mutation injects a preprocess filter that deliberately damages the
# token stream the way a broken formatter would.  The conservation check
# must report every one of them; a green mutation test proves the check is
# not vacuously passing.


class _CorruptFilter:
    """Preprocess filter applying a deliberately destructive transform."""

    def __init__(self, corrupt):
        self.corrupt = corrupt

    def process(self, stream):
        for ttype, value in stream:
            yield from self.corrupt(ttype, value)


def _split_arrow_operator(ttype, value):
    # token boundary: break the multi-char operator '->>' into '-' + '>'
    if ttype in T.Operator and value == '->>':
        yield T.Operator, '-'
        yield T.Operator.Comparison, '>'
    else:
        yield ttype, value


def _make_merge_comparison():
    # token boundary: merge adjacent '>' '=' into a single '>='
    pending = False
    held_ws = []

    def corrupt(ttype, value):
        nonlocal pending, held_ws
        if pending:
            if ttype in T.Whitespace:
                held_ws.append((ttype, value))
                return
            pending = False
            if ttype in T.Operator and value == '=':
                held_ws = []
                yield T.Operator.Comparison, '>='
                return
            yield T.Operator.Comparison, '>'
            for ws_ttype, ws_value in held_ws:
                yield ws_ttype, ws_value
            held_ws = []
        if ttype in T.Operator and value == '>':
            pending = True
            held_ws = []
            return
        yield ttype, value
    return corrupt


def _rewrite_string_literal(ttype, value):
    # literal damage: hollow out string contents, keep the quotes
    if ttype == T.Literal.String.Single and len(value) > 2:
        yield ttype, value[0] + 'X' + value[-1]
    else:
        yield ttype, value


def _mutate_comment_text(ttype, value):
    # semantic comment damage: silently edit the comment body
    if ttype in T.Comment:
        if value.endswith('\n'):
            value = value[:-1] + ' tampered\n'
        else:
            value = value + ' tampered'
    yield ttype, value


def _rename_placeholder(ttype, value):
    # placeholder damage: rewrite the bound parameter name
    if ttype == T.Name.Placeholder and value.startswith(':'):
        yield ttype, value + '_x'
    else:
        yield ttype, value


MUTATIONS = [
    pytest.param('select meta->>data from t', _split_arrow_operator,
                 CATEGORY_OPERATOR,
                 id='split_multi_char_operator'),
    pytest.param('select a from t where a > = 1', _make_merge_comparison(),
                 CATEGORY_OPERATOR,
                 id='merge_adjacent_operators'),
    pytest.param("select 'precious payload' from t", _rewrite_string_literal,
                 CATEGORY_STRING,
                 id='rewrite_string_literal'),
    pytest.param('select a -- semantic note\nfrom t', _mutate_comment_text,
                 CATEGORY_COMMENT,
                 id='mutate_comment_text'),
    pytest.param('select * from t where id = :bind', _rename_placeholder,
                 CATEGORY_PLACEHOLDER,
                 id='rename_placeholder'),
]


class TestMutationsAlarm:
    """Feed deliberately corrupted formatting into the checker."""

    @pytest.mark.parametrize('sql,corrupt,expected_category', MUTATIONS)
    @pytest.mark.parametrize('options', [{}, {'reindent': True}],
                             ids=['defaults', 'reindent'])
    def test_corruption_is_detected(self, monkeypatch, sql, corrupt,
                                    expected_category, options):
        real_build = build_filter_stack

        def patched(stack, opts):
            stack = real_build(stack, opts)
            stack.preprocess.append(_CorruptFilter(corrupt))
            return stack

        monkeypatch.setattr('sqlparse.formatter.build_filter_stack', patched)
        before = expected_tokens(sql, options)
        after = actual_tokens(sqlparse.format(sql, **options), options)
        errors = conservation_errors(before, after)
        assert errors, 'corrupted formatting went undetected'
        # the independent category check must point at the damaged construct
        assert any(expected_category in err for err in errors), (
            f'expected a {expected_category!r} violation, got: {errors}')

    def test_naive_whitespace_compare_is_blind(self):
        # Justifies the token-based check: stripping *all* whitespace cannot
        # see a space deleted inside a string literal, quoted identifier or
        # comment, because it deletes the evidence together with layout.
        original = "select 'two words', \"q id\" from t -- two words\n"
        damaged = original.replace('two words', 'twowords') \
                          .replace('q id', 'qid')
        naive = lambda s: ''.join(s.split())
        assert naive(original) == naive(damaged)  # blind spot demonstrated
        errors = conservation_errors(expected_tokens(original, {}),
                                     actual_tokens(damaged, {}))
        assert errors, 'token-based check must see literal damage'


# --- corpus completeness guard --------------------------------------------

class TestCorpusCoverage:
    """The corpus must actually exercise every checked category."""

    def test_all_categories_present(self):
        seen = {rec[0] for sql in CORPUS for rec in semantic_tokens(sql)}
        assert {CATEGORY_STRING, CATEGORY_QUOTED_ID, CATEGORY_PLACEHOLDER,
                CATEGORY_COMMENT, CATEGORY_OPERATOR} <= seen

    def test_corpus_is_deterministic(self):
        assert _generate_seeded_cases(SEED, 40) == SEEDED_CASES


# --- dialect syntax not covered yet ---------------------------------------
#
# The generic lexer models a common SQL subset.  The following dialect
# constructs are *not* part of this conservation corpus yet, mostly because
# the lexer splits them into misleading token types or does not know the
# syntax at all (conservation would still be checked token-wise, but the
# expected-token baseline would need dialect-aware canonicalization):
#
# * PostgreSQL dollar-quoted strings ($$...$$, $tag$...$tag$) and E'...'
#   escape strings -- lexed as multiple tokens, not one string literal.
# * MySQL/Oracle string prefixes beyond the generic ones (N'...', x'...'
#   lex as Name + String pairs) and charset introducers (_utf8'...').
# * T-SQL / Oracle variables and parameters: @var, @@ident, &var, &&var --
#   lexed as Operator/Name pairs, not placeholders.
# * PostgreSQL casts (expr::type), array slices (a[1:2]), JSON operators
#   beyond ->> (#>, @>, <@, ?|, ?&) and positional params mixed with them.
# * Quoted-identifier styles: dialect-specific escapes inside "..." and
#   `...` (e.g. backslash escapes in MySQL strings) are not unescaped.
# * Hints and pragmas: Oracle /*+ hint */, MySQL /*!40101 ... */ executable
#   comments -- lexed as plain comments, hint semantics unchecked.
# * BigQuery/Snowflake specifics: struct fields, QUALIFY, ILIKE, $$...$$
#   UDF bodies, semi-structured access (a:b:c).
# * Statement-level constructs: PL/pgSQL bodies, BEGIN...END blocks,
#   CTE...SELECT with recursive terms -- only simple forms are generated.
UNCOVERED_DIALECT_SYNTAX = [
    'postgres_dollar_quoted_strings',
    'postgres_cast_and_json_operators',
    'mysql_charset_introducers_and_backslash_escapes',
    'tsql_oracle_variables_at_ampersand',
    'oracle_mysql_hint_comments',
    'bigquery_snowflake_semi_structured_access',
    'plpgsql_and_begin_end_blocks',
]
