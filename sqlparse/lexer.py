#
# Copyright (C) 2009-2020 the sqlparse authors and contributors
# <see AUTHORS file>
#
# This module is part of python-sqlparse and is released under
# the BSD License: https://opensource.org/licenses/BSD-3-Clause

"""SQL Lexer"""
import re

# This code is based on the SqlLexer in pygments.
# http://pygments.org/
# It's separated from the rest of pygments to increase performance
# and to allow some customizations.
from bisect import bisect_left
from collections import defaultdict
from io import TextIOBase
from threading import Lock

from sqlparse import keywords, tokens
from sqlparse.utils import consume

# Same shapes as the string token rules in keywords.SQL_REGEX, used to
# decide whether a quote in the buffered suffix already has its closer.
# DOTALL mirrors the lexer's compiled rules, so quoted strings span lines.
_QUOTE_FLAGS = re.DOTALL
_SINGLE_QUOTE_RE = re.compile(r"'(''|\\'|[^'])*'", _QUOTE_FLAGS)
_DOUBLE_QUOTE_RE = re.compile(r'"(""|\\"|[^"])*"', _QUOTE_FLAGS)
_BACKTICK_RE = re.compile(r"`(``|[^`])*`", _QUOTE_FLAGS)
# Placeholder rule whose lookbehind depends on the character before the
# buffer, which the regex engine cannot see across chunk boundaries.
_PLACEHOLDER_RULE = r'(?<!\w)[$:?]\w+'
# Name rule anchored on a preceding period; the lookbehind must see the
# character delivered just before the buffer.
_DOT_NAME_RULE = r'(?<=\.)[A-ZÀ-Ü]\w*'
# Same rule without the lookbehind, applied when the period was already
# delivered as a token before the current buffer.
_DOT_NAME_NO_LOOKBEHIND = re.compile(
    r'[A-ZÀ-Ü]\w*', re.IGNORECASE | re.UNICODE)
# Bracket-quoted name rule with the same cross-boundary lookbehind need.
_BRACKET_RULE = r'(?<![\w\])])(\[[^\]\[]+\])'


# Multi-word rules whose later words may arrive in a separate chunk must
# keep their first word provisional. Maps the first word (lower case) to
# the rule's pattern, so the check does not depend on the buffered prefix
# already being a complete match.
_JOIN_PATTERN = (
    r'((LEFT\s+|RIGHT\s+|FULL\s+)?(INNER\s+|OUTER\s+|STRAIGHT\s+)?|'
    r'(CROSS\s+|NATURAL\s+)?)?JOIN\b')
_WHITESPACE_RULE_PREFIXES = {
    'union': r'UNION\s+ALL\b',
    'order': r'ORDER\s+BY\b',
    'group': r'GROUP\s+BY\b',
    'double': r'DOUBLE\s+PRECISION\b',
    'primary': r'PRIMARY\s+KEY\b',
    'handler': r'HANDLER\s+FOR\b',
    'nulls': r'NULLS\s+(FIRST|LAST)\b',
    'create': r'CREATE(\s+OR\s+REPLACE)?\b',
    'end': r'END(\s+IF|\s+LOOP|\s+WHILE|\s+FOR|\s+CASE)?\b',
    'if': r'IF\s+(NOT\s+)?EXISTS\b',
    'not': r'NOT\s+NULL\b',
    'asc': r'(ASC|DESC)(\s+NULLS\s+(FIRST|LAST))?\b',
    'desc': r'(ASC|DESC)(\s+NULLS\s+(FIRST|LAST))?\b',
    'go': r'GO(\s\d+)\b',
    'like': r'(NOT\s+)?(LIKE|ILIKE|RLIKE)\b',
    'ilike': r'(NOT\s+)?(LIKE|ILIKE|RLIKE)\b',
    'rlike': r'(NOT\s+)?(LIKE|ILIKE|RLIKE)\b',
    'regexp': r'(NOT\s+)?(REGEXP)(\s+(BINARY))?\b',
    'lateral': r'(LATERAL\s+VIEW\s+)(EXPLODE|INLINE|PARSE_URL_TUPLE|'
               r'POSEXPLODE|STACK)\b',
    'at': r"(AT|WITH')\s+TIME\s+ZONE\s+'[^']+'",
    "with'": r"(AT|WITH')\s+TIME\s+ZONE\s+'[^']+'",
}
for _first in ('left', 'right', 'full', 'inner', 'outer', 'straight',
               'cross', 'natural', 'join'):
    _WHITESPACE_RULE_PREFIXES[_first] = _JOIN_PATTERN


