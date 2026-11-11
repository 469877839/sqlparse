"""Tests for fmt: off / fmt: on protected regions."""

import pytest

import sqlparse
from sqlparse import protected
from sqlparse.exceptions import SQLParseError

OFF = '-- fmt: off'
ON = '-- fmt: on'


def region_of(text):
    """Return the text from the off marker through the on marker."""
    start = text.index('-- fmt: off')
    end = text.index('-- fmt: on') + len('-- fmt: on')
    return text[start:end]


class TestMarkerDetection:
    def test_scan_regions_offsets(self):
        sql = f"select 1\n{OFF}\nselect   2\n{ON}\n"
        regions = protected.scan_regions(sql)
        assert len(regions) == 1
        start, end, balanced = regions[0]
        assert balanced is True
        # the comment token keeps its trailing line break inside the region
        assert sql[start:end] == f"{OFF}\nselect   2\n{ON}\n"
        assert sql[end:] == ''

    def test_no_regions_without_markers(self):
        assert protected.scan_regions('select 1') == []

    def test_string_literal_is_not_a_marker(self):
        sql = "select '-- fmt: off' as x,   y from t"
        out = sqlparse.format(sql, reindent=True)
        assert '-- fmt: off' in out
        # the code around the literal is formatted normally
        assert out == "select '-- fmt: off' as x,\n       y\nfrom t"

    def test_block_comment_is_not_a_marker(self):
        sql = '/* fmt: off */ select   x   from t'
        out = sqlparse.format(sql, reindent=True)
        assert out == '/* fmt: off */\nselect x\nfrom t'

    def test_directive_inside_code_line_is_not_a_marker(self):
        sql = 'select 1; -- fmt: off\nselect   2'
        out = sqlparse.format(sql, keyword_case='upper')
        assert out == 'SELECT 1; -- fmt: off\nSELECT   2'

    def test_indented_marker_is_standalone(self):
        sql = f'select 1\n  {OFF}\nselect   2\n  {ON}\n'
        out = sqlparse.format(sql, keyword_case='upper')
        assert out == f'SELECT 1\n  {OFF}\nselect   2\n  {ON}\n'

    def test_hash_comment_marker(self):
        sql = '# fmt: off\nselect   x\n# fmt: on\nselect y'
        out = sqlparse.format(sql, keyword_case='upper')
        assert out == '# fmt: off\nselect   x\n# fmt: on\nSELECT y'

    def test_marker_without_space_after_comment_prefix(self):
        sql = '--fmt: off\nselect   x\n--fmt: on\n'
        out = sqlparse.format(sql, keyword_case='upper')
        assert out == '--fmt: off\nselect   x\n--fmt: on\n'

    @pytest.mark.parametrize('payload', [
        '-- fmt: off please',
        '-- fmt: offx',
        '-- fmt: on extra',
        '-- FMT: OFF',
        '-- format: off',
    ])
    def test_non_matching_payloads(self, payload):
        sql = f'{payload}\nselect   x'
        out = sqlparse.format(sql, keyword_case='upper')
        assert out == f'{payload}\nSELECT   x'


