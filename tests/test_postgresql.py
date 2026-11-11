# Tests for the PostgreSQL script mode (dialect='postgresql').

import pytest

import sqlparse
from sqlparse import tokens as T
from sqlparse.engine import postgresql


# A short script with two COPY blocks and an ordinary statement in
# between.  The data lines contain everything that must not be
# interpreted as SQL: semicolons, quotes, comment-looking text and
# escaped backslashes.
SCRIPT = (
    "CREATE TABLE t (a int, b text);\n"
    "COPY t (a, b) FROM STDIN;\n"
    "1\thello; world\n"
    "2\t'quoted' -- not a comment\n"
    "3\tbackslash\\\\dot\n"
    "4\t/* not a comment either */\n"
    "\\.\n"
    "SELECT * FROM t;\n"
    "COPY t2 (x) FROM STDIN WITH (FORMAT csv);\n"
    "a;b\n"
    "'c'\n"
    "\\.\n"
    "SELECT 42;"
)


def test_copy_blocks_roundtrip_character_by_character():
    stmts = sqlparse.parse(SCRIPT, dialect='postgresql')
    assert len(stmts) == 5
    # Concatenating the split pieces must reproduce the input exactly.
    assert ''.join(str(stmt) for stmt in stmts) == SCRIPT


def test_copy_block_delivered_as_single_raw_statement():
    stmts = sqlparse.parse(SCRIPT, dialect='postgresql')
    copy1, copy2 = stmts[1], stmts[3]
    # header, raw data and terminator form one statement ...
    assert str(copy1) == (
        "\nCOPY t (a, b) FROM STDIN;\n"
        "1\thello; world\n"
        "2\t'quoted' -- not a comment\n"
        "3\tbackslash\\\\dot\n"
        "4\t/* not a comment either */\n"
        "\\.\n"
    )
    assert str(copy2) == (
        "\nCOPY t2 (x) FROM STDIN WITH (FORMAT csv);\n"
        "a;b\n"
        "'c'\n"
        "\\.\n"
    )
    # ... whose content is not tokenized (field values are not parsed).
    assert copy1.tokens[-1].ttype is T.Other
    assert copy2.tokens[-1].ttype is T.Other


def test_copy_data_may_look_like_sql():
    sql = (
        "COPY t FROM STDIN;\n"
        "1\tSELECT * FROM t;\n"
        "2\tDROP TABLE t;\n"
        "\\.\n"
        "SELECT 1;"
    )
    stmts = sqlparse.parse(sql, dialect='postgresql')
    assert len(stmts) == 2
    assert ''.join(str(stmt) for stmt in stmts) == sql


@pytest.mark.parametrize('terminator', ['\\.\n', '\\.\r\n', '\\.\r', '\\.'])
def test_terminator_line_endings(terminator):
    sql = 'COPY t FROM STDIN;\n1\tx\n' + terminator + 'SELECT 1;'
    stmts = sqlparse.parse(sql, dialect='postgresql')
    assert len(stmts) == 2
    assert ''.join(str(stmt) for stmt in stmts) == sql


@pytest.mark.parametrize('line', [
    ' \\.',      # leading whitespace
    '\\. ',      # trailing whitespace
    '\\..',      # extra character
    'x\\.',      # not at line start
    '\\.\\.',    # doubled marker
])
def test_terminator_must_be_alone_on_its_line(line):
    sql = f'COPY t FROM STDIN;\n{line}\n\\.\nSELECT 1;'
    stmts = sqlparse.parse(sql, dialect='postgresql')
    assert len(stmts) == 2
    assert str(stmts[0]).endswith('\\.\n')
    assert ''.join(str(stmt) for stmt in stmts) == sql


def test_copy_to_stdout_is_ordinary_sql():
    sql = 'COPY t TO STDOUT;\nSELECT 1;'
    stmts = sqlparse.parse(sql, dialect='postgresql')
    assert [str(stmt) for stmt in stmts] == ['COPY t TO STDOUT;',
                                             '\nSELECT 1;']
    assert all(t.ttype is not T.Other for stmt in stmts
               for t in stmt.flatten())


def test_from_stdin_inside_string_is_ordinary_sql():
    sql = "SELECT 'COPY t FROM STDIN';\nSELECT 2;"
    stmts = sqlparse.parse(sql, dialect='postgresql')
    assert [str(stmt) for stmt in stmts] == ["SELECT 'COPY t FROM STDIN';",
                                             '\nSELECT 2;']


def test_copy_keyword_inside_comment_is_ignored():
    sql = '-- COPY t FROM STDIN;\nSELECT 1;'
    stmts = sqlparse.parse(sql, dialect='postgresql')
    assert len(stmts) == 1
    assert str(stmts[0]) == sql


