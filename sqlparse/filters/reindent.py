#
# Copyright (C) 2009-2020 the sqlparse authors and contributors
# <see AUTHORS file>
#
# This module is part of python-sqlparse and is released under
# the BSD License: https://opensource.org/licenses/BSD-3-Clause

from sqlparse import sql
from sqlparse import tokens as T


class ReindentFilter:
    def __init__(self, width=2, char=' ', wrap_after=0, n='\n',
                 comma_first=False, indent_after_first=False,
                 indent_columns=False, compact=False):
        self.n = n
        self.width = width
        self.char = char
        self.indent = 1 if indent_after_first else 0
        self.offset = 0
        self.wrap_after = wrap_after
        self.comma_first = comma_first
        self.indent_columns = indent_columns
        self.compact = compact
        self._curr_stmt = None
        self._last_stmt = None
        self._last_func = None

    @property
    def leading_ws(self):
        return self.offset + self.indent * self.width

    def _current_line_len(self, token):
        """Returns the width of what's already emitted on *token*'s line.

        The tokens preceding *token* are visited last one first, so the walk
        stops at the line break that starts the current line.  Rebuilding the
        statement prefix from its start instead made every caller
        O(statement), and the callers running once per group (tuple lists,
        identifier lists) quadratic in the number of groups -- a CPU
        exhaustion vector (GHSA-cfqr-cjx5-5jcm).  The walk is inlined and
        counts characters rather than collecting them: both matter, since a
        line without any break still has to be measured token by token.
        """
        length = 0
        node = token
        while node is not self._curr_stmt and node.parent is not None:
            parent = node.parent
            # ``Token`` doesn't implement ``__eq__``, so ``index()`` is an
            # identity lookup and safe against tokens sharing a value.
            stack = parent.tokens[:parent.tokens.index(node)]
            while stack:
                prev_ = stack.pop()
                if prev_.is_group:
                    stack.extend(prev_.tokens)
                    continue

                value = prev_.value
                size = len(value)
                if not size:
                    continue
                lines = value.splitlines()
                if len(lines) == 1 and len(lines[0]) == size:
                    # No break in here.  ``splitlines()`` hands back the value
                    # itself in that case, so this costs a scan but no copy --
                    # which is what keeps a long break-free line affordable.
                    length += size
                    continue

                # ``value`` holds the break that starts the current line.  The
                # sentinel keeps a trailing break from collapsing, so ``lines``
                # always has one entry more than the number of breaks.
                lines = (value + '.').splitlines()
                tail = len(lines[-1]) - 1 + length
                if tail:
                    return tail
                # Nothing but a break to our right, and ``splitlines()`` drops
                # that empty line -- so the line to measure is the one before.
                if len(lines) > 2:
                    return len(lines[-2])
                length = len(lines[0])
            node = parent
        return length

    def _get_offset(self, token):
        # Now take current offset into account and return relative offset.
        return self._current_line_len(token) - len(self.char * self.leading_ws)

    def nl(self, offset=0):
        return sql.Token(
            T.Whitespace,
            self.n + self.char * max(0, self.leading_ws + offset))

    def _next_token(self, tlist, idx=-1):
        split_words = ('FROM', 'STRAIGHT_JOIN$', 'JOIN$', 'AND', 'OR',
                       'GROUP BY', 'ORDER BY', 'UNION', 'VALUES',
                       'SET', 'BETWEEN', 'EXCEPT', 'HAVING', 'LIMIT')
        m_split = T.Keyword, split_words, True
        tidx, token = tlist.token_next_by(m=m_split, idx=idx)

        if token and token.normalized == 'BETWEEN':
            tidx, token = self._next_token(tlist, tidx)

            if token and token.normalized == 'AND':
                tidx, token = self._next_token(tlist, tidx)

        return tidx, token

    def _split_kwds(self, tlist):
        tidx, token = self._next_token(tlist)
        while token:
            pidx, prev_ = tlist.token_prev(tidx, skip_ws=False)
            uprev = str(prev_)

            if prev_ and prev_.is_whitespace:
                del tlist.tokens[pidx]
                tidx -= 1

            if not (uprev.endswith('\n') or uprev.endswith('\r')):
                tlist.insert_before(tidx, self.nl())
                tidx += 1

            tidx, token = self._next_token(tlist, tidx)

    def _split_statements(self, tlist):
        ttypes = T.Keyword.DML, T.Keyword.DDL
        tidx, token = tlist.token_next_by(t=ttypes)
        while token:
            pidx, prev_ = tlist.token_prev(tidx, skip_ws=False)
            if prev_ and prev_.is_whitespace:
                del tlist.tokens[pidx]
                tidx -= 1
            # only break if it's not the first token
            if prev_:
                tlist.insert_before(tidx, self.nl())
                tidx += 1
            tidx, token = tlist.token_next_by(t=ttypes, idx=tidx)

    def _process(self, tlist):
        func_name = f'_process_{type(tlist).__name__}'
        func = getattr(self, func_name.lower(), self._process_default)
        return func(tlist)

    def _process_where(self, tlist):
        tidx, token = tlist.token_next_by(m=(T.Keyword, 'WHERE'))
        if not token:
            # No WHERE keyword: the recursion used to stop here, leaving the
            # whole subtree untouched; the explicit traversal mirrors that
            # by skipping this group's children (see ``process``).
            return False
        # issue121, errors in statement fixed??
        tlist.insert_before(tidx, self.nl())
        self.indent += 1
        self._process_default(tlist)
        # The indent stays incremented for this group's children; the
        # traversal restores it when leaving the group (see ``process``).

    def _process_parenthesis(self, tlist):
        ttypes = T.Keyword.DML, T.Keyword.DDL
        _, is_dml_dll = tlist.token_next_by(t=ttypes)
        fidx, first = tlist.token_next_by(m=sql.Parenthesis.M_OPEN)
        if first is None:
            # See _process_where: an early return used to skip the subtree.
            return False

        self.indent += 1 if is_dml_dll else 0
        if is_dml_dll:
            tlist.tokens.insert(0, self.nl())
        self.offset += self._get_offset(first) + 1
        self._process_default(tlist, not is_dml_dll)
        # Both values stay adjusted for this group's children; the traversal
        # restores them when leaving the group (see ``process``).

    def _process_function(self, tlist):
        self._last_func = tlist[0]
        self._process_default(tlist)

    def _process_identifierlist(self, tlist):
        identifiers = list(tlist.get_identifiers())
        if self.indent_columns:
            first = next(identifiers[0].flatten())
            num_offset = 1 if self.char == '\t' else self.width
        else:
            first = next(identifiers.pop(0).flatten())
            num_offset = 1 if self.char == '\t' else self._get_offset(first)

        if not tlist.within(sql.Function) and not tlist.within(sql.Values):
            # The offset applies only while this branch's line breaks are
            # computed; restore it before subgroups are traversed.
            self.offset += num_offset
            try:
                position = 0
                for token in identifiers:
                    # Add 1 for the "," separator
                    position += len(token.value) + 1
                    if position > (self.wrap_after - self.offset):
                        adjust = 0
                        if self.comma_first:
                            adjust = -2
                            _, comma = tlist.token_prev(
                                tlist.token_index(token))
                            if comma is None:
                                continue
                            token = comma
                        tlist.insert_before(token, self.nl(offset=adjust))
                        if self.comma_first:
                            _, ws = tlist.token_next(
                                tlist.token_index(token), skip_ws=False)
                            if (ws is not None
                                    and ws.ttype is not T.Text.Whitespace):
                                tlist.insert_after(
                                    token, sql.Token(T.Whitespace, ' '))
                        position = 0
            finally:
                self.offset -= num_offset
        else:
            # ensure whitespace
            for token in tlist:
                _, next_ws = tlist.token_next(
                    tlist.token_index(token), skip_ws=False)
                if token.value == ',' and not next_ws.is_whitespace:
                    tlist.insert_after(
                        token, sql.Token(T.Whitespace, ' '))

            end_at = self.offset + sum(len(i.value) + 1 for i in identifiers)
            adjusted_offset = 0
            if (self.wrap_after > 0
                    and end_at > (self.wrap_after - self.offset)
                    and self._last_func):
                adjusted_offset = -len(self._last_func.value) - 1

            # Same as above: the adjustment is local to this branch.
            self.offset += adjusted_offset
            self.indent += 1
            try:
                if adjusted_offset < 0:
                    tlist.insert_before(identifiers[0], self.nl())
                position = 0
                for token in identifiers:
                    # Add 1 for the "," separator
                    position += len(token.value) + 1
                    if (self.wrap_after > 0
                            and position > (self.wrap_after - self.offset)):
                        adjust = 0
                        tlist.insert_before(token, self.nl(offset=adjust))
                        position = 0
            finally:
                self.offset -= adjusted_offset
                self.indent -= 1
        self._process_default(tlist)

    def _process_case(self, tlist):
        iterable = iter(tlist.get_cases())
        cond, _ = next(iterable)
        first = next(cond[0].flatten())

        # Reproduce the two nested ``with offset(...)`` scopes explicitly.
        # The second offset is evaluated while the first already applies and
        # ``_get_offset`` reads ``self.offset`` via ``leading_ws``, so it must
        # not be precomputed: doing so double-counts the line prefix.
        base = self.offset
        off1 = self._get_offset(tlist[0])
        self.offset += off1
        off2 = self._get_offset(first)
        self.offset += off2
        for cond, value in iterable:
            str_cond = ''.join(str(x) for x in cond or [])
            str_value = ''.join(str(x) for x in value)
            end_pos = self.offset + 1 + len(str_cond) + len(str_value)
            if (not self.compact and end_pos > self.wrap_after):
                token = value[0] if cond is None else cond[0]
                tlist.insert_before(token, self.nl())

        # Keyword splitting ran inside the inner scope (off2) plus the
        # "WHEN " adjustment.
        self.offset -= off2
        self.offset += len("WHEN ")
        try:
            self._process_default(tlist)
        finally:
            self.offset -= len("WHEN ")

        # The END line was inserted back inside the outer scope (off1),
        # after the inner one had ended.
        end_idx, end = tlist.token_next_by(m=sql.Case.M_CLOSE)
        if end_idx is not None and not self.compact:
            tlist.insert_before(end_idx, self.nl())
        # Restore the outer scope; the traversal restores the base offset
        # when this group's subtree has been handled.
        self.offset = base

    def _process_values(self, tlist):
        tlist.insert_before(0, self.nl())
        tidx, token = tlist.token_next_by(i=sql.Parenthesis)
        first_token = token
        while token:
            ptidx, ptoken = tlist.token_next_by(m=(T.Punctuation, ','),
                                                idx=tidx)
            if ptoken:
                if self.comma_first:
                    adjust = -2
                    offset = self._get_offset(first_token) + adjust
                    tlist.insert_before(ptoken, self.nl(offset))
                else:
                    tlist.insert_after(ptoken,
                                       self.nl(self._get_offset(token)))
            tidx, token = tlist.token_next_by(i=sql.Parenthesis, idx=tidx)

    def _process_default(self, tlist, stmts=True):
        self._split_statements(tlist) if stmts else None
        self._split_kwds(tlist)
        # Subgroups are visited by the explicit stack in ``process``; this
        # method only handles *tlist* itself.

    def process(self, stmt):
        self._curr_stmt = stmt
        # The descent used to recurse through ``_process`` ->
        # ``_process_default`` -> ``_process``, one call frame per nesting
        # level, which failed on moderately deep parenthesised input. It is
        # now an explicit stack; each frame is
        # ``[group, state, children, next_index]`` where *state* is the
        # indent/offset snapshot to restore when leaving the group and
        # *children* is snapshotted once, right after the group's own
        # handler ran (so every subgroup is enumerated exactly once and a
        # handler returning False skips its subtree, as the recursion did).
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
            stack.append([sgroup, (self.indent, self.offset), None, 0])

        if self._last_stmt is not None:
            nl = '\n' if str(self._last_stmt).endswith('\n') else '\n\n'
            stmt.tokens.insert(0, sql.Token(T.Whitespace, nl))

        self._last_stmt = stmt
        return stmt