class TestRegionPreservation:
    def test_region_verbatim_outside_formatted(self):
        sql = f'select a,b from t\n{OFF}\nwhere   x   =   1\n{ON}\nand z=2\n'
        out = sqlparse.format(sql, reindent=True)
        assert region_of(out) == f'{OFF}\nwhere   x   =   1\n{ON}'
        assert out.startswith('select a,\n       b\nfrom t\n')
        assert out.endswith('and z=2')

    def test_region_with_trailing_whitespace_and_multiple_spaces(self):
        line = 'select   a ,\tb   from   t   '
        sql = f'select 1\n{OFF}\n{line}\n{ON}\nselect   2'
        out = sqlparse.format(sql, reindent=True)
        assert f'{OFF}\n{line}\n{ON}' in out

    def test_markers_are_preserved(self):
        sql = f'{OFF}\nselect   x\n{ON}\n'
        out = sqlparse.format(sql, keyword_case='upper')
        assert out == sql

    def test_strip_comments_keeps_region(self):
        sql = f'select   1\n{OFF}\n-- keep me\nselect   2\n{ON}\n'
        assert sqlparse.format(sql, strip_comments=True) == sql

    def test_strip_whitespace_keeps_region(self):
        sql = f'select a,b from t\n{OFF}\nwhere   x   =   1\n{ON}\nand z=2\n'
        out = sqlparse.format(sql, strip_whitespace=True)
        assert region_of(out) == f'{OFF}\nwhere   x   =   1\n{ON}'
        assert out == (
            f'select a,b from t\n{OFF}\nwhere   x   =   1\n'
            f'{ON}\nand z=2')

    def test_identifier_case_keeps_region(self):
        sql = f'{OFF}\nselect FooBar\n{ON}\nselect Baz'
        out = sqlparse.format(sql, identifier_case='upper')
        assert out == f'{OFF}\nselect FooBar\n{ON}\nselect BAZ'

    def test_truncate_strings_keeps_region(self):
        sql = f"{OFF}\nselect 'abcdefghij'\n{ON}\nselect 'abcdefghij'"
        out = sqlparse.format(sql, truncate_strings=5)
        assert out == f"{OFF}\nselect 'abcdefghij'\n{ON}\nselect 'abcde[...]'"

    def test_spaces_around_operators_keeps_region(self):
        sql = f'select a+1\n{OFF}\nselect b+2\n{ON}\n'
        out = sqlparse.format(sql, use_space_around_operators=True)
        assert out == f'select a + 1\n{OFF}\nselect b+2\n{ON}\n'

    def test_aligned_indent_keeps_region(self):
        sql = f'select a,b from t\n{OFF}\nwhere   x   =   1\n{ON}\nand z=2\n'
        out = sqlparse.format(sql, reindent_aligned=True)
        assert region_of(out) == f'{OFF}\nwhere   x   =   1\n{ON}'

    def test_region_inside_parenthesis_keeps_statement_context(self):
        sql = f'select   *   from (\n{OFF}\nselect x from t\n{ON}\n) as sub'
        out = sqlparse.format(sql, reindent=True)
        assert region_of(out) == f'{OFF}\nselect x from t\n{ON}'
        assert out == (
            f'select *\nfrom (\n{OFF}\nselect x from t\n'
            f'{ON}\n) as sub')

    def test_region_spanning_statements(self):
        sql = (f'{OFF}\ninsert into t values (1);\n'
               f'insert into t values (2);\n{ON}\nselect   x from y')
        out = sqlparse.format(sql, reindent=True)
        assert region_of(out) == (
            f'{OFF}\ninsert into t values (1);\n'
            f'insert into t values (2);\n{ON}')
        assert out.endswith('\n\nselect x\nfrom y')


class TestNewlineHandling:
    def test_crlf_inside_region_is_kept(self):
        sql = ('select\r\nx\r\n-- fmt: off\r\nselect   y,\r\nz\r\n'
               '-- fmt: on\r\nfrom t\r\n')
        out = sqlparse.format(sql, reindent=True)
        start = out.index('-- fmt: off')
        end = out.index('-- fmt: on') + len('-- fmt: on')
        assert out[start:end] == '-- fmt: off\r\nselect   y,\r\nz\r\n-- fmt: on'
        # outside the region line breaks are normalized to LF
        assert '\r' not in out[:start]

    def test_cr_inside_region_is_kept(self):
        sql = 'select   x\r-- fmt: off\rselect   y\r-- fmt: on\rselect   z'
        out = sqlparse.format(sql, reindent=True)
        start = out.index('-- fmt: off')
        end = out.index('-- fmt: on') + len('-- fmt: on')
        assert out[start:end] == '-- fmt: off\rselect   y\r-- fmt: on'

    def test_region_at_start_of_input(self):
        sql = f'{OFF}\nselect   x\n{ON}\nselect   y'
        out = sqlparse.format(sql, reindent=True)
        assert out.startswith(f'{OFF}\nselect   x\n{ON}')


class TestInvalidPairing:
    def test_unclosed_region_runs_to_eof(self):
        sql = f'select   x from t\n{OFF}\nselect   y   from z\n'
        out = sqlparse.format(sql, keyword_case='upper')
        assert out == f'SELECT   x FROM t\n{OFF}\nselect   y   from z\n'

    def test_on_without_off_is_ordinary_comment(self):
        sql = f'{ON}\nselect   x from t'
        out = sqlparse.format(sql, keyword_case='upper')
        assert out == f'{ON}\nSELECT   x FROM t'

    def test_nested_off_does_not_close_early(self):
        sql = (f'{OFF}\nselect   a,\n{OFF}\n{ON}\n b from t;\n'
               f'{ON}\nselect z')
        out = sqlparse.format(sql, keyword_case='upper')
        assert out == (
            f'{OFF}\nselect   a,\n{OFF}\n{ON}\n b from t;\n'
            f'{ON}\nSELECT z')

    def test_extra_on_is_ordinary_comment(self):
        sql = f'select   1\n{ON}\n{ON}\nselect   2'
        out = sqlparse.format(sql, keyword_case='upper')
        assert out == f'SELECT   1\n{ON}\n{ON}\nSELECT   2'

    def test_empty_region(self):
        sql = f'select 1\n{OFF}\n{ON}\nselect   2'
        out = sqlparse.format(sql, reindent=True)
        assert out.startswith(f'select 1\n{OFF}\n{ON}')