class Lexer:
    """The Lexer supports configurable syntax.
    To add support for additional keywords, use the `add_keywords` method."""

    _default_instance = None
    _lock = Lock()

    # Development notes:
    # - This class is prepared to be able to support additional SQL dialects
    #   in the future by adding additional functions that take the place of
    #   the function default_initialization().
    # - The lexer class uses an explicit singleton behavior with the
    #   instance-getter method get_default_instance(). This mechanism has
    #   the advantage that the call signature of the entry-points to the
    #   sqlparse library are not affected. Also, usage of sqlparse in third
    #   party code does not need to be adapted. On the other hand, the current
    #   implementation does not easily allow for multiple SQL dialects to be
    #   parsed in the same process.
    #   Such behavior can be supported in the future by passing a
    #   suitably initialized lexer object as an additional parameter to the
    #   entry-point functions (such as `parse`). Code will need to be written
    #   to pass down and utilize such an object. The current implementation
    #   is prepared to support this thread safe approach without the
    #   default_instance part needing to change interface.

    @classmethod
    def get_default_instance(cls):
        """Returns the lexer instance used internally
        by the sqlparse core functions."""
        with cls._lock:
            if cls._default_instance is None:
                cls._default_instance = cls()
                cls._default_instance.default_initialization()
        return cls._default_instance

    def default_initialization(self):
        """Initialize the lexer with default dictionaries.
        Useful if you need to revert custom syntax settings."""
        self.clear()
        self.set_SQL_REGEX(keywords.SQL_REGEX)
        self.add_keywords(keywords.KEYWORDS_COMMON)
        self.add_keywords(keywords.KEYWORDS_ORACLE)
        self.add_keywords(keywords.KEYWORDS_MYSQL)
        self.add_keywords(keywords.KEYWORDS_PLPGSQL)
        self.add_keywords(keywords.KEYWORDS_HQL)
        self.add_keywords(keywords.KEYWORDS_MSACCESS)
        self.add_keywords(keywords.KEYWORDS_SNOWFLAKE)
        self.add_keywords(keywords.KEYWORDS_BIGQUERY)
        self.add_keywords(keywords.KEYWORDS)

    def clear(self):
        """Clear all syntax configurations.
        Useful if you want to load a reduced set of syntax configurations.
        After this call, regexps and keyword dictionaries need to be loaded
        to make the lexer functional again."""
        self._SQL_REGEX = []
        self._keywords = []

    def set_SQL_REGEX(self, SQL_REGEX):
        """Set the list of regex that will parse the SQL."""
        FLAGS = re.IGNORECASE | re.UNICODE
        self._SQL_REGEX = [
            (re.compile(rx, FLAGS).match, tt)
            for rx, tt in SQL_REGEX
        ]

    def add_keywords(self, keywords):
        """Add keyword dictionaries. Keywords are looked up in the same order
        that dictionaries were added."""
        self._keywords.append(keywords)

    def is_keyword(self, value):
        """Checks for a keyword.

        If the given value is in one of the KEYWORDS_* dictionary
        it's considered a keyword. Otherwise, tokens.Name is returned.
        """
        val = value.upper()
        for kwdict in self._keywords:
            if val in kwdict:
                return kwdict[val], value
        else:
            return tokens.Name, value

    def get_tokens(self, text, encoding=None):
        """
        Return an iterable of (tokentype, value) pairs generated from
        `text`. If `unfiltered` is set to `True`, the filtering mechanism
        is bypassed even if filters are defined.

        Also preprocess the text, i.e. expand tabs and strip it if
        wanted and applies registered filters.

        Split ``text`` into (tokentype, text) pairs.

        ``stack`` is the initial stack (default: ``['root']``)
        """
        if isinstance(text, TextIOBase):
            text = text.read()

        if isinstance(text, str):
            pass
        elif isinstance(text, bytes):
            if encoding:
                text = text.decode(encoding)
            else:
                try:
                    text = text.decode('utf-8')
                except UnicodeDecodeError:
                    text = text.decode('unicode-escape')
        else:
            raise TypeError(f"Expected text or file-like object, got {type(text)!r}")

        delimited_spans = keywords.find_delimited_spans(text)
        span_openers = delimited_spans.openers

        iterable = enumerate(text)
        for pos, char in iterable:
            # Only positions the lexer actually reaches may open a
            # dollar-quoted literal or a multiline comment; a delimiter
            # inside a string literal or behind a "--" comment is skipped
            # along with its surrounding token and never resolved.
            if pos in span_openers:
                resolved = delimited_spans.resolve(pos)
                if resolved is not None:
                    end, ttype = resolved
                    yield ttype, text[pos:end]
                    consume(iterable, end - pos - 1)
                    continue

            for rexmatch, action in self._SQL_REGEX:
                m = rexmatch(text, pos)

                if not m:
                    continue
                elif isinstance(action, tokens._TokenType):
                    yield action, m.group()
                elif action is keywords.PROCESS_AS_KEYWORD:
                    yield self.is_keyword(m.group())

                consume(iterable, m.end() - pos - 1)
                break
            else:
                yield tokens.Error, char


