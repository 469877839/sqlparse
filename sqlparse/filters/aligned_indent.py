#
# Copyright (C) 2009-2020 the sqlparse authors and contributors
# <see AUTHORS file>
#
# This module is part of python-sqlparse and is released under
# the BSD License: https://opensource.org/licenses/BSD-3-Clause

from sqlparse import sql
from sqlparse import tokens as T


class AlignedIndentFilter:
    join_words = (r'((LEFT\s+|RIGHT\s+|FULL\s+)?'
                  r'(INNER\s+|OUTER\s+|STRAIGHT\s+)?|'
                  r'(CROSS\s+|NATURAL\s+)?)?JOIN\b')
    by_words = r'(GROUP|ORDER)\s+BY\b'
    split_words = ('FROM',
                   join_words, 'ON', by_words,
                   'WHERE', 'AND', 'OR',
                   'HAVING', 'LIMIT',
                   'UNION', 'VALUES',
                   'SET', 'BETWEEN', 'EXCEPT')

    def __init__(self, char=' ', n='\n'):
        self.n = n
        self.offset = 0
        self.indent = 0
        self.char = char
        self._max_kwd_len = len('select')

    def nl(self, offset=1):
        # offset = 1 represent a single space after SELECT
        offset = -len(offset) if not isinstance(offset, int) else offset
        # add two for the space and parenthesis
        indent = self.indent * (2 + self._max_kwd_len)

        return sql.Token(T.Whitespace, self.n + self.char * (
            self._max_kwd_len + offset + indent + self.offset))

    def _process_statement(self, tlist):
        if len(tlist.tokens) > 0 and tlist.tokens[0].is_whitespace \
                and self.indent == 0:
            tlist.tokens.pop(0)

        # process the main query body
        self._process(sql.TokenList(tlist.tokens))

    def _process_parenthesis(self, tlist):
        # if this isn't a subquery, don't re-indent
        _, token = tlist.token_next_by(m=(T.DML, 'SELECT'))
        if token is None:
            # The recursion left non-subquery parenthesis and their children
            # untouched; the explicit traversal mirrors that.
            return False
        outer_indent = self.indent
        self.indent += 1
        tlist.insert_after(tlist[0], self.nl('SELECT'))
        # process the inside of the parenthesis
        self._process_default(tlist)
        # de-indent last parenthesis (this line uses the outer indent)
        self.indent = outer_indent
        tlist.insert_before(tlist[-1], self.nl())
        # Children are still indented; the frame that entered this group
        # restores the original indent after they have been handled.
        self.indent = outer_indent + 1

    def _process_identifierlist(self, tlist):
        # columns being selected
        identifiers = list(tlist.get_identifiers())
        identifiers.pop(0)
        [tlist.insert_before(token, self.nl()) for token in identifiers]
        self._process_default(tlist)

    def _process_case(self, tlist):
        offset_ = len('case ') + len('when ')
        cases = tlist.get_cases(skip_ws=True)
        # align the end as well
        end_token = tlist.token_next_by(m=(T.Keyword, 'END'))[1]
        # END is missing on malformed input, e.g. when an inner BEGIN ... END
        # block closed the CASE.
        if end_token is not None:
            cases.append((None, [end_token]))

        condition_width = [len(' '.join(map(str, cond))) if cond else 0
                           for cond, _ in cases]
        max_cond_width = max(condition_width, default=0)

        for i, (cond, value) in enumerate(cases):
            # cond is None when 'else or end'
            stmt = cond[0] if cond else value[0]

            if i > 0:
                tlist.insert_before(stmt, self.nl(offset_ - len(str(stmt))))
            if cond:
                ws = sql.Token(T.Whitespace, self.char * (
                    max_cond_width - condition_width[i]))
                tlist.insert_after(cond[-1], ws)

    def _next_token(self, tlist, idx=-1):
        split_words = T.Keyword, self.split_words, True
        tidx, token = tlist.token_next_by(m=split_words, idx=idx)
        # treat "BETWEEN x and y" as a single statement
        if token and token.normalized == 'BETWEEN':
            tidx, token = self._next_token(tlist, tidx)
            if token and token.normalized == 'AND':
                tidx, token = self._next_token(tlist, tidx)
        return tidx, token

    def _split_kwds(self, tlist):
        tidx, token = self._next_token(tlist)
        while token:
            # joins, group/order by are special case. only consider the first
            # word as aligner
            if (
                token.match(T.Keyword, self.join_words, regex=True)
                or token.match(T.Keyword, self.by_words, regex=True)
            ):
                token_indent = token.value.split()[0]
            else:
                token_indent = str(token)
            tlist.insert_before(token, self.nl(token_indent))
            tidx += 1
            tidx, token = self._next_token(tlist, tidx)

    def _process_default(self, tlist):
        self._split_kwds(tlist)
        # Subgroups are visited by the explicit stack in ``process``; this
        # method only handles *tlist* itself.

    def _process(self, tlist):
        func_name = f'_process_{type(tlist).__name__}'
        func = getattr(self, func_name.lower(), self._process_default)
        return func(tlist)

    def process(self, stmt):
        # The descent used to recurse one call frame per nesting level; it
        # is now an explicit stack. Each frame is
        # ``[group, state, children, next_index]`` where *state* is the
        # indent/offset snapshot to restore when leaving the group and
        # *children* is snapshotted once, right after the group's own
        # handler ran. The indentation a handler leaves for its subtree
        # therefore applies to all of its children, as the recursive
        # ``with indent(...)`` scope did.
        stack = [[stmt, (self.indent, self.offset), None, 0]]
        while stack:
            frame = stack[-1]
            tlist, state, children, index = frame
            if children is None:
                descend = self._process(tlist)
                frame[2] = (list(tlist.get_sublists())
                            if descend is not False else [])
                continue
            if index >= len(children):
                self.indent, self.offset = state
                stack.pop()
                continue
            sgroup = children[index]
            frame[3] = index + 1
            idx = tlist.token_index(sgroup)
            pidx, prev_ = tlist.token_prev(idx)
            # HACK: make "group/order by" work. Longer than max_len.
            offset_ = 3 if (
                prev_ and prev_.match(T.Keyword, self.by_words, regex=True)
            ) else 0
            self.offset += offset_
            stack.append([sgroup, (self.indent, self.offset - offset_),
                          None, 0])
        return stmt