def test_semicolons_inside_header_strings_do_not_end_header():
    sql = ("COPY t FROM STDIN WHERE x = $$a;b$$ AND y = 'c;d';\n"
           "1\tx\n\\.\nSELECT 1;")
    stmts = sqlparse.parse(sql, dialect='postgresql')
    assert len(stmts) == 2
    assert str(stmts[0]).endswith('\\.\n')
    assert ''.join(str(stmt) for stmt in stmts) == sql


def test_plain_statements_split_as_usual():
    assert sqlparse.split('SELECT 1; SELECT 2;',
                          dialect='postgresql') == ['SELECT 1;', 'SELECT 2;']


def test_split_strips_and_keeps_copy_block_intact():
    stmts = sqlparse.split(SCRIPT, dialect='postgresql')
    assert len(stmts) == 5
    assert stmts[1].startswith('COPY t (a, b) FROM STDIN;')
    assert stmts[1].endswith('\\.')
    assert stmts[3].startswith('COPY t2 (x) FROM STDIN WITH (FORMAT csv);')


def test_split_strip_semicolon_leaves_copy_block_untouched():
    sql = 'COPY t FROM STDIN;\n1\tx\n\\.\nSELECT 1;'
    stmts = sqlparse.split(sql, dialect='postgresql', strip_semicolon=True)
    assert stmts == ['COPY t FROM STDIN;\n1\tx\n\\.', 'SELECT 1']


def test_unterminated_data_section_is_not_a_copy_block():
    # Explicit result for a truncated script: without the terminating
    # marker line the COPY header is treated as ordinary SQL and the
    # remaining lines are split as ordinary statements; concatenating
    # the pieces still reproduces the input.
    sql = 'SELECT 1;\nCOPY t FROM STDIN;\n1\ta\n2\tb'
    stmts = sqlparse.parse(sql, dialect='postgresql')
    assert [str(stmt) for stmt in stmts] == [
        'SELECT 1;', '\nCOPY t FROM STDIN;', '\n1\ta\n2\tb']
    assert ''.join(str(stmt) for stmt in stmts) == sql
    assert all(t.ttype is not T.Other for stmt in stmts
               for t in stmt.flatten())


def test_unterminated_header_without_semicolon():
    sql = 'COPY t FROM STDIN\n1\tx\n\\.\nSELECT 1;'
    stmts = sqlparse.parse(sql, dialect='postgresql')
    assert len(stmts) == 1
    assert ''.join(str(stmt) for stmt in stmts) == sql


def test_first_marker_terminates_first_copy_data_section():
    # A missing marker cannot be detected: the data section of the first
    # COPY statement legitimately extends to the next marker line, which
    # is exactly how PostgreSQL itself reads COPY data.
    sql = ('COPY bad FROM STDIN;\n'
           '1\tx\n'
           'COPY good FROM STDIN;\n'
           '1\ty\n'
           '\\.\n'
           'SELECT 2;')
    stmts = sqlparse.parse(sql, dialect='postgresql')
    assert len(stmts) == 2
    assert stmts[0].tokens[-1].ttype is T.Other
    assert str(stmts[0]) == (
        'COPY bad FROM STDIN;\n'
        '1\tx\n'
        'COPY good FROM STDIN;\n'
        '1\ty\n'
        '\\.\n'
    )
    assert str(stmts[1]) == 'SELECT 2;'
    assert ''.join(str(stmt) for stmt in stmts) == sql


def test_default_mode_is_unchanged():
    # Without the dialect the COPY data is still split at semicolons.
    sql = 'COPY t FROM STDIN;\n1\thello;x\n\\.\nSELECT 1;'
    assert sqlparse.split(sql) == [
        'COPY t FROM STDIN;', '1\thello;', 'x\n\\.\nSELECT 1;']


@pytest.mark.parametrize('dialect', [None, '', 'default', 'sql'])
def test_dialect_aliases_for_default_mode(dialect):
    assert sqlparse.split('SELECT 1; SELECT 2;', dialect=dialect) == [
        'SELECT 1;', 'SELECT 2;']


@pytest.mark.parametrize('dialect', ['postgres', 'PostgreSQL', 'POSTGRESQL'])
def test_dialect_aliases_for_postgresql(dialect):
    stmts = sqlparse.parse(SCRIPT, dialect=dialect)
    assert ''.join(str(stmt) for stmt in stmts) == SCRIPT


def test_unknown_dialect_raises():
    with pytest.raises(ValueError, match='Unknown dialect'):
        sqlparse.parse('SELECT 1;', dialect='mysql')
    with pytest.raises(TypeError):
        sqlparse.parse('SELECT 1;', dialect=42)


def test_parsestream_with_dialect():
    from io import StringIO
    stream = StringIO(SCRIPT)
    stmts = list(sqlparse.parsestream(stream, dialect='postgresql'))
    assert ''.join(str(stmt) for stmt in stmts) == SCRIPT


def test_splitter_yields_statements_directly():
    splitter = postgresql.PostgreSQLScriptSplitter()
    stmts = list(splitter.process(SCRIPT))
    assert ''.join(str(stmt) for stmt in stmts) == SCRIPT