class TestIdempotency:
    @pytest.mark.parametrize('options', [
        {'reindent': True},
        {'reindent_aligned': True},
        {'strip_whitespace': True},
        {'keyword_case': 'upper'},
        {'identifier_case': 'upper'},
        {'strip_comments': True},
        {'use_space_around_operators': True},
        {'reindent': True, 'comma_first': True},
        {'reindent': True, 'compact': True},
    ])
    def test_repeated_formatting_is_stable(self, options):
        sql = (f'select a,b from t\n{OFF}\nwhere   x   =   1\n'
               f'select   y from z;\n{ON}\nand z=2\n')
        once = sqlparse.format(sql, **options)
        twice = sqlparse.format(once, **options)
        assert once == twice

    def test_region_text_stable_across_reindent(self):
        sql = f'select a,b from t\n{OFF}\nwhere   x   =   1\n{ON}\nand z=2\n'
        once = sqlparse.format(sql, reindent=True)
        twice = sqlparse.format(once, reindent=True)
        assert region_of(once) == region_of(twice) == (
            f'{OFF}\nwhere   x   =   1\n{ON}')


class TestOptionHandling:
    def test_keep_fmt_regions_disabled(self):
        sql = f'{OFF}\nselect   x\n{ON}\nselect   y'
        out = sqlparse.format(
            sql, keyword_case='upper', keep_fmt_regions=False)
        assert out == f'{OFF}\nSELECT   x\n{ON}\nSELECT   y'

    def test_keep_fmt_regions_invalid_value(self):
        with pytest.raises(SQLParseError):
            sqlparse.format('select 1', keep_fmt_regions='yes')

    def test_without_markers_behaves_as_before(self):
        sql = 'select a,b from t\nwhere   x   =   1\nand z=2'
        assert sqlparse.format(sql, reindent=True) == (
            'select a,\n       b\nfrom t\nwhere x = 1\n  and z=2')

    def test_removing_markers_restores_plain_formatting(self):
        with_markers = f'select a,b from t\n{OFF}\nwhere   x   =   1\n{ON}\n'
        plain = with_markers.replace(f'{OFF}\n', '').replace(f'{ON}\n', '')
        assert sqlparse.format(plain, reindent=True) == (
            'select a,\n       b\nfrom t\nwhere x = 1')


class TestAdditionalInteractions:
    def test_region_inside_select_list(self):
        sql = f'select a,\n{OFF}\n  weird_col   ,  x\n{ON}\nfrom t'
        out = sqlparse.format(sql, reindent=True)
        assert region_of(out) == f'{OFF}\n  weird_col   ,  x\n{ON}'
        assert sqlparse.format(out, reindent=True) == out

    def test_region_covering_where_clause(self):
        sql = f'select x from t\n{OFF}\nwhere a=1   and b=2\n{ON}\norder by x'
        out = sqlparse.format(sql, reindent=True)
        assert region_of(out) == f'{OFF}\nwhere a=1   and b=2\n{ON}'
        assert sqlparse.format(out, reindent=True) == out

    def test_unclosed_region_at_eof_without_newline(self):
        sql = f'select   1\n{OFF}\nselect   2'
        out = sqlparse.format(sql, reindent=True)
        assert out == f'select 1\n{OFF}\nselect   2'
        assert sqlparse.format(out, reindent=True) == out

    def test_strip_comments_with_unclosed_region(self):
        sql = f'select   1\n{OFF}\n-- keep\nselect   2'
        assert sqlparse.format(sql, strip_comments=True) == sql

    def test_strip_comments_removes_only_outside(self):
        sql = f'-- remove me\nselect   1\n{OFF}\n-- keep me\nselect   2\n{ON}\n'
        out = sqlparse.format(sql, strip_comments=True)
        assert '-- keep me' in out
        assert 'remove me' not in out

    def test_parse_is_not_affected(self):
        sql = f'select 1;\n{OFF}\nselect 2;\n{ON}\nselect 3;'
        stmts = sqlparse.parse(sql)
        assert len(stmts) == 3
        # trailing comments attach to the following statement
        assert str(stmts[1]) == f'\n{OFF}\nselect 2;'
        assert str(stmts[2]) == f'\n{ON}\nselect 3;'

    def test_split_is_not_affected(self):
        sql = f'select 1;\n{OFF}\nselect 2;\n{ON}\nselect 3;'
        assert len(sqlparse.split(sql)) == 3