def tokenize(sql, encoding=None):
    """Tokenize sql.

    Tokenize *sql* using the :class:`Lexer` and return a 2-tuple stream
    of ``(token type, value)`` items.
    """
    return Lexer.get_default_instance().get_tokens(sql, encoding)


class IncrementalLexer:
    """Stateful lexer for text arriving in chunks.

    Feed :meth:`feed` with the chunks of a script in arrival order and
    iterate the instance for ``(ttype, value)`` pairs; call :meth:`close`
    (or let the instance be garbage collected) once the input is done.

    Only the suffix that cannot be classified yet is buffered: a chunk
    boundary may fall inside an escape, a two-character line comment
    (``--`` / ``# ``), a multiline comment or a dollar-quoted literal, and
    such a prefix is withheld until the next chunk -- or :meth:`close` --
    settles it. The emitted token sequence is identical to what
    :meth:`Lexer.get_tokens` produces for the concatenated text.
    """

    def __init__(self, lexer=None):
        self._lexer = lexer or Lexer.get_default_instance()
        self._buffer = ''
        # Span index over the buffered text; positions are relative to
        # the buffer and shift when completed prefixes are dropped.
        self._openers = {}
        self._closers = defaultdict(lambda: ([], []))
        self._closed = False
        # Last character already delivered, needed to decide whether a
        # held "$" at the start of the buffer can still open a dollar
        # quote (a word character directly before it forbids that).
        self._prev_char = ''
        # Family of the last delivered token: "word", "operator" or "".
        # A buffered token continuing that family must not be split off.
        self._prev_family = ''

    def feed(self, chunk):
        """Append one text chunk and yield every token that can already be
        decided. Empty chunks yield nothing."""
        if self._closed:
            raise ValueError('cannot feed a closed incremental lexer')
        if not isinstance(chunk, str):
            raise TypeError(
                f'Expected str chunk, got {type(chunk)!r}')
        if not chunk:
            return
        prev_len = len(self._buffer)
        self._buffer += chunk
        # Only positions at or behind the new suffix can be *new* openers;
        # the scan itself re-walks the whole buffered text so a delimiter
        # that straddles the chunk boundary is never missed.
        keywords.find_delimited_spans(
            self._buffer, start=prev_len,
            openers=self._openers, closers=self._closers)
        # A prefix withheld from the previous chunk (e.g. "/" or "$tag")
        # may now form a complete opener starting before the new region.
        self._index_pending_openers()
        yield from self._drain(final=False)

    def close(self):
        """Settle the remaining buffered text with no further input.

        Prefixes that would have waited for another chunk are resolved the
        same way the one-shot lexer resolves a truncated tail, e.g. an
        unclosed quote becomes an :data:`~sqlparse.tokens.Error` token."""
        if self._closed:
            return
        self._closed = True
        self._index_pending_openers()
        yield from self._drain(final=True)

    def _drain(self, final):
        buffer = self._buffer
        pos = 0
        length = len(buffer)
        safe_end = length if final else self._safe_end(buffer)
        while pos < safe_end:
            if pos in self._openers:
                end, ttype = self._resolve_span(pos, safe_end)
                if end is None and not final:
                    # Opener without a closer so far: keep waiting.
                    break
                if end is None:
                    # End of input. If the full buffer still has no closer
                    # the opener never completes -- exactly like a regex
                    # whose closer is missing; drop the entry and let the
                    # ordinary rules lex the characters.
                    final_end, _ = self._resolve_span(pos, length)
                    if final_end is not None:
                        end, ttype = final_end, self._openers[pos][2]
                    else:
                        del self._openers[pos]
                if end is not None:
                    value = buffer[pos:end]
                    yield ttype, value
                    self._prev_char = value[-1]
                    self._prev_family = self._token_family(value, ttype)
                    pos = end
                    continue

            chosen = None
            undecidable = False
            emulated = False
            for rexmatch, action in self._lexer._SQL_REGEX:
                # The "(?<=\.)" name rule cannot see a period delivered
                # before the buffer, so emulate its lookbehind up front.
                if pos == 0 and rexmatch.__self__.pattern == \
                        _DOT_NAME_RULE:
                    if self._prev_char != '.':
                        continue
                    dot_match = _DOT_NAME_NO_LOOKBEHIND.match(buffer, 0)
                    if dot_match is None:
                        # Not a name after the period; other rules apply.
                        continue
                    if dot_match.end() > safe_end:
                        undecidable = True
                        break
                    value = dot_match.group()
                    yield tokens.Name, value
                    self._prev_char = value[-1]
                    self._prev_family = self._token_family(
                        value, tokens.Name)
                    pos = dot_match.end()
                    emulated = True
                    break
                m = rexmatch(buffer, pos)
                if not m:
                    continue
                if m.end() > safe_end:
                    # The match reaches into undecidable suffix territory;
                    # a longer input could still change it, so wait.
                    undecidable = True
                    break
                # The placeholder lookbehind cannot see the character
                # delivered just before the buffer; a "$", ":" or "?"
                # continuing a word is lexed by the ordinary rules.
                if pos == 0 and rexmatch.__self__.pattern == \
                        _PLACEHOLDER_RULE and \
                        self._prev_is_word_char(buffer, 0):
                    continue
                # Same issue for the bracket-name rule: its lookbehind
                # must account for the character before the buffer.
                if pos == 0 and rexmatch.__self__.pattern == \
                        _BRACKET_RULE and \
                        not self._bracket_can_open(buffer, 0):
                    continue
                chosen = (m, action)
                break
            if undecidable:
                break
            if emulated:
                continue
            if chosen is None:
                # No regex matches here, mirroring the one-shot lexer's
                # single-character Error fallback.
                char = buffer[pos]
                yield tokens.Error, char
                self._prev_char = char
                self._prev_family = self._token_family(char, tokens.Error)
                pos += 1
                continue
            m, action = chosen
            if not final and self._match_can_grow(buffer, pos, m,
                                                  safe_end):
                # A later chunk could make a different, longer rule win at
                # this position (e.g. "create" growing into
                # "create or replace"), so the token is not settled yet.
                break
            value = m.group()
            if isinstance(action, tokens._TokenType):
                ttype = action
                yield action, value
            elif action is keywords.PROCESS_AS_KEYWORD:
                ttype, _ = self._lexer.is_keyword(value)
                yield ttype, value
            self._prev_char = value[-1]
            self._prev_family = self._token_family(value, ttype)
            pos = m.end()
        self._discard(pos)

    def _safe_end(self, buffer):
        """Rightmost offset that may be lexed without risking a token that
        a later chunk could change. Everything behind it is a prefix whose
        complete form has not arrived yet."""
        length = len(buffer)
        if length == 0:
            return 0
        blockers = self._continuation_blockers(buffer, length)
        blockers.extend(self._span_prefix_blockers(buffer, length))
        blockers.extend(
            self._quote_blockers(buffer, length, self._comment_regions(
                buffer, length)))
        blockers.extend(self._comment_blockers(buffer, length))
        for pos in sorted(self._openers):
            end, _ = self._resolve_span(pos, length)
            if end is None or end >= length - 1:
                blockers.append(pos)
        if blockers:
            return min(blockers)
        # Without a hard boundary the final character stays undecidable,
        # since a word, number or operator run may still grow.
        if not self._is_hard_boundary(buffer[-1]):
            return length - 1
        if buffer[-1] in ' \t\n' and self._last_token_can_grow(buffer):
            # Keep the separator: a multi-word rule (e.g.
            # "create or replace") spans it and must see both halves at
            # once rather than emitting the prefix as its own token.
            return length - 1
        return length

    def _continuation_blockers(self, buffer, length):
        """Positions where a token may still merge across the boundary."""
        blockers = []
        if self._lexer_continues_prev(buffer[0]):
            blockers.append(0)
        for pos in range(length):
            has_next = pos + 1 < length
            next_is_word = has_next and self._is_word_char(
                buffer[pos + 1])
            # An operator directly before a word may be part of a Name
            # (e.g. "#b0"), and "$:?" may open a placeholder there.
            if next_is_word and (buffer[pos] in '+/@#%^&|^-' or
                                 buffer[pos] in '$:?'):
                blockers.append(pos)
            # "$:?" at the buffer edge may complete a placeholder later.
            if not has_next and buffer[pos] in '$:?':
                blockers.append(pos)
            # A "$" inside an identifier is only consumed when another
            # word character follows; otherwise it is its own Error token.
            if buffer[pos] == '$' and pos > 0 and \
                    self._is_word_char(buffer[pos - 1]) and \
                    (not has_next or not next_is_word):
                blockers.append(pos)
        if buffer[-1] == '\r':
            blockers.append(length - 1)
        return blockers

    def _span_prefix_blockers(self, buffer, length):
        """Truncated multiline-comment/dollar-quote opener prefixes."""
        blockers = []
        for pos in range(max(0, length - 2), length):
            if not keywords.is_span_prefix(buffer, pos):
                continue
            # A "$" continuing a delivered word cannot open a quote.
            if buffer[pos] == '$' and \
                    self._preceded_by_word_char(buffer, pos):
                continue
            blockers.append(pos)
        if length >= 2 and buffer[0] in '/$' and \
                self._prev_is_word_char(buffer, 0):
            blockers.append(0)
        return blockers

    def _quote_blockers(self, buffer, length, comment_regions):
        """Unclosed quotes, doubled quotes and unclosed bracket names."""
        blockers = []
        pending_quote = None
        # Positions inside already matched quoted strings or bracket
        # names cannot start a new string; remembering the spans keeps
        # interior quotes (e.g. a backtick inside '...' or '[...`...]')
        # from being mistaken for openers.
        consumed_until = -1
        for pos, char in enumerate(buffer):
            if pos < consumed_until:
                continue
            # Characters inside a line comment are never string openers.
            if any(start <= pos < end for start, end in comment_regions):
                continue
            if char == '[' and self._bracket_can_open(buffer, pos):
                newline = buffer.find('\n', pos + 1)
                close = buffer.find(']', pos + 1)
                inner = buffer.find('[', pos + 1)
                # The name ends at the first newline or inner bracket.
                if close < 0 or (newline >= 0 and newline < close) or \
                        (inner >= 0 and inner < close):
                    blockers.append(pos)
                    continue
                consumed_until = close + 1
            if char in ("'", '"', '`'):
                end = self._quote_match_end(buffer, pos)
                if end is None:
                    if pending_quote is None:
                        pending_quote = pos
                else:
                    consumed_until = end
                    # The string is only settled when a hard boundary
                    # follows its closing quote. Otherwise the next chunk
                    # may merge an identical quote (doubled quote) or let
                    # the backtracking regex extend the match (e.g. an
                    # escaped quote that turns out to be mid-string).
                    if end >= length or not \
                            self._is_hard_boundary(buffer[end]):
                        blockers.append(pos)
        if pending_quote is not None:
            blockers.append(pending_quote)
        return blockers

    def _comment_regions(self, buffer, length):
        """Spans (start, end) covered by line comments in the buffer."""
        regions = []
        for pos in range(length - 1):
            if buffer[pos:pos + 2] == '--' or \
                    buffer[pos:pos + 2] == '# ':
                rest = buffer[pos + 2:]
                nl = rest.find('\n')
                cr = rest.find('\r')
                candidates = [i for i in (nl, cr) if i >= 0]
                end = length if not candidates else pos + 2 + min(candidates)
                regions.append((pos, end))
        return regions

    def _comment_blockers(self, buffer, length):
        """Two-character line-comment openers in the last physical line."""
        line_start = buffer.rfind('\n', 0, length - 1) + 1
        cr_pos = buffer.rfind('\r', 0, length - 1)
        if cr_pos + 1 > line_start:
            line_start = cr_pos + 1
        blockers = []
        suffix = buffer[line_start:]
        for pos, char in enumerate(suffix):
            if pos + 1 >= len(suffix):
                continue
            if char == '-' and suffix[pos + 1] == '-':
                blockers.append(line_start + pos)
            if char == '#' and suffix[pos + 1] == ' ':
                blockers.append(line_start + pos)
        return blockers

    def _last_token_can_grow(self, buffer):
        for rexmatch, _ in self._lexer._SQL_REGEX:
            probe = rexmatch(buffer, 0)
            if probe is None or probe.end() >= len(buffer):
                continue
            if self._multiword_rule_pending(probe.group()):
                return True
        return False

    @staticmethod
    def _is_hard_boundary(char):
        # Whitespace and single-character punctuation that no token rule
        # can extend past. "[" is excluded: it may open a bracket-quoted
        # name, and "\r" is excluded: it may pair with a following "\n".
        return char in ' \t\n;,()]'

    @staticmethod
    def _quote_match_end(line, quote_pos):
        """End offset of the quoted string opening at `quote_pos`, or
        None when its closing quote is missing. An empty pair ('' or
        "") is a complete match and returns the offset behind it."""
        char = line[quote_pos]
        if char == '`':
            pattern = _BACKTICK_RE
        elif char == '"':
            pattern = _DOUBLE_QUOTE_RE
        else:
            pattern = _SINGLE_QUOTE_RE
        m = pattern.match(line, quote_pos)
        if m is None:
            return None
        return m.end()

    def _match_can_grow(self, buffer, pos, match, safe_end):
        """True if appending text could let a rule outrank `match` at
        `pos`. A match ending at the provisional edge may always still
        extend. Otherwise only a multi-word rule spanning the whitespace
        separator right behind the match (e.g. "create" | " or replace")
        can change it; hard punctuation cannot."""
        end = match.end()
        # A whitespace separator right behind the match may be spanned by
        # a longer multi-word rule; otherwise only a match reaching the
        # buffer edge is still provisional.
        if end < safe_end and buffer[end] not in ' \t\n':
            return False
        return self._multiword_rule_pending(match.group())

    @staticmethod
    def _multiword_rule_pending(matched):
        """True if `matched` is the first word of a whitespace-spanning
        rule whose remaining words may still arrive. Withholding is always
        safe: when the next word does not complete such a rule, the word
        is emitted on its own exactly as the one-shot lexer would."""
        return matched.lower() in _WHITESPACE_RULE_PREFIXES

    @staticmethod
    def _is_complete_opener(buffer, pos):
        char = buffer[pos]
        if char == '/' and buffer.startswith(('/*', '/*+'), pos):
            return True
        if char == '$':
            m = keywords._DOLLAR_QUOTE_DELIM.match(buffer, pos)
            return m is not None and m.group(1).endswith('$') and \
                keywords._DOLLAR_QUOTE_OPENER_OK.match(buffer, pos) \
                is not None
        return False

    def _preceded_by_word_char(self, buffer, pos):
        """True if a word character, `"` or `$` directly precedes `pos`,
        including a character that was already delivered as a token."""
        if pos > 0:
            prev = buffer[pos - 1]
        else:
            prev = self._prev_char
        return bool(prev) and (prev.isalnum() or prev == '_' or
                               prev in '"$')

    def _prev_is_word_char(self, buffer, pos):
        """True if the character directly before `pos` is a word
        character (the `(?<!\\w)` lookbehind), including one already
        delivered as a token."""
        prev = buffer[pos - 1] if pos > 0 else self._prev_char
        return bool(prev) and (prev.isalnum() or prev == '_')

    def _bracket_can_open(self, buffer, pos):
        """True if `[` at `pos` may open a bracket-quoted name, i.e. it
        is not preceded by a word character, `]` or `)`."""
        if pos > 0:
            prev = buffer[pos - 1]
        else:
            prev = self._prev_char
        return not prev or not (prev.isalnum() or prev == '_' or
                                prev in '])')

    def _lexer_continues_prev(self, char):
        """True if a token starting with `char` would merge with the
        already-delivered token when both are lexed together."""
        family = self._prev_family
        if family == 'word':
            # Identifiers may contain "#" and "$" after the first
            # character, and word characters always continue a word.
            return char.isalnum() or char in '_#$'
        if family == 'operator':
            return char in '+/@#%^&|^-'
        return False

    @staticmethod
    def _is_word_char(char):
        return char.isalnum() or char == '_'

    @staticmethod
    def _token_family(value, ttype):
        if ttype is tokens.Error:
            # An Error "#" or "$" can still continue a following word
            # token (e.g. "$#b0"), but a lone "$" does not by itself.
            if value and all(c in '#' or c.isalnum() or c == '_'
                             for c in value):
                return 'word'
            return ''
        if ttype in (tokens.Name, tokens.Name.Placeholder,
                     tokens.Keyword, tokens.Keyword.DML,
                     tokens.Keyword.DDL, tokens.Keyword.Order,
                     tokens.Name.Builtin, tokens.Operator) or \
                ttype in tokens.Keyword:
            value = value.strip()
            if value and all(c in '+/@#%^&|^-' for c in value):
                return 'operator'
            if value and (value[0].isalnum() or value[0] == '_'
                          or value[0] == '#'):
                return 'word'
        return ''

    def _index_pending_openers(self):
        # The stateless scan cannot know the character delivered just
        # before the buffer, so drop any dollar opener at position zero
        # that continues an already-delivered word.
        if 0 in self._openers and self._buffer[:1] == '$' and \
                self._preceded_by_word_char(self._buffer, 0):
            self._openers.pop(0)
        for pos in range(len(self._buffer)):
            if not self._is_complete_opener(self._buffer, pos):
                continue
            if self._buffer[pos] == '$' and pos == 0 and \
                    self._preceded_by_word_char(self._buffer, 0):
                continue
            if self._buffer[pos] == '/':
                hint = self._buffer.startswith('/*+', pos)
                entry = (
                    pos + (3 if hint else 2), keywords._COMMENT_TAG,
                    tokens.Comment.Multiline.Hint if hint
                    else tokens.Comment.Multiline)
            else:
                m = keywords._DOLLAR_QUOTE_DELIM.match(self._buffer, pos)
                tag = m.group(1)
                entry = (pos + len(tag), tag, tokens.Literal)
            # Also upgrades a "/*" registered before its trailing "+" was
            # known to the hint variant.
            self._openers[pos] = entry

    def _resolve_span(self, pos, safe_end):
        opener_end, tag, ttype = self._openers[pos]
        starts, ends = self._closers[tag]
        idx = bisect_left(starts, opener_end)
        if idx == len(starts) or ends[idx] > safe_end:
            return None, None
        return ends[idx], ttype

    def _discard(self, pos):
        if pos == 0:
            self._cull_openers()
            return
        self._buffer = self._buffer[pos:]
        self._openers = {
            start - pos: (end - pos, tag, ttype)
            for start, (end, tag, ttype) in self._openers.items()
            if start >= pos}
        new_closers = defaultdict(lambda: ([], []))
        for tag, (starts, ends) in self._closers.items():
            cut = bisect_left(ends, pos)
            if cut < len(ends):
                new_closers[tag] = (
                    [start - pos for start in starts[cut:]],
                    [end - pos for end in ends[cut:]])
        self._closers = new_closers
        self._cull_openers()

    def _cull_openers(self):
        """Drop opener entries that fall inside an outer pending span.

        Such a position is consumed by the outer span's token and is never
        lexed on its own; keeping its entry would make the end-of-input
        fallback misparse the span's interior."""
        kept = {}
        for start in sorted(self._openers):
            if kept:
                outer_end, _, _ = kept[max(kept)]
                if start < outer_end:
                    continue
            kept[start] = self._openers[start]
        self._openers = kept


def tokenize_iter(chunks, encoding=None):
    """Tokenize an iterable of text chunks incrementally.

    Each chunk is pulled only when the previously buffered text has been
    exhausted, so completed tokens -- and, one layer above, completed
    statements -- are delivered without waiting for end of input. Bytes
    chunks are decoded with `encoding` (default UTF-8)."""
    inc = IncrementalLexer()
    try:
        for chunk in chunks:
            if isinstance(chunk, bytes):
                chunk = _decode_chunk(chunk, encoding)
            yield from inc.feed(chunk)
        yield from inc.close()
    except GeneratorExit:
        # The consumer stopped early; release the buffered statement
        # instead of keeping the whole script alive.
        inc._discard(len(inc._buffer))
        raise


def _decode_chunk(chunk, encoding):
    if encoding:
        return chunk.decode(encoding)
    try:
        return chunk.decode('utf-8')
    except UnicodeDecodeError:
        return chunk.decode('unicode-escape')
