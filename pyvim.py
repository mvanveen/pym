#!/usr/bin/env python3
"""pyvim - A pure Python vim-like editor"""
import curses
import os
import re
import sys
import copy
from enum import Enum, auto

# ── Modes ────────────────────────────────────────────────────────────────────

class Mode(Enum):
    NORMAL = auto()
    INSERT = auto()
    VISUAL = auto()
    VISUAL_LINE = auto()
    COMMAND = auto()
    SEARCH = auto()
    EXPLORER = auto()

# ── Core data ────────────────────────────────────────────────────────────────

class Cursor:
    __slots__ = ('row', 'col', 'col_want')
    def __init__(self):
        self.row = 0; self.col = 0; self.col_want = 0

class Buffer:
    def __init__(self, lines=None):
        self.lines = lines or ['']
        self.filename = None
        self.modified = False
        self._undo = []; self._redo = []
        self._gen = 0  # incremented on every mutation; used as highlight cache key

    def save_undo(self):
        self._undo.append(copy.deepcopy(self.lines))
        self._redo.clear()
        if len(self._undo) > 500: self._undo.pop(0)

    def undo(self):
        if not self._undo: return False
        self._redo.append(copy.deepcopy(self.lines))
        self.lines = self._undo.pop(); self.modified = True; self._gen += 1; return True

    def redo(self):
        if not self._redo: return False
        self._undo.append(copy.deepcopy(self.lines))
        self.lines = self._redo.pop(); self.modified = True; self._gen += 1; return True

    def line_count(self): return len(self.lines)
    def get_line(self, r): return self.lines[r] if 0 <= r < len(self.lines) else ''

    def set_line(self, r, t):
        if 0 <= r < len(self.lines): self.lines[r] = t; self.modified = True; self._gen += 1

    def insert_line(self, r, t=''):
        self.lines.insert(max(0, min(r, len(self.lines))), t); self.modified = True; self._gen += 1

    def delete_line(self, r):
        if 0 <= r < len(self.lines):
            l = self.lines.pop(r)
            if not self.lines: self.lines = ['']
            self.modified = True; self._gen += 1; return l
        return ''

    def insert_char(self, r, c, ch):
        if 0 <= r < len(self.lines):
            s = self.lines[r]; self.lines[r] = s[:c] + ch + s[c:]; self.modified = True; self._gen += 1

    def delete_char(self, r, c):
        if 0 <= r < len(self.lines):
            s = self.lines[r]
            if 0 <= c < len(s):
                self.lines[r] = s[:c] + s[c+1:]; self.modified = True; self._gen += 1; return s[c]
        return ''

    def split_line(self, r, c):
        if 0 <= r < len(self.lines):
            s = self.lines[r]; self.lines[r] = s[:c]
            self.lines.insert(r+1, s[c:]); self.modified = True; self._gen += 1

    def join_lines(self, r):
        if 0 <= r < len(self.lines)-1:
            self.lines[r] += self.lines[r+1]; self.lines.pop(r+1); self.modified = True; self._gen += 1

    @classmethod
    def from_file(cls, filename):
        b = cls(); b.filename = filename
        try:
            with open(filename, 'r', errors='replace') as f: content = f.read()
            lines = content.splitlines(); b.lines = lines if lines else ['']
        except FileNotFoundError: b.lines = ['']
        b.modified = False; return b

    def save(self, filename=None):
        fname = filename or self.filename
        if not fname: raise ValueError('No file name')
        with open(fname, 'w') as f: f.write('\n'.join(self.lines) + '\n')
        self.filename = fname; self.modified = False

class Pane:
    def __init__(self, buf=None):
        self.buf    = buf or Buffer()
        self.cursor = Cursor()
        self.top_row = 0; self.left_col = 0
        # Screen geometry (set by layout engine)
        self.y = 0; self.x = 0; self.height = 24; self.width = 80

# ── Layout tree ──────────────────────────────────────────────────────────────

class _Leaf:
    def __init__(self, pane): self.pane = pane

class _Split:
    def __init__(self, a, b, vertical=False):
        self.a = a; self.b = b; self.vertical = vertical

def _lc_compute(node, y, x, h, w, divs):
    if isinstance(node, _Leaf):
        p = node.pane; p.y, p.x, p.height, p.width = y, x, max(1,h), max(1,w)
    else:
        if node.vertical:
            w1 = max(1, (w-1)//2); w2 = max(1, w-w1-1)
            divs.append(('v', y, x+w1, h))
            _lc_compute(node.a, y, x,      h, w1, divs)
            _lc_compute(node.b, y, x+w1+1, h, w2, divs)
        else:
            h1 = max(1, (h-1)//2); h2 = max(1, h-h1-1)
            divs.append(('h', y+h1, x, w))
            _lc_compute(node.a, y,    x, h1, w, divs)
            _lc_compute(node.b, y+h1+1, x, h2, w, divs)

def _lc_panes(node):
    if isinstance(node, _Leaf): return [node.pane]
    return _lc_panes(node.a) + _lc_panes(node.b)

def _lc_split(node, target, new_pane, vertical):
    if isinstance(node, _Leaf):
        if node.pane is target:
            return _Split(_Leaf(target), _Leaf(new_pane), vertical)
        return node
    n = copy.copy(node)
    n.a = _lc_split(node.a, target, new_pane, vertical)
    n.b = _lc_split(node.b, target, new_pane, vertical)
    return n

def _lc_close(node, target):
    if isinstance(node, _Leaf):
        return None if node.pane is target else node
    na = _lc_close(node.a, target); nb = _lc_close(node.b, target)
    if na is None: return nb
    if nb is None: return na
    n = copy.copy(node); n.a = na; n.b = nb; return n

# ── Syntax highlighting (pygments) ───────────────────────────────────────────

from pygments import lex
from pygments.lexers import get_lexer_for_filename, guess_lexer, get_lexer_by_name
from pygments.lexers import TextLexer
from pygments.token import Token
from pygments.util import ClassNotFound

# pygments ttype → (color_pair_index, extra_attr)
# Color pairs 9-15 are initialized in Editor.__init__ to match the old scheme.
def _pg_attr(ttype):
    if ttype in Token.Keyword:                             return (9,  curses.A_BOLD)
    if ttype in Token.Literal.String or ttype in Token.String: return (10, 0)
    if ttype in Token.Comment:                             return (11, curses.A_DIM)
    if ttype in Token.Literal.Number or ttype in Token.Number: return (12, 0)
    if ttype in Token.Name.Class or ttype in Token.Name.Exception \
            or ttype in Token.Name.Namespace:              return (13, 0)
    if ttype in Token.Name.Function:                       return (13, 0)
    if ttype in Token.Name.Builtin:                        return (14, 0)
    if ttype in Token.Name.Decorator:                      return (15, curses.A_BOLD)
    if ttype in Token.Operator.Word:                       return (9,  0)
    return None

# (id(buf), gen) → list[list[(start, end, curses_attr)]] indexed by row
_pg_cache: dict = {}

def _pg_highlight(buf):
    key = (id(buf), buf._gen)
    if key in _pg_cache:
        return _pg_cache[key]
    # Evict stale entries for this buffer (different gen)
    stale = [k for k in _pg_cache if k[0] == id(buf)]
    for k in stale: del _pg_cache[k]
    # Cap total cache size
    if len(_pg_cache) > 40:
        _pg_cache.clear()

    text = '\n'.join(buf.lines)
    try:
        if buf.filename:
            lexer = get_lexer_for_filename(buf.filename, stripnl=False, ensurenl=False)
        else:
            lexer = guess_lexer(text[:2048], stripnl=False, ensurenl=False)
    except ClassNotFound:
        lexer = TextLexer()

    # Build per-row span lists — store (start, end, (pair_idx, extra)) to defer
    # curses.color_pair() until draw time (requires initscr to have been called).
    by_row: list[list] = [[] for _ in buf.lines]
    row = 0; col = 0
    for ttype, value in lex(text, lexer):
        pg_attr = _pg_attr(ttype)
        lines = value.split('\n')
        for li, part in enumerate(lines):
            if li > 0:
                row += 1; col = 0
                if row >= len(by_row): break
            if pg_attr and part:
                by_row[row].append((col, col + len(part), pg_attr))
            col += len(part)

    _pg_cache[key] = by_row
    return by_row

def _detect_lang(filename):
    if not filename: return None
    try:
        get_lexer_for_filename(filename)
        return filename  # truthy — used only to decide "do we highlight?"
    except ClassNotFound:
        return None

def _is_markdown(filename):
    return bool(filename and filename.rsplit('.',1)[-1].lower() in ('md','markdown','mkd'))

# ── Markdown rendering via markdown-it-py ────────────────────────────────────
# Parses with a proper GFM parser so nested inline styles (bold+code, etc.)
# are handled correctly. Each buffer row gets (visual_line, spans) where
# markers are removed and spans carry (color_pair_idx, extra_attr) tuples.
# Cursor row / visual-selected rows are shown raw by the caller.

try:
    from markdown_it import MarkdownIt as _MdIt
    _MD = _MdIt("gfm-like").disable("linkify")
except ImportError:
    _MD = None

_md_cache: dict = {}
_TABLE_SEP_RE = re.compile(r'^\s*\|[-:| ]+\|\s*$')


def _is_table_row(line):
    s = line.strip()
    return s.startswith('|') and s.endswith('|') and not _TABLE_SEP_RE.match(line)


def _table_cells(line):
    """Return list of (content_start, content_end) for each cell in a raw table line."""
    if not _is_table_row(line): return []
    pipes = [i for i, c in enumerate(line) if c == '|']
    if len(pipes) < 2: return []
    cells = []
    for a, b in zip(pipes, pipes[1:]):
        s = a + 1
        while s < b and line[s] == ' ': s += 1
        e = b - 1
        while e > a and line[e] == ' ': e -= 1
        cells.append((s, e + 1))
    return cells


def _table_cell_at(line, col):
    """Return (cell_idx, content_start, content_end) for the cell containing col, or None."""
    pipes = [i for i, c in enumerate(line) if c == '|']
    cells = _table_cells(line)
    for i, (a, b) in enumerate(zip(pipes, pipes[1:])):
        if a < col <= b and i < len(cells):
            return (i, cells[i][0], cells[i][1])
    return None


def _inline_render(children):
    """Walk markdown-it inline token children → (visual_line, spans).
    Uses a per-character style array so nested bold+code etc. work cleanly."""
    active = set()
    chars: list = []
    styles: list = []

    def cur():
        if not active: return None
        p, a = 0, 0
        if 'bold'   in active: a |= curses.A_BOLD
        if 'italic' in active: a |= curses.A_UNDERLINE
        if 'strike' in active: a |= curses.A_DIM
        if 'link'   in active: p  = 13
        return (p, a) if (p or a) else None

    for tok in (children or []):
        t = tok.type
        if   t == 'strong_open':  active.add('bold')
        elif t == 'strong_close': active.discard('bold')
        elif t == 'em_open':      active.add('italic')
        elif t == 'em_close':     active.discard('italic')
        elif t == 'link_open':    active.add('link')
        elif t == 'link_close':   active.discard('link')
        elif t == 's_open':       active.add('strike')
        elif t == 's_close':      active.discard('strike')
        elif t == 'text':
            st = cur()
            for ch in tok.content: chars.append(ch); styles.append(st)
        elif t == 'code_inline':
            for ch in tok.content: chars.append(ch); styles.append((10, 0))
        elif t == 'image':
            alt = tok.content or (tok.attrs or {}).get('alt', '')
            st  = cur()
            for ch in f'[{alt}]': chars.append(ch); styles.append(st)
        # softbreak / hardbreak / html_inline: skip

    vis = ''.join(chars)
    spans, i = [], 0
    while i < len(styles):
        st = styles[i]
        if st is None: i += 1; continue
        j = i + 1
        while j < len(styles) and styles[j] == st: j += 1
        spans.append((i, j, st))
        i = j
    return vis, spans


def _inline_text(children) -> str:
    """Extract plain text from inline token children (used for headings/cells)."""
    return ''.join(tok.content for tok in (children or [])
                   if tok.type in ('text', 'code_inline'))


def _render_table_tokens(toks, lines):
    """Render a table token slice → {row_idx: (visual_line, spans)}."""
    DIM  = (11, curses.A_DIM)
    HEAD = (9,  curses.A_BOLD)
    in_head = False
    cur_ri = cur_hdr = None
    cur_cells: list = []
    rows = []

    for tok in toks:
        if   tok.type == 'thead_open':  in_head = True
        elif tok.type == 'thead_close': in_head = False
        elif tok.type == 'tr_open':
            cur_ri = tok.map[0] if tok.map else None
            cur_hdr = in_head; cur_cells = []
        elif tok.type == 'tr_close':
            if cur_ri is not None: rows.append((cur_ri, cur_hdr, cur_cells[:]))
        elif tok.type == 'inline' and cur_cells is not None:
            cur_cells.append(_inline_text(tok.children))

    if not rows: return {}
    ncols  = max(len(r[2]) for r in rows)
    widths = [1] * ncols
    for _, _, cells in rows:
        for ci, c in enumerate(cells[:ncols]):
            widths[ci] = max(widths[ci], len(c))

    result = {}
    for ri, hdr, cells in rows:
        parts = ['│']; spans = [(0, 1, DIM)]; pos = 1
        for ci in range(ncols):
            cell   = cells[ci] if ci < len(cells) else ''
            padded = ' ' + cell.ljust(widths[ci]) + ' '
            if hdr: spans.append((pos, pos + len(padded), HEAD))
            parts.append(padded); pos += len(padded)
            parts.append('│'); spans.append((pos, pos + 1, DIM)); pos += 1
        result[ri] = (''.join(parts), spans)

    # Separator row sits between last header row and first body row in the buffer
    hdrs  = [r for r in rows if     r[1]]
    bodys = [r for r in rows if not r[1]]
    if hdrs and bodys:
        for r in range(hdrs[-1][0] + 1, bodys[0][0]):
            if r < len(lines) and _TABLE_SEP_RE.match(lines[r]):
                vis = '├' + '┼'.join('─' * (w + 2) for w in widths) + '┤'
                result[r] = (vis, [(0, len(vis), DIM)]); break
    return result


def _md_highlight(buf):
    key = (id(buf), buf._gen)
    if key in _md_cache: return _md_cache[key]
    stale = [k for k in _md_cache if k[0] == id(buf)]
    for k in stale: del _md_cache[k]
    if len(_md_cache) > 20: _md_cache.clear()

    lines  = buf.lines
    by_row = [(ln, []) for ln in lines]   # default: raw, no styling

    if _MD is None:
        _md_cache[key] = by_row; return by_row

    H1 = (9, curses.A_BOLD | curses.A_UNDERLINE)
    H2 = (9, curses.A_BOLD)
    H3 = (9, 0)
    DM = (11, curses.A_DIM)
    FC = (14, 0)
    QU = (11, 0)

    tokens = _MD.parse('\n'.join(lines))
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok.type == 'heading_open' and tok.map:
            lv  = int(tok.tag[1]) if tok.tag and len(tok.tag) > 1 and tok.tag[1].isdigit() else 2
            inl = tokens[i + 1] if i + 1 < len(tokens) and tokens[i + 1].type == 'inline' else None
            vis = _inline_text(inl.children if inl else [])
            r   = tok.map[0]
            if r < len(by_row):
                by_row[r] = (vis, [(0, len(vis), H1 if lv == 1 else H2 if lv == 2 else H3)])
            i += 3; continue

        if tok.type == 'hr' and tok.map:
            r = tok.map[0]
            if r < len(by_row):
                vis = '─' * max(len(lines[r]), 3)
                by_row[r] = (vis, [(0, len(vis), DM)])
            i += 1; continue

        if tok.type in ('fence', 'code_block') and tok.map:
            r0, r1 = tok.map
            for r in range(r0, min(r1, len(by_row))):
                style = DM if (r == r0 or r == r1 - 1) else FC
                by_row[r] = (lines[r], [(0, len(lines[r]), style)])
            i += 1; continue

        if tok.type == 'table_open' and tok.map:
            j, d = i + 1, 1
            while j < len(tokens):
                if tokens[j].type == 'table_open':  d += 1
                if tokens[j].type == 'table_close':
                    d -= 1
                    if d == 0: break
                j += 1
            for r, v in _render_table_tokens(tokens[i:j + 1], lines).items():
                if r < len(by_row): by_row[r] = v
            i = j + 1; continue

        if tok.type == 'inline' and tok.map:
            r0 = tok.map[0]
            # Split children by softbreak → one visual line per source row
            groups: list = [[]]
            for child in (tok.children or []):
                if child.type in ('softbreak', 'hardbreak'): groups.append([])
                else: groups[-1].append(child)

            for gi, group in enumerate(groups):
                r = r0 + gi
                if r >= len(by_row): break
                vis, spans = _inline_render(group)
                raw = lines[r]
                # Detect block context from the raw buffer line, add visual prefix
                bm = re.match(r'^(>+\s?)', raw)
                lm = re.match(r'^(\s*)([-*+]|\d+\.)(\s)', raw)
                if bm:
                    vis   = '│ ' + vis
                    spans = [(0, 2, DM)] + [(s + 2, e + 2, st) for s, e, st in spans]
                    # re-apply quote color to unmarked text segments
                    spans = [(s, e, QU if st is None else st) for s, e, st in spans]
                elif lm:
                    indent = lm.group(1)
                    marker = lm.group(2)
                    pre    = indent + ('• ' if marker in '-*+' else marker + ' ')
                    vis    = pre + vis
                    spans  = [(s + len(pre), e + len(pre), st) for s, e, st in spans]
                by_row[r] = (vis, spans)
            i += 1; continue

        i += 1

    _md_cache[key] = by_row
    return by_row

# ── Editor ───────────────────────────────────────────────────────────────────

class Editor:
    def __init__(self, stdscr, filename=None):
        self.stdscr = stdscr
        self.mode   = Mode.NORMAL
        self.height = 0
        self.width  = 0

        # Pane management
        initial_buf = Buffer.from_file(filename) if filename else Buffer()
        p = Pane(initial_buf)
        self._layout  = _Leaf(p)
        self._panes   = [p]
        self._pane_i  = 0

        # Editor-global state
        self.register_text    = ''
        self.register_linewise= False
        self.search_pat  = ''
        self.search_dir  = 1
        self.cmd_line    = ''
        self.status_msg  = ''
        self.status_err  = False
        self.visual_start= None
        self.pending_op  = None
        self.pending_count = ''
        self.show_numbers  = True
        self.last_f_char   = None
        self.last_f_forward= True
        self.last_f_till   = False
        self.marks   = {}
        self.running = True

        # Explorer state
        self.ex_dir     = os.getcwd()
        self.ex_entries = []
        self.ex_sel     = 0
        self.ex_top     = 0
        self._prev_buf    = None
        self._prev_cursor = None

        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1,  curses.COLOR_BLACK,  curses.COLOR_CYAN)
        curses.init_pair(2,  curses.COLOR_BLACK,  curses.COLOR_YELLOW)
        curses.init_pair(3,  curses.COLOR_BLACK,  curses.COLOR_WHITE)
        curses.init_pair(4,  curses.COLOR_RED,    -1)
        curses.init_pair(5,  curses.COLOR_CYAN,   -1)
        curses.init_pair(6,  curses.COLOR_WHITE,  -1)
        curses.init_pair(7,  curses.COLOR_BLUE,   -1)
        curses.init_pair(8,  curses.COLOR_BLACK,  curses.COLOR_CYAN)
        # Syntax pairs
        curses.init_pair(9,  curses.COLOR_YELLOW, -1)   # keyword
        curses.init_pair(10, curses.COLOR_GREEN,  -1)   # string
        curses.init_pair(11, curses.COLOR_WHITE,  -1)   # comment (dim)
        curses.init_pair(12, curses.COLOR_MAGENTA,-1)   # number
        curses.init_pair(13, curses.COLOR_CYAN,   -1)   # type
        curses.init_pair(14, curses.COLOR_BLUE,   -1)   # builtin
        curses.init_pair(15, curses.COLOR_YELLOW, -1)   # decorator

        self.stdscr.keypad(True)
        curses.raw(); curses.noecho(); curses.curs_set(1)

    # ── Pane accessors (properties delegate to active pane) ──────────────────

    @property
    def _pane(self): return self._panes[self._pane_i]

    @property
    def buf(self): return self._pane.buf
    @buf.setter
    def buf(self, v): self._pane.buf = v

    @property
    def cursor(self): return self._pane.cursor
    @cursor.setter
    def cursor(self, v): self._pane.cursor = v

    @property
    def top_row(self): return self._pane.top_row
    @top_row.setter
    def top_row(self, v): self._pane.top_row = v

    @property
    def left_col(self): return self._pane.left_col
    @left_col.setter
    def left_col(self, v): self._pane.left_col = v

    def _pane_lnw(self, pane):
        return len(str(pane.buf.line_count())) + 1 if self.show_numbers else 0

    # ── Draw ─────────────────────────────────────────────────────────────────

    def draw(self):
        self.height, self.width = self.stdscr.getmaxyx()
        if self.mode == Mode.EXPLORER:
            self._draw_explorer(); return
        self.stdscr.erase()

        avail_h = self.height - 2
        divs = []
        _lc_compute(self._layout, 0, 0, avail_h, self.width, divs)

        for pane in _lc_panes(self._layout):
            self._draw_pane(pane, pane is self._pane)

        # Draw dividers
        for kind, dy, dx, dlen in divs:
            if kind == 'h':
                # Find pane whose bottom edge is this divider to show its name
                label = ''
                for p in _lc_panes(self._layout):
                    if p.y + p.height == dy:
                        fn = p.buf.filename or '[No Name]'
                        mod = '[+]' if p.buf.modified else ''
                        label = f' {fn} {mod} '
                        break
                bar = (label + '─' * self.width)[:self.width]
                self._as(dy, 0, bar, curses.color_pair(1))
            else:
                for i in range(dlen):
                    self._as(dy + i, dx, '│', curses.color_pair(1))

        self._draw_statusbar()
        self._place_cursor()
        self.stdscr.refresh()

    def _draw_pane(self, pane, is_active):
        lnw = self._pane_lnw(pane)
        tw  = pane.width - lnw
        lc  = pane.left_col

        vis_rows = set()
        vis_range = {}
        if is_active and self.mode in (Mode.VISUAL, Mode.VISUAL_LINE) and self.visual_start:
            sr, sc = self.visual_start
            er, ec = pane.cursor.row, pane.cursor.col
            if (sr, sc) > (er, ec): sr, sc, er, ec = er, ec, sr, sc
            for r in range(sr, er+1):
                vis_rows.add(r)
                if self.mode == Mode.VISUAL_LINE:
                    vis_range[r] = None
                else:
                    ll = len(pane.buf.get_line(r))
                    if sr == er:     vis_range[r] = (sc, ec)
                    elif r == sr:    vis_range[r] = (sc, ll)
                    elif r == er:    vis_range[r] = (0, ec)
                    else:            vis_range[r] = None

        is_md = _is_markdown(pane.buf.filename)
        # In insert mode show everything plain — no jarring render/raw flipping
        # as the cursor moves line to line while typing.
        in_insert = is_active and self.mode == Mode.INSERT
        md_table = (_md_highlight(pane.buf) if is_md and not in_insert else None)
        hl_table = (None if is_md else
                    _pg_highlight(pane.buf) if _detect_lang(pane.buf.filename) else None)

        try:
            spat = re.compile(self.search_pat,
                re.IGNORECASE if self.search_pat == self.search_pat.lower() else 0
            ) if self.search_pat else None
        except re.error:
            spat = None

        for sy in range(pane.height):
            br = pane.top_row + sy
            scr_y = pane.y + sy
            scr_x = pane.x

            if br >= pane.buf.line_count():
                self._as(scr_y, scr_x, '~', curses.A_DIM); continue

            if self.show_numbers:
                ns = str(br+1).rjust(lnw-1) + ' '
                na = (curses.color_pair(6)|curses.A_BOLD
                      if is_active and br == pane.cursor.row
                      else curses.color_pair(5))
                self._as(scr_y, scr_x, ns, na)

            line = pane.buf.get_line(br)
            if md_table:
                # Reveal raw for cursor line and visually-selected lines —
                # buffer col positions stay valid, no mapping needed.
                raw = (is_active and br == pane.cursor.row) or br in vis_rows
                display, hl_row = (line, []) if raw else (
                    md_table[br] if br < len(md_table) else (line, []))
            else:
                display = line
                hl_row = (hl_table[br] if hl_table and br < len(hl_table) else [])
            self._render_line(scr_y, scr_x + lnw, display, lc, tw,
                              vis_rows, vis_range, br, spat, hl_row, is_active)

    def _render_line(self, sy, sx, line, lc, tw, vis_rows, vis_range, br,
                     spat, hl_row, is_active):
        # Build span list: (start_in_line, end_in_line, attr)
        spans = []

        # 1. Syntax tokens from pygments (lowest priority)
        # hl_row entries: (start, end, (pair_idx, extra_attr)) — resolve here
        for ts, te, pg in hl_row:
            spans.append((ts, te, curses.color_pair(pg[0]) | pg[1]))

        # 2. Search matches (override syntax)
        if spat:
            for m in spat.finditer(line):
                spans.append((m.start(), m.end(), curses.color_pair(2)))

        # 3. Visual selection (highest priority, active pane only)
        if is_active and br in vis_rows:
            rng = vis_range.get(br)
            if rng is None:
                spans.append((0, len(line), curses.color_pair(3)))
            else:
                sc2, ec2 = rng
                spans.append((sc2, ec2+1, curses.color_pair(3)))

        # Sort spans so that higher-priority (later-added) ones can override
        # We draw in priority order: paint low→high, last wins per cell
        # Use a simple per-character attr array for the visible slice
        vis_len = tw
        attrs = [0] * vis_len

        for ts, te, attr in spans:
            for i in range(max(0, ts-lc), min(vis_len, te-lc)):
                attrs[i] = attr

        # Draw character by character (group consecutive same-attr runs)
        col_in_line = lc
        scr_col = sx
        i = 0
        while i < vis_len and col_in_line < len(line):
            a = attrs[i]
            j = i + 1
            while j < vis_len and attrs[j] == a and col_in_line + (j-i) < len(line):
                j += 1
            seg = line[col_in_line: col_in_line + (j-i)]
            if a:
                self._as(sy, scr_col, seg, a)
            else:
                self._as(sy, scr_col, seg)
            scr_col += len(seg); col_in_line += len(seg); i = j

        # Fill remainder with spaces (clear to end of pane width)
        rest = line[lc + i: lc + tw] if lc + i < len(line) else ''
        if rest:
            self._as(sy, scr_col, rest)

    def _draw_statusbar(self):
        _tbl_nm = (self.mode == Mode.NORMAL and
                   _is_table_row(self.buf.get_line(self.cursor.row)))
        ml = {
            Mode.NORMAL:' TABLE  ' if _tbl_nm else ' NORMAL ',
            Mode.INSERT:' INSERT ',
            Mode.VISUAL:' VISUAL ', Mode.VISUAL_LINE:' V-LINE ',
            Mode.COMMAND:' COMMAND', Mode.SEARCH:' SEARCH ',
        }.get(self.mode, ' NORMAL ')
        fname = self.buf.filename or '[No Name]'
        mod   = ' [+]' if self.buf.modified else ''
        pos   = f' {self.cursor.row+1}:{self.cursor.col+1} '

        if self.mode in (Mode.COMMAND, Mode.SEARCH):
            self._as(self.height-1, 0, (self.cmd_line+' '*self.width)[:self.width-1])
        elif self.status_msg:
            self._as(self.height-1, 0,
                     (self.status_msg+' '*self.width)[:self.width-1],
                     curses.color_pair(4) if self.status_err else 0)
        else:
            info = f' {ml}  {fname}{mod}'
            bar  = info.ljust(self.width-len(pos)-1) + pos
            self._as(self.height-1, 0, bar[:self.width-1], curses.color_pair(1))

        cnt  = f' {self.pending_count}' if self.pending_count else ''
        bar2 = (ml+cnt).ljust(self.width-1)
        self._as(self.height-2, 0, bar2[:self.width-1], curses.color_pair(1))

    def _place_cursor(self):
        if self.mode in (Mode.COMMAND, Mode.SEARCH):
            try: self.stdscr.move(self.height-1, min(len(self.cmd_line), self.width-1))
            except curses.error: pass
            return
        p = self._pane
        lnw = self._pane_lnw(p)
        sy = p.y + p.cursor.row - p.top_row
        sx = p.x + lnw + p.cursor.col - p.left_col
        try:
            self.stdscr.move(max(p.y, min(sy, p.y+p.height-1)),
                             max(p.x, min(sx, p.x+p.width-1)))
        except curses.error: pass

    def _as(self, y, x, text, attr=0):
        try:
            if y < 0 or y >= self.height or x < 0 or x >= self.width: return
            ml = self.width - x
            if ml <= 0: return
            if attr: self.stdscr.addstr(y, x, text[:ml], attr)
            else:    self.stdscr.addstr(y, x, text[:ml])
        except curses.error: pass

    # ── Cursor / scroll ───────────────────────────────────────────────────────

    def _clamp(self):
        p = self._pane
        p.cursor.row = max(0, min(p.cursor.row, p.buf.line_count()-1))
        line = p.buf.get_line(p.cursor.row)
        mc = len(line) if self.mode == Mode.INSERT else max(0, len(line)-1)
        p.cursor.col = max(0, min(p.cursor.col, mc))

    def _scroll(self):
        p = self._pane
        th = p.height
        lnw = self._pane_lnw(p)
        tw  = p.width - lnw
        if p.cursor.row < p.top_row: p.top_row = p.cursor.row
        elif p.cursor.row >= p.top_row + th: p.top_row = p.cursor.row - th + 1
        if p.cursor.col < p.left_col: p.left_col = max(0, p.cursor.col-5)
        elif p.cursor.col >= p.left_col + tw: p.left_col = p.cursor.col - tw + 6

    def _move(self, dr, dc):
        p = self._pane
        if dr:
            p.cursor.row = max(0, min(p.cursor.row+dr, p.buf.line_count()-1))
            line = p.buf.get_line(p.cursor.row)
            mc = max(0, len(line)-(0 if self.mode==Mode.INSERT else 1))
            p.cursor.col = min(p.cursor.col_want, mc)
        else:
            line = p.buf.get_line(p.cursor.row)
            mc = max(0, len(line)-(0 if self.mode==Mode.INSERT else 1))
            p.cursor.col = max(0, min(p.cursor.col+dc, mc))
            p.cursor.col_want = p.cursor.col

    def _cell_next(self):
        line = self.buf.get_line(self.cursor.row)
        cells = _table_cells(line)
        if not cells: return
        info = _table_cell_at(line, self.cursor.col)
        idx = info[0] if info else -1
        if idx + 1 < len(cells):
            self.cursor.col = cells[idx + 1][0]
        else:
            nr = self.cursor.row + 1
            if nr < self.buf.line_count():
                self.cursor.row = nr
                c2 = _table_cells(self.buf.get_line(nr))
                if c2: self.cursor.col = c2[0][0]

    def _cell_prev(self):
        line = self.buf.get_line(self.cursor.row)
        cells = _table_cells(line)
        if not cells: return
        info = _table_cell_at(line, self.cursor.col)
        idx = info[0] if info else len(cells)
        if idx > 0:
            self.cursor.col = cells[idx - 1][0]
        else:
            nr = self.cursor.row - 1
            if nr >= 0:
                self.cursor.row = nr
                c2 = _table_cells(self.buf.get_line(nr))
                if c2: self.cursor.col = c2[-1][0]

    def _cell_down(self):
        line = self.buf.get_line(self.cursor.row)
        info = _table_cell_at(line, self.cursor.col)
        idx = info[0] if info else 0
        nr = self.cursor.row + 1
        while nr < self.buf.line_count():
            nl = self.buf.get_line(nr)
            if _TABLE_SEP_RE.match(nl): nr += 1; continue
            c2 = _table_cells(nl)
            if c2:
                self.cursor.row = nr
                self.cursor.col = c2[min(idx, len(c2) - 1)][0]
                return
            break
        self._move(1, 0)

    def _cell_up(self):
        line = self.buf.get_line(self.cursor.row)
        info = _table_cell_at(line, self.cursor.col)
        idx = info[0] if info else 0
        nr = self.cursor.row - 1
        while nr >= 0:
            nl = self.buf.get_line(nr)
            if _TABLE_SEP_RE.match(nl): nr -= 1; continue
            c2 = _table_cells(nl)
            if c2:
                self.cursor.row = nr
                self.cursor.col = c2[min(idx, len(c2) - 1)][0]
                return
            break
        self._move(-1, 0)

    def _first_nonblank(self, row=None):
        r = self.cursor.row if row is None else row
        line = self.buf.get_line(r)
        for i, c in enumerate(line):
            if not c.isspace(): self.cursor.col = i; return
        self.cursor.col = 0

    # ── Word motions ──────────────────────────────────────────────────────────

    @staticmethod
    def _is_word(c): return c.isalnum() or c == '_'

    def _word_fwd(self, big=False):
        test = (lambda c: not c.isspace()) if big else self._is_word
        row, col = self.cursor.row, self.cursor.col
        line = self.buf.get_line(row)
        if col < len(line):
            if test(line[col]):
                while col < len(line) and test(line[col]): col += 1
            else:
                while col < len(line) and not line[col].isspace() and not test(line[col]): col += 1
        while True:
            while col < len(line) and line[col].isspace(): col += 1
            if col < len(line): break
            if row+1 >= self.buf.line_count(): break
            row += 1; line = self.buf.get_line(row); col = 0
        self.cursor.row = row
        self.cursor.col = min(col, max(0, len(self.buf.get_line(row))-1))

    def _word_bwd(self, big=False):
        test = (lambda c: not c.isspace()) if big else self._is_word
        row, col = self.cursor.row, self.cursor.col
        if col == 0:
            if row == 0: return
            row -= 1; col = len(self.buf.get_line(row))
        col -= 1
        line = self.buf.get_line(row)
        while col > 0 and line[col].isspace(): col -= 1
        if col >= 0 and not line[col].isspace():
            if test(line[col]):
                while col > 0 and test(line[col-1]): col -= 1
            else:
                while col > 0 and not line[col-1].isspace() and not test(line[col-1]): col -= 1
        self.cursor.row = row; self.cursor.col = col

    def _word_end(self, big=False):
        test = (lambda c: not c.isspace()) if big else self._is_word
        row, col = self.cursor.row, self.cursor.col
        line = self.buf.get_line(row)
        col += 1
        if col >= len(line):
            if row+1 < self.buf.line_count(): row += 1; line = self.buf.get_line(row); col = 0
        while col < len(line) and line[col].isspace(): col += 1
        if col < len(line):
            if test(line[col]):
                while col+1 < len(line) and test(line[col+1]): col += 1
            else:
                while col+1 < len(line) and not line[col+1].isspace() and not test(line[col+1]): col += 1
        self.cursor.row = row; self.cursor.col = col

    def _para_fwd(self):
        r = self.cursor.row
        while r < self.buf.line_count()-1 and self.buf.get_line(r).strip(): r += 1
        while r < self.buf.line_count()-1 and not self.buf.get_line(r).strip(): r += 1
        self.cursor.row = r; self._first_nonblank()

    def _para_bwd(self):
        r = self.cursor.row
        while r > 0 and self.buf.get_line(r).strip(): r -= 1
        while r > 0 and not self.buf.get_line(r-1).strip(): r -= 1
        self.cursor.row = r; self._first_nonblank()

    # ── Search ────────────────────────────────────────────────────────────────

    def _search_next(self, direction):
        if not self.search_pat: return
        try:
            flags = re.IGNORECASE if self.search_pat==self.search_pat.lower() else 0
            pat = re.compile(self.search_pat, flags)
        except re.error:
            self.status_msg = f'Bad regex: {self.search_pat}'; return
        row, col = self.cursor.row, self.cursor.col
        if direction > 0:
            for r in range(row, self.buf.line_count()):
                m = pat.search(self.buf.get_line(r), col+1 if r==row else 0)
                if m: self.cursor.row, self.cursor.col = r, m.start(); return
            for r in range(0, row+1):
                m = pat.search(self.buf.get_line(r))
                if m: self.cursor.row, self.cursor.col = r, m.start(); self.status_msg='search wrapped'; return
        else:
            for r in range(row, -1, -1):
                ms = [m for m in pat.finditer(self.buf.get_line(r)) if r<row or m.start()<col]
                if ms: m=ms[-1]; self.cursor.row,self.cursor.col=r,m.start(); return
            for r in range(self.buf.line_count()-1, row-1, -1):
                ms = list(pat.finditer(self.buf.get_line(r)))
                if r==row: ms=[m for m in ms if m.start()>=col]
                if ms: m=ms[-1]; self.cursor.row,self.cursor.col=r,m.start(); self.status_msg='search wrapped'; return
        self.status_msg = f'Pattern not found: {self.search_pat}'

    # ── Operators ─────────────────────────────────────────────────────────────

    def _op_lines(self, op, r1, r2):
        r1=max(0,r1); r2=min(self.buf.line_count()-1,r2)
        self.register_text = '\n'.join(self.buf.lines[r1:r2+1])
        self.register_linewise = True
        if op in ('d','c'):
            self.buf.save_undo()
            n = r2-r1+1
            for _ in range(n):
                if self.buf.line_count()>1: self.buf.delete_line(r1)
                else: self.buf.set_line(0,'')
            self.cursor.row = min(r1, self.buf.line_count()-1)
            self._first_nonblank()
            if op=='c':
                ind=self._indent(self.cursor.row)
                self.buf.save_undo(); self.buf.insert_line(self.cursor.row,ind)
                self.buf.delete_line(self.cursor.row+1); self.cursor.col=len(ind)
                self.mode=Mode.INSERT
        elif op=='y':
            self.cursor.row=r1; self._first_nonblank()
            self.status_msg=f'Yanked {r2-r1+1} lines'

    def _op_range(self, op, r1, c1, r2, c2):
        if r1==r2:
            line=self.buf.get_line(r1); self.register_text=line[c1:c2]; self.register_linewise=False
            if op in ('d','c'):
                self.buf.save_undo(); self.buf.set_line(r1,line[:c1]+line[c2:])
                self.cursor.row,self.cursor.col=r1,c1
                if op=='c': self.mode=Mode.INSERT
        else:
            parts=[self.buf.get_line(r1)[c1:]]
            for r in range(r1+1,r2): parts.append(self.buf.get_line(r))
            parts.append(self.buf.get_line(r2)[:c2])
            self.register_text='\n'.join(parts); self.register_linewise=False
            if op in ('d','c'):
                self.buf.save_undo()
                nl=self.buf.get_line(r1)[:c1]+self.buf.get_line(r2)[c2:]
                self.buf.set_line(r1,nl)
                for r in range(r2,r1,-1): self.buf.delete_line(r)
                self.cursor.row,self.cursor.col=r1,c1
                if op=='c': self.mode=Mode.INSERT

    # ── Text objects ──────────────────────────────────────────────────────────

    def _text_object(self, kind, obj):
        row,col=self.cursor.row,self.cursor.col; line=self.buf.get_line(row)
        if obj in ('w','W'):
            test=(lambda c:not c.isspace()) if obj=='W' else self._is_word
            s=col
            while s>0 and test(line[s-1]): s-=1
            e=col
            while e<len(line) and test(line[e]): e+=1
            if kind=='a':
                while e<len(line) and line[e]==' ': e+=1
            return row,s,row,e
        if obj in ('"',"'",'`'):
            q=obj; s=line.rfind(q,0,col)
            if s==-1:
                s=line.find(q,col)
                if s==-1: return None,None,None,None
                e=line.find(q,s+1)
            else: e=line.find(q,s+1)
            if e==-1: return None,None,None,None
            return (row,s+1,row,e) if kind=='i' else (row,s,row,e+1)
        pairs={'(':('(',')'),')':(  '(',')'),'b':('(',')'),'{':('{','}'),
               '}':('{','}'),'B':('{','}'),'[':('[',']'),']':('[',']'),
               '<':('<','>'),'>':(  '<','>')}
        if obj in pairs:
            oc,cc=pairs[obj]; return self._find_pair(row,col,oc,cc,kind=='a')
        return None,None,None,None

    def _find_pair(self, row, col, oc, cc, inclusive):
        depth=0; r,c=row,col; sr,sc=None,None
        while r>=0:
            ln=self.buf.get_line(r); end=c if r==row else len(ln)-1
            for i in range(min(end,len(ln)-1),-1,-1):
                if ln[i]==cc: depth+=1
                elif ln[i]==oc:
                    if depth==0: sr,sc=r,i; break
                    depth-=1
            if sr is not None: break
            r-=1; c=99999
        if sr is None: return None,None,None,None
        depth=0; r,c=sr,sc; er,ec=None,None
        while r<self.buf.line_count():
            ln=self.buf.get_line(r); s2=c if r==sr else 0
            for i in range(s2,len(ln)):
                if ln[i]==oc: depth+=1
                elif ln[i]==cc:
                    depth-=1
                    if depth==0: er,ec=r,i; break
            if er is not None: break
            r+=1; c=0
        if er is None: return None,None,None,None
        return (sr,sc,er,ec+1) if inclusive else (sr,sc+1,er,ec)

    # ── Paste / indent / misc ─────────────────────────────────────────────────

    def _paste(self, after):
        if not self.register_text: return
        self.buf.save_undo()
        if self.register_linewise:
            lines=self.register_text.split('\n')
            ins=self.cursor.row+(1 if after else 0)
            for i,l in enumerate(lines): self.buf.insert_line(ins+i,l)
            self.cursor.row=ins; self._first_nonblank()
        else:
            line=self.buf.get_line(self.cursor.row)
            ipos=self.cursor.col+(1 if after and line else 0)
            self.buf.set_line(self.cursor.row,line[:ipos]+self.register_text+line[ipos:])
            self.cursor.col=ipos+len(self.register_text)-1

    def _indent(self, row):
        line=self.buf.get_line(row); return line[:len(line)-len(line.lstrip())]

    def _indent_lines(self, r1, r2, direction):
        self.buf.save_undo()
        for r in range(r1,r2+1):
            line=self.buf.get_line(r)
            if direction>0: self.buf.set_line(r,'    '+line)
            else:
                if line.startswith('    '): self.buf.set_line(r,line[4:])
                elif line.startswith('\t'): self.buf.set_line(r,line[1:])

    def _word_under_cursor(self):
        line=self.buf.get_line(self.cursor.row); col=self.cursor.col
        if col>=len(line) or not self._is_word(line[col]): return None
        s=col
        while s>0 and self._is_word(line[s-1]): s-=1
        e=col
        while e<len(line) and self._is_word(line[e]): e+=1
        return line[s:e]

    def _char_search(self, ch, forward, till, count=1):
        self.last_f_char=ch; self.last_f_forward=forward; self.last_f_till=till
        line=self.buf.get_line(self.cursor.row); col=self.cursor.col; found=0
        if forward:
            for i in range(col+1,len(line)):
                if line[i]==ch:
                    found+=1
                    if found==count: self.cursor.col=i-(1 if till else 0); return
        else:
            for i in range(col-1,-1,-1):
                if line[i]==ch:
                    found+=1
                    if found==count: self.cursor.col=i+(1 if till else 0); return

    # ── Splits ────────────────────────────────────────────────────────────────

    def _split(self, vertical, filename=None):
        new_buf = Buffer.from_file(filename) if filename else Buffer.from_file(self.buf.filename) if self.buf.filename else Buffer(list(self.buf.lines))
        new_pane = Pane(new_buf)
        new_pane.cursor.row = self.cursor.row
        new_pane.cursor.col = self.cursor.col
        new_pane.top_row    = self.top_row
        orig_i = self._pane_i
        self._layout = _lc_split(self._layout, self._pane, new_pane, vertical)
        self._panes.append(new_pane)
        self._pane_i = orig_i  # stay in original pane, new one is below/right

    def _close_pane(self, force=False):
        if len(self._panes) == 1:
            # Last pane: behave like :q
            if self.buf.modified and not force:
                self.status_msg='No write since last change (add ! to override)'
                self.status_err=True; return
            self.running=False; return
        p=self._pane
        if p.buf.modified and not force:
            self.status_msg='No write since last change (add ! to override)'
            self.status_err=True; return
        new_layout=_lc_close(self._layout, p)
        if new_layout is None: self.running=False; return
        self._layout=new_layout
        self._panes=[x for x in self._panes if x is not p]
        self._pane_i=min(self._pane_i, len(self._panes)-1)

    def _next_pane(self, delta=1):
        self._pane_i=(self._pane_i+delta)%len(self._panes)

    def _goto_pane_dir(self, d):
        cur=self._pane; best=None; best_score=10**9
        for i, p in enumerate(self._panes):
            if p is cur: continue
            ov_h = p.y < cur.y+cur.height and p.y+p.height > cur.y
            ov_v = p.x < cur.x+cur.width  and p.x+p.width  > cur.x
            if   d=='h' and p.x+p.width < cur.x and ov_h:
                score = cur.x - (p.x+p.width)
            elif d=='l' and p.x > cur.x+cur.width and ov_h:
                score = p.x - (cur.x+cur.width)
            elif d=='k' and p.y+p.height < cur.y and ov_v:
                score = cur.y - (p.y+p.height)
            elif d=='j' and p.y > cur.y+cur.height and ov_v:
                score = p.y - (cur.y+cur.height)
            else: continue
            if score < best_score: best_score=score; best=i
        if best is not None: self._pane_i=best

    # ── Mode transitions ──────────────────────────────────────────────────────

    def _enter_insert(self, col_delta=0):
        self.buf.save_undo(); self.cursor.col+=col_delta
        self.mode=Mode.INSERT; self.status_msg=''

    def _enter_normal(self):
        self.mode=Mode.NORMAL; self.pending_op=None; self.pending_count=''
        line=self.buf.get_line(self.cursor.row)
        if self.cursor.col>0 and self.cursor.col>=len(line):
            self.cursor.col=max(0,len(line)-1)
        self.status_msg=''

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        while self.running:
            self._clamp(); self._scroll(); self.draw()
            key=self.stdscr.getch()
            self.status_msg=''; self.status_err=False
            self._dispatch(key)

    def _dispatch(self, key):
        if   self.mode==Mode.NORMAL:      self._normal(key)
        elif self.mode==Mode.INSERT:      self._insert(key)
        elif self.mode==Mode.VISUAL:      self._visual(key)
        elif self.mode==Mode.VISUAL_LINE: self._visual_line(key)
        elif self.mode==Mode.COMMAND:     self._command_key(key)
        elif self.mode==Mode.SEARCH:      self._search_key(key)
        elif self.mode==Mode.EXPLORER:    self._explorer_key(key)

    # ── Normal mode ───────────────────────────────────────────────────────────

    def _normal(self, key):
        ch=chr(key) if 32<=key<=126 else None
        if ch and ch.isdigit() and (ch!='0' or self.pending_count):
            self.pending_count+=ch; return
        count=int(self.pending_count) if self.pending_count else 1
        self.pending_count=''

        _on_tbl = _is_table_row(self.buf.get_line(self.cursor.row))
        if   key in (curses.KEY_LEFT,)  or ch=='h':
            if _on_tbl: [self._cell_prev() for _ in range(count)]
            else: [self._move(0,-1) for _ in range(count)]
        elif key in (curses.KEY_RIGHT,) or ch=='l':
            if _on_tbl: [self._cell_next() for _ in range(count)]
            else: [self._move(0,1) for _ in range(count)]
        elif key in (curses.KEY_UP,)    or ch=='k':
            if _on_tbl: [self._cell_up()   for _ in range(count)]
            else: [self._move(-1,0) for _ in range(count)]
        elif key in (curses.KEY_DOWN,)  or ch=='j':
            if _on_tbl: [self._cell_down() for _ in range(count)]
            else: [self._move(1,0)  for _ in range(count)]
        elif key==9 and _on_tbl: [self._cell_next() for _ in range(count)]
        elif key==353 and _on_tbl: [self._cell_prev() for _ in range(count)]  # Shift-Tab
        elif ch=='0': self.cursor.col=0; self.cursor.col_want=0
        elif ch=='^': self._first_nonblank()
        elif ch=='$':
            line=self.buf.get_line(self.cursor.row)
            self.cursor.col=max(0,len(line)-1); self.cursor.col_want=999999
        elif ch=='G':
            self.cursor.row=(count-1 if self.pending_count else self.buf.line_count()-1)
            self._first_nonblank()
        elif ch=='g': self._normal_g(count)
        elif ch=='w': [self._word_fwd(False) for _ in range(count)]
        elif ch=='W': [self._word_fwd(True)  for _ in range(count)]
        elif ch=='b': [self._word_bwd(False) for _ in range(count)]
        elif ch=='B': [self._word_bwd(True)  for _ in range(count)]
        elif ch=='e': [self._word_end(False) for _ in range(count)]
        elif ch=='E': [self._word_end(True)  for _ in range(count)]
        elif ch=='{': [self._para_bwd() for _ in range(count)]
        elif ch=='}': [self._para_fwd() for _ in range(count)]
        elif ch=='f':
            k2=self.stdscr.getch()
            if 32<=k2<=126: self._char_search(chr(k2),True,False,count)
        elif ch=='F':
            k2=self.stdscr.getch()
            if 32<=k2<=126: self._char_search(chr(k2),False,False,count)
        elif ch=='t':
            k2=self.stdscr.getch()
            if 32<=k2<=126: self._char_search(chr(k2),True,True,count)
        elif ch=='T':
            k2=self.stdscr.getch()
            if 32<=k2<=126: self._char_search(chr(k2),False,True,count)
        elif ch==';' and self.last_f_char: self._char_search(self.last_f_char,self.last_f_forward,self.last_f_till,count)
        elif ch==',' and self.last_f_char: self._char_search(self.last_f_char,not self.last_f_forward,self.last_f_till,count)
        elif key==6  or key==curses.KEY_NPAGE: self._scroll_page(count)
        elif key==2  or key==curses.KEY_PPAGE: self._scroll_page(-count)
        elif key==4:  self._scroll_half(count)
        elif key==21 and self.mode==Mode.NORMAL: self._scroll_half(-count)
        elif ch=='i': self._enter_insert()
        elif ch=='I': self._first_nonblank(); self._enter_insert()
        elif ch=='a':
            line=self.buf.get_line(self.cursor.row)
            self._enter_insert(1 if line else 0)
        elif ch=='A':
            line=self.buf.get_line(self.cursor.row)
            self.cursor.col=len(line); self._enter_insert()
        elif ch=='o':
            self.buf.save_undo(); ind=self._indent(self.cursor.row)
            self.buf.insert_line(self.cursor.row+1,ind)
            self.cursor.row+=1; self.cursor.col=len(ind); self.mode=Mode.INSERT
        elif ch=='O':
            self.buf.save_undo(); ind=self._indent(self.cursor.row)
            self.buf.insert_line(self.cursor.row,ind)
            self.cursor.col=len(ind); self.mode=Mode.INSERT
        elif ch=='x':
            self.buf.save_undo()
            for _ in range(count):
                c=self.buf.delete_char(self.cursor.row,self.cursor.col)
                if c: self.register_text=c; self.register_linewise=False
        elif ch=='X':
            self.buf.save_undo()
            for _ in range(count):
                if self.cursor.col>0:
                    self.cursor.col-=1; self.buf.delete_char(self.cursor.row,self.cursor.col)
        elif ch=='r':
            k2=self.stdscr.getch()
            if 32<=k2<=126:
                self.buf.save_undo(); line=self.buf.get_line(self.cursor.row)
                if self.cursor.col<len(line):
                    self.buf.set_line(self.cursor.row,
                        line[:self.cursor.col]+chr(k2)+line[self.cursor.col+1:])
        elif ch=='s':
            self.buf.save_undo()
            for _ in range(count): self.buf.delete_char(self.cursor.row,self.cursor.col)
            self.mode=Mode.INSERT
        elif ch=='S':
            self.buf.save_undo(); ind=self._indent(self.cursor.row)
            self.buf.set_line(self.cursor.row,ind); self.cursor.col=len(ind); self.mode=Mode.INSERT
        elif ch=='C':
            self.buf.save_undo(); line=self.buf.get_line(self.cursor.row)
            self.register_text=line[self.cursor.col:]; self.register_linewise=False
            self.buf.set_line(self.cursor.row,line[:self.cursor.col]); self.mode=Mode.INSERT
        elif ch=='D':
            self.buf.save_undo(); line=self.buf.get_line(self.cursor.row)
            self.register_text=line[self.cursor.col:]; self.register_linewise=False
            self.buf.set_line(self.cursor.row,line[:self.cursor.col])
        elif ch=='~':
            self.buf.save_undo(); line=self.buf.get_line(self.cursor.row)
            if self.cursor.col<len(line):
                c=line[self.cursor.col]
                self.buf.set_line(self.cursor.row,
                    line[:self.cursor.col]+c.swapcase()+line[self.cursor.col+1:])
                self._move(0,1)
        elif ch=='J':
            self.buf.save_undo()
            for _ in range(count):
                line=self.buf.get_line(self.cursor.row)
                nxt=self.buf.get_line(self.cursor.row+1).lstrip()
                self.buf.set_line(self.cursor.row,(line.rstrip()+' '+nxt) if nxt else line.rstrip())
                if self.cursor.row+1<self.buf.line_count(): self.buf.delete_line(self.cursor.row+1)
        elif ch=='d': self._await_motion('d',count)
        elif ch=='c': self._await_motion('c',count)
        elif ch=='y': self._await_motion('y',count)
        elif ch=='>': self._indent_lines(self.cursor.row,self.cursor.row,1)
        elif ch=='<': self._indent_lines(self.cursor.row,self.cursor.row,-1)
        elif ch=='p': self._paste(True)
        elif ch=='P': self._paste(False)
        elif ch=='u':
            if not self.buf.undo(): self.status_msg='Already at oldest change'
            else: self.status_msg='Undo'; self._clamp()
        elif key==18:
            if not self.buf.redo(): self.status_msg='Already at newest change'
            else: self.status_msg='Redo'; self._clamp()
        elif ch=='v': self.mode=Mode.VISUAL;      self.visual_start=(self.cursor.row,self.cursor.col)
        elif ch=='V': self.mode=Mode.VISUAL_LINE; self.visual_start=(self.cursor.row,self.cursor.col)
        elif ch=='/': self.mode=Mode.SEARCH; self.search_dir=1;  self.cmd_line='/'
        elif ch=='?': self.mode=Mode.SEARCH; self.search_dir=-1; self.cmd_line='?'
        elif ch=='n': self._search_next(self.search_dir)
        elif ch=='N': self._search_next(-self.search_dir)
        elif ch=='*':
            w=self._word_under_cursor()
            if w: self.search_pat=r'\b'+re.escape(w)+r'\b'; self._search_next(1)
        elif ch=='#':
            w=self._word_under_cursor()
            if w: self.search_pat=r'\b'+re.escape(w)+r'\b'; self._search_next(-1)
        elif ch=='m':
            k2=self.stdscr.getch()
            if 32<=k2<=126 and chr(k2).isalpha(): self.marks[chr(k2)]=(self.cursor.row,self.cursor.col)
        elif ch=="'":
            k2=self.stdscr.getch()
            if 32<=k2<=126 and chr(k2) in self.marks:
                self.cursor.row=self.marks[chr(k2)][0]; self._first_nonblank()
        elif ch=='`':
            k2=self.stdscr.getch()
            if 32<=k2<=126 and chr(k2) in self.marks:
                self.cursor.row,self.cursor.col=self.marks[chr(k2)]
        elif ch==':': self.mode=Mode.COMMAND; self.cmd_line=':'
        elif ch=='z': self._normal_z()
        elif key==5:  self._eval_region(self.cursor.row, self.cursor.row)  # Ctrl-E
        elif key==23: self._ctrl_w()   # Ctrl-W
        elif key==26:                  # Ctrl-Z suspend
            import signal
            curses.endwin()
            os.kill(os.getpid(), signal.SIGTSTP)
            self.stdscr.refresh()
        elif key==27: self.search_pat=''; self.status_msg=''
        elif key==curses.KEY_HOME: self.cursor.col=0
        elif key==curses.KEY_END:
            self.cursor.col=max(0,len(self.buf.get_line(self.cursor.row))-1)

    def _normal_g(self, count):
        k2=self.stdscr.getch(); ch2=chr(k2) if 32<=k2<=126 else None
        if ch2=='g': self.cursor.row=max(0,count-1); self._first_nonblank()
        elif ch2=='_':
            line=self.buf.get_line(self.cursor.row); self.cursor.col=max(0,len(line.rstrip())-1)
        elif ch2=='e': self._word_end(False)
        elif ch2=='E': self._word_end(True)

    def _normal_z(self):
        k2=self.stdscr.getch(); ch2=chr(k2) if 32<=k2<=126 else None
        th=self._pane.height
        if   ch2=='z': self.top_row=max(0,self.cursor.row-th//2)
        elif ch2=='t': self.top_row=self.cursor.row
        elif ch2=='b': self.top_row=max(0,self.cursor.row-th+1)

    def _ctrl_w(self):
        k2=self.stdscr.getch(); ch2=chr(k2) if 32<=k2<=126 else None
        if   k2==23 or ch2=='w': self._next_pane(1)
        elif ch2=='W':           self._next_pane(-1)
        elif ch2=='h' or k2==curses.KEY_LEFT:  self._goto_pane_dir('h')
        elif ch2=='l' or k2==curses.KEY_RIGHT: self._goto_pane_dir('l')
        elif ch2=='j' or k2==curses.KEY_DOWN:  self._goto_pane_dir('j')
        elif ch2=='k' or k2==curses.KEY_UP:    self._goto_pane_dir('k')
        elif ch2 in ('q','c'): self._close_pane()
        elif ch2=='v': self._split(vertical=True)
        elif ch2=='s': self._split(vertical=False)
        elif ch2=='=': pass  # equalize (TODO)

    def _scroll_page(self, count):
        p=self._pane; th=p.height; dr=th*abs(count)*(1 if count>0 else -1)
        p.top_row=max(0,min(p.top_row+dr,p.buf.line_count()-1))
        p.cursor.row=max(0,min(p.cursor.row+dr,p.buf.line_count()-1)); self._clamp()

    def _scroll_half(self, count):
        p=self._pane; half=max(1,p.height//2); dr=half*abs(count)*(1 if count>0 else -1)
        p.top_row=max(0,min(p.top_row+dr,p.buf.line_count()-1))
        p.cursor.row=max(0,min(p.cursor.row+dr,p.buf.line_count()-1)); self._clamp()

    # ── Operator + motion ─────────────────────────────────────────────────────

    def _await_motion(self, op, count):
        k2=self.stdscr.getch(); ch2=chr(k2) if 32<=k2<=126 else None
        if ch2 is None: return
        if ch2==op:
            r1=self.cursor.row; r2=min(r1+count-1,self.buf.line_count()-1)
            self._op_lines(op,r1,r2); return
        if ch2 in ('i','a'):
            k3=self.stdscr.getch()
            if 32<=k3<=126:
                r1,c1,r2,c2=self._text_object(ch2,chr(k3))
                if r1 is not None: self._op_range(op,r1,c1,r2,c2)
            return
        orig_r,orig_c=self.cursor.row,self.cursor.col
        if ch2=='k' or k2==curses.KEY_UP:
            self._op_lines(op,max(0,orig_r-count),orig_r); return
        if ch2=='j' or k2==curses.KEY_DOWN:
            self._op_lines(op,orig_r,min(orig_r+count,self.buf.line_count()-1)); return
        if ch2=='G':
            self._op_lines(op,orig_r,self.buf.line_count()-1); return
        if ch2=='g':
            k3=self.stdscr.getch()
            if chr(k3)=='g': self._op_lines(op,0,orig_r)
            return
        moved=True
        if   ch2=='h' or k2==curses.KEY_LEFT:  self._move(0,-count)
        elif ch2=='l' or k2==curses.KEY_RIGHT:  self._move(0, count)
        elif ch2=='0': self.cursor.col=0
        elif ch2=='^': self._first_nonblank()
        elif ch2=='$':
            line=self.buf.get_line(self.cursor.row); self.cursor.col=len(line)
        elif ch2=='w':
            for _ in range(count): self._word_fwd(False)
        elif ch2=='W':
            for _ in range(count): self._word_fwd(True)
        elif ch2=='b':
            for _ in range(count): self._word_bwd(False)
        elif ch2=='e':
            for _ in range(count): self._word_end(False); self.cursor.col+=1
        elif ch2=='E':
            for _ in range(count): self._word_end(True);  self.cursor.col+=1
        elif ch2=='f':
            k3=self.stdscr.getch()
            if 32<=k3<=126: self._char_search(chr(k3),True,False,count); self.cursor.col+=1
        elif ch2=='t':
            k3=self.stdscr.getch()
            if 32<=k3<=126: self._char_search(chr(k3),True,True,count); self.cursor.col+=1
        else: moved=False
        if moved:
            nr,nc=self.cursor.row,self.cursor.col
            self.cursor.row,self.cursor.col=orig_r,orig_c
            if (orig_r,orig_c)<=(nr,nc): self._op_range(op,orig_r,orig_c,nr,nc)
            else: self._op_range(op,nr,nc,orig_r,orig_c)
            if op!='c':
                self.cursor.row=min(orig_r,nr)
                self.cursor.col=(min(orig_c,nc) if orig_r==nr
                                 else orig_c if orig_r<nr else nc)

    # ── Insert mode ───────────────────────────────────────────────────────────

    def _insert(self, key):
        ch=chr(key) if 32<=key<=126 else None
        if key==27: self._enter_normal()
        elif key in (curses.KEY_BACKSPACE,127,8):
            if self.cursor.col>0:
                self.cursor.col-=1; self.buf.delete_char(self.cursor.row,self.cursor.col)
            elif self.cursor.row>0:
                prev_len=len(self.buf.get_line(self.cursor.row-1))
                self.buf.join_lines(self.cursor.row-1)
                self.cursor.row-=1; self.cursor.col=prev_len
        elif key==curses.KEY_DC: self.buf.delete_char(self.cursor.row,self.cursor.col)
        elif key in (10,13):
            ind=self._indent(self.cursor.row)
            self.buf.split_line(self.cursor.row,self.cursor.col)
            self.cursor.row+=1; self.cursor.col=len(ind)
            self.buf.set_line(self.cursor.row,ind+self.buf.get_line(self.cursor.row))
        elif key==9:
            if _is_table_row(self.buf.get_line(self.cursor.row)): self._cell_next()
            else:
                for _ in range(4): self.buf.insert_char(self.cursor.row,self.cursor.col,' '); self.cursor.col+=1
        elif key==353:  # Shift-Tab
            if _is_table_row(self.buf.get_line(self.cursor.row)): self._cell_prev()
        elif key==23:  # Ctrl-W delete word back
            line=self.buf.get_line(self.cursor.row); c=self.cursor.col
            while c>0 and line[c-1]==' ': c-=1
            while c>0 and line[c-1]!=' ': c-=1
            n=self.cursor.col-c
            for _ in range(n): self.buf.delete_char(self.cursor.row,c)
            self.cursor.col=c
        elif key==21:  # Ctrl-U delete to line start
            line=self.buf.get_line(self.cursor.row)
            self.buf.set_line(self.cursor.row,line[self.cursor.col:]); self.cursor.col=0
        elif key in (curses.KEY_LEFT,):  self._move(0,-1)
        elif key in (curses.KEY_RIGHT,): self._move(0, 1)
        elif key in (curses.KEY_UP,):    self._move(-1,0)
        elif key in (curses.KEY_DOWN,):  self._move(1, 0)
        elif key==curses.KEY_HOME: self.cursor.col=0
        elif key==curses.KEY_END:  self.cursor.col=len(self.buf.get_line(self.cursor.row))
        elif ch is not None: self.buf.insert_char(self.cursor.row,self.cursor.col,ch); self.cursor.col+=1

    # ── Visual modes ──────────────────────────────────────────────────────────

    def _vmove(self, key, count=1):
        """Handle a motion key in visual mode. Returns True if consumed."""
        ch=chr(key) if 32<=key<=126 else None
        if   key==curses.KEY_LEFT  or ch=='h': [self._move(0,-1) for _ in range(count)]
        elif key==curses.KEY_RIGHT or ch=='l': [self._move(0, 1) for _ in range(count)]
        elif key==curses.KEY_UP    or ch=='k': [self._move(-1,0) for _ in range(count)]
        elif key==curses.KEY_DOWN  or ch=='j': [self._move(1, 0) for _ in range(count)]
        elif ch=='0': self.cursor.col=0; self.cursor.col_want=0
        elif ch=='^': self._first_nonblank()
        elif ch=='$':
            self.cursor.col=max(0,len(self.buf.get_line(self.cursor.row))-1)
            self.cursor.col_want=999999
        elif ch=='w': [self._word_fwd(False) for _ in range(count)]
        elif ch=='W': [self._word_fwd(True)  for _ in range(count)]
        elif ch=='b': [self._word_bwd(False) for _ in range(count)]
        elif ch=='B': [self._word_bwd(True)  for _ in range(count)]
        elif ch=='e': [self._word_end(False) for _ in range(count)]
        elif ch=='E': [self._word_end(True)  for _ in range(count)]
        elif ch=='{': [self._para_bwd() for _ in range(count)]
        elif ch=='}': [self._para_fwd() for _ in range(count)]
        elif key==6  or key==curses.KEY_NPAGE: self._scroll_page(count)
        elif key==2  or key==curses.KEY_PPAGE: self._scroll_page(-count)
        elif key==4:  self._scroll_half(count)
        elif key==21: self._scroll_half(-count)
        elif ch=='f':
            k2=self.stdscr.getch()
            if 32<=k2<=126: self._char_search(chr(k2),True,False,count)
        elif ch=='F':
            k2=self.stdscr.getch()
            if 32<=k2<=126: self._char_search(chr(k2),False,False,count)
        elif ch=='t':
            k2=self.stdscr.getch()
            if 32<=k2<=126: self._char_search(chr(k2),True,True,count)
        elif ch=='T':
            k2=self.stdscr.getch()
            if 32<=k2<=126: self._char_search(chr(k2),False,True,count)
        elif ch==';' and self.last_f_char: self._char_search(self.last_f_char,self.last_f_forward,self.last_f_till,count)
        elif ch==',' and self.last_f_char: self._char_search(self.last_f_char,not self.last_f_forward,self.last_f_till,count)
        elif ch=='G': self.cursor.row=self.buf.line_count()-1; self._first_nonblank()
        elif ch=='g':
            k2=self.stdscr.getch()
            if 32<=k2<=126 and chr(k2)=='g': self.cursor.row=0; self._first_nonblank()
        elif key==curses.KEY_HOME: self.cursor.col=0
        elif key==curses.KEY_END:
            self.cursor.col=max(0,len(self.buf.get_line(self.cursor.row))-1)
        else: return False
        return True

    def _vis_yank(self):
        r1,c1,r2,c2=self._vis_range()
        if r1==r2: self.register_text=self.buf.get_line(r1)[c1:c2+1]
        else:
            pts=[self.buf.get_line(r1)[c1:]]
            for r in range(r1+1,r2): pts.append(self.buf.get_line(r))
            pts.append(self.buf.get_line(r2)[:c2+1])
            self.register_text='\n'.join(pts)
        self.register_linewise=False
        self.cursor.row,self.cursor.col=r1,c1

    def _vis_case(self, fn):
        r1,c1,r2,c2=self._vis_range()
        self.buf.save_undo()
        if r1==r2:
            ln=self.buf.get_line(r1)
            self.buf.set_line(r1, ln[:c1]+fn(ln[c1:c2+1])+ln[c2+1:])
        else:
            ln=self.buf.get_line(r1); self.buf.set_line(r1, ln[:c1]+fn(ln[c1:]))
            for r in range(r1+1,r2): self.buf.set_line(r, fn(self.buf.get_line(r)))
            ln=self.buf.get_line(r2); self.buf.set_line(r2, fn(ln[:c2+1])+ln[c2+1:])
        self._enter_normal()

    def _visual(self, key):
        ch=chr(key) if 32<=key<=126 else None

        # Count prefix
        if ch and ch.isdigit() and (ch!='0' or self.pending_count):
            self.pending_count+=ch; return
        count=int(self.pending_count) if self.pending_count else 1
        self.pending_count=''

        if key==27 or ch=='v': self._enter_normal(); return
        if ch=='V':
            self.mode=Mode.VISUAL_LINE
            self.visual_start=(self.visual_start[0], 0)
            return
        if ch=='o':  # swap cursor and anchor
            self.visual_start, (self.cursor.row, self.cursor.col) = \
                (self.cursor.row, self.cursor.col), self.visual_start
            return
        if ch==':':
            self.mode=Mode.COMMAND; self.cmd_line=":'<,'>"; return
        if key==5:   # Ctrl-E — eval selection
            sr,sc=self.visual_start; er,ec=self.cursor.row,self.cursor.col
            if (sr,sc)>(er,ec): sr,sc,er,ec=er,ec,sr,sc
            self._eval_region(sr, er); self._enter_normal(); return
        if ch=='n': self._search_next(self.search_dir); return
        if ch=='N': self._search_next(-self.search_dir); return
        if self._vmove(key, count): return

        r1,c1,r2,c2=self._vis_range()
        if ch in ('d','x'):
            self._op_range('d',r1,c1,r2,c2+1); self._enter_normal()
        elif ch=='y':
            self._vis_yank(); self._enter_normal()
            self.status_msg='Yanked'
        elif ch=='c': self._op_range('c',r1,c1,r2,c2+1)
        elif ch=='s': self._op_range('c',r1,c1,r2,c2+1)
        elif ch=='p' or ch=='P':
            # Replace selection with register
            self._op_range('d',r1,c1,r2,c2+1)
            self.cursor.row,self.cursor.col=r1,c1
            self._paste(False)
            self._enter_normal()
        elif ch=='>': self._indent_lines(r1,r2,1); self._enter_normal()
        elif ch=='<': self._indent_lines(r1,r2,-1); self._enter_normal()
        elif ch=='~': self._vis_case(str.swapcase)
        elif ch=='u': self._vis_case(str.lower)
        elif ch=='U': self._vis_case(str.upper)
        elif ch=='r':
            k2=self.stdscr.getch()
            if 32<=k2<=126:
                repl=chr(k2); self.buf.save_undo()
                if r1==r2:
                    ln=self.buf.get_line(r1)
                    self.buf.set_line(r1,ln[:c1]+repl*(c2-c1+1)+ln[c2+1:])
                else:
                    ln=self.buf.get_line(r1); self.buf.set_line(r1,ln[:c1]+repl*len(ln[c1:]))
                    for r in range(r1+1,r2):
                        self.buf.set_line(r,repl*len(self.buf.get_line(r)))
                    ln=self.buf.get_line(r2); self.buf.set_line(r2,repl*(c2+1)+ln[c2+1:])
                self._enter_normal()
        elif ch=='J':
            self.buf.save_undo()
            for _ in range(r2-r1):
                ln=self.buf.get_line(r1); nxt=self.buf.get_line(r1+1).lstrip()
                self.buf.set_line(r1,(ln.rstrip()+' '+nxt) if nxt else ln.rstrip())
                self.buf.delete_line(r1+1)
            self.cursor.row=r1; self._enter_normal()

    def _visual_line(self, key):
        ch=chr(key) if 32<=key<=126 else None

        # Count prefix
        if ch and ch.isdigit() and (ch!='0' or self.pending_count):
            self.pending_count+=ch; return
        count=int(self.pending_count) if self.pending_count else 1
        self.pending_count=''

        if key==27 or ch=='V': self._enter_normal(); return
        if ch=='v': self.mode=Mode.VISUAL; return
        if ch=='o':  # swap anchor
            self.visual_start, (self.cursor.row, self.cursor.col) = \
                (self.cursor.row, self.cursor.col), self.visual_start
            return
        if ch==':':
            self.mode=Mode.COMMAND; self.cmd_line=":'<,'>"; return
        if ch=='n': self._search_next(self.search_dir); return
        if ch=='N': self._search_next(-self.search_dir); return
        if key==5:   # Ctrl-E — eval selection (line-wise)
            r1,r2=self._vis_lrange()
            self._eval_region(r1, r2); self._enter_normal(); return

        # All motions (line-mode cares about row movement mainly)
        moved=self._vmove(key, count)

        r1,r2=self._vis_lrange()
        if moved: return  # motion only, no operator
        if ch in ('d','x'): self._op_lines('d',r1,r2); self._enter_normal()
        elif ch=='y':
            self.register_text='\n'.join(self.buf.lines[r1:r2+1])
            self.register_linewise=True
            self.cursor.row=r1; self._first_nonblank(); self._enter_normal()
            self.status_msg=f'Yanked {r2-r1+1} lines'
        elif ch=='c': self._op_lines('c',r1,r2)
        elif ch=='s': self._op_lines('c',r1,r2)
        elif ch=='p' or ch=='P':
            self._op_lines('d',r1,r2)
            self._paste(False); self._enter_normal()
        elif ch=='>': self._indent_lines(r1,r2,1); self._enter_normal()
        elif ch=='<': self._indent_lines(r1,r2,-1); self._enter_normal()
        elif ch=='~':
            self.buf.save_undo()
            for r in range(r1,r2+1): self.buf.set_line(r,self.buf.get_line(r).swapcase())
            self._enter_normal()
        elif ch=='u':
            self.buf.save_undo()
            for r in range(r1,r2+1): self.buf.set_line(r,self.buf.get_line(r).lower())
            self._enter_normal()
        elif ch=='U':
            self.buf.save_undo()
            for r in range(r1,r2+1): self.buf.set_line(r,self.buf.get_line(r).upper())
            self._enter_normal()
        elif ch=='J':
            self.buf.save_undo()
            for _ in range(r2-r1):
                ln=self.buf.get_line(r1); nxt=self.buf.get_line(r1+1).lstrip()
                self.buf.set_line(r1,(ln.rstrip()+' '+nxt) if nxt else ln.rstrip())
                self.buf.delete_line(r1+1)
            self.cursor.row=r1; self._enter_normal()

    def _vis_range(self):
        sr,sc=self.visual_start; er,ec=self.cursor.row,self.cursor.col
        return (er,ec,sr,sc) if (sr,sc)>(er,ec) else (sr,sc,er,ec)

    def _vis_lrange(self):
        sr=self.visual_start[0]; er=self.cursor.row; return min(sr,er),max(sr,er)

    # ── Command mode ──────────────────────────────────────────────────────────

    def _command_key(self, key):
        if key==27: self.mode=Mode.NORMAL; self.cmd_line=''
        elif key in (10,13):
            cmd=self.cmd_line[1:]; self.mode=Mode.NORMAL; self.cmd_line=''; self._exec_cmd(cmd)
        elif key in (curses.KEY_BACKSPACE,127,8):
            if len(self.cmd_line)>1: self.cmd_line=self.cmd_line[:-1]
            else: self.mode=Mode.NORMAL; self.cmd_line=''
        elif 32<=key<=126: self.cmd_line+=chr(key)

    # ── Python eval integration ───────────────────────────────────────────────

    def _eval_ns(self):
        """Return the persistent eval namespace, seeded with live editor refs."""
        if not hasattr(self, '_py_ns'):
            self._py_ns = {}
        self._py_ns.update({'ed': self, 'buf': self.buf, 'pane': self._pane})
        return self._py_ns

    @staticmethod
    def _buf_is_python(buf):
        """True if the buffer looks like Python — .py extension or valid ast.parse."""
        import ast
        if buf.filename and buf.filename.endswith(('.py', '.pyw')):
            return True
        if buf.filename:
            return False  # named non-Python file — don't even try
        # Unnamed buffer: try parsing first non-blank lines as a heuristic
        sample = '\n'.join(l for l in buf.lines[:20] if l.strip())
        try:
            ast.parse(sample); return True
        except SyntaxError:
            return False

    def _resolve_deps(self, r1, r2):
        """Return statements needed before running lines r1..r2.

        Parses the selection to find free variable names, then walks the
        file's top-level AST to find the imports and definitions that supply
        them. Returns a list of source strings to exec in order, skipping
        anything already present in the eval namespace.
        """
        import ast
        if not self._buf_is_python(self.buf):
            return []
        ns = self._eval_ns()
        builtins = set(dir(__builtins__)) if isinstance(__builtins__, dict) else set(dir(__builtins__))

        # 1. Find names the selection uses but doesn't define locally.
        sel_src = '\n'.join(self.buf.get_line(r) for r in range(r1, r2+1))
        try:
            sel_tree = ast.parse(sel_src)
        except SyntaxError:
            return []

        used = set()
        for node in ast.walk(sel_tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                # dotted access: collect root name (random.gauss → random)
                n = node
                while isinstance(n, ast.Attribute): n = n.value
                if isinstance(n, ast.Name): used.add(n.id)

        local_defs = set()
        for node in ast.walk(sel_tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local_defs.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name): local_defs.add(t.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    local_defs.add(a.asname or a.name.split('.')[0])

        free = used - local_defs - builtins - set(ns)
        if not free:
            return []

        # 2. Walk the file's top-level nodes to find what supplies each free name.
        full_src = '\n'.join(self.buf.lines)
        try:
            full_tree = ast.parse(full_src)
        except SyntaxError:
            return []

        deps = []
        seen_names = set()
        for node in ast.iter_child_nodes(full_tree):
            node_r1 = node.lineno - 1  # convert to 0-indexed
            if node_r1 >= r1:          # don't look at or past the selection
                continue

            provided = set()
            if isinstance(node, ast.Import):
                for a in node.names:
                    provided.add(a.asname or a.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    provided.add(a.asname or (a.name if a.name != '*' else ''))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                provided.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name): provided.add(t.id)

            needed = provided & free - seen_names
            if needed:
                end = getattr(node, 'end_lineno', node.lineno) - 1
                src = '\n'.join(self.buf.lines[node_r1:end+1])
                deps.append(src)
                seen_names |= needed

        return deps

    def _eval_region(self, r1, r2):
        """Eval lines r1..r2 (inclusive), insert captured output after r2."""
        import io, contextlib, traceback
        ns = self._eval_ns()
        for dep in self._resolve_deps(r1, r2):
            try:
                exec(compile(dep, '<dep>', 'exec'), ns)
            except Exception:
                pass
        code = '\n'.join(self.buf.get_line(r) for r in range(r1, r2+1))
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                exec(compile(code, '<pyvim>', 'exec'), self._eval_ns())
            result = out.getvalue()
            self.status_msg = 'eval ok'
        except Exception:
            result = traceback.format_exc()
            self.status_err = True
            self.status_msg = result.splitlines()[-1]
        if result.strip():
            self.buf.save_undo()
            lines = result.rstrip('\n').splitlines()
            for i, line in enumerate(lines):
                self.buf.insert_line(r2 + 1 + i, '# >> ' + line)
            self.cursor.row = r2 + 1

    def _exec_file_toplevel(self):
        """Run every top-level statement in the current buffer into _py_ns.
        Used to pre-populate the REPL so all imports and defs are available.
        """
        import ast
        if not self._buf_is_python(self.buf):
            return
        ns = self._eval_ns()
        full_src = '\n'.join(self.buf.lines)
        try:
            full_tree = ast.parse(full_src)
        except SyntaxError:
            return
        for node in ast.iter_child_nodes(full_tree):
            r1 = node.lineno - 1
            end = getattr(node, 'end_lineno', node.lineno) - 1
            src = '\n'.join(self.buf.lines[r1:end+1])
            try:
                exec(compile(src, '<toplevel>', 'exec'), ns)
            except Exception:
                pass

    def _py_repl(self):
        """Drop into an interactive Python REPL, then return to pyvim."""
        import code as _code
        self._exec_file_toplevel()
        curses.endwin()
        print(f'\n  pyvim REPL  —  locals: ed, buf, pane  —  Ctrl-D to return\n')
        _code.interact(local=self._eval_ns(), banner='')
        self.stdscr.refresh()
        self.status_msg = 'returned from REPL'

    def _py_exec(self, expr):
        """Eval a single expression from command line, show result in status."""
        import io, contextlib, traceback
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                result = eval(compile(expr, '<cmd>', 'eval'), self._eval_ns())
            self.status_msg = repr(result) if result is not None else (out.getvalue().rstrip() or 'ok')
        except SyntaxError:
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    exec(compile(expr, '<cmd>', 'exec'), self._eval_ns())
                self.status_msg = out.getvalue().rstrip() or 'ok'
            except Exception:
                self.status_msg = traceback.format_exc().splitlines()[-1]; self.status_err = True
        except Exception:
            self.status_msg = traceback.format_exc().splitlines()[-1]; self.status_err = True

    def _exec_cmd(self, cmd):
        cmd=cmd.strip()
        if not cmd: return
        if cmd in ('py','python'):
            self._py_repl(); return
        elif cmd.startswith('py ') or cmd.startswith('python '):
            self._py_exec(cmd.split(None,1)[1]); return
        if cmd in ('qa','qall'):
            dirty=[p for p in self._panes if p.buf.modified]
            if dirty:
                names=', '.join(p.buf.filename or '[No Name]' for p in dirty)
                self.status_msg=f'No write since last change: {names} (add ! to override)'; self.status_err=True
            else:
                self.running=False
        elif cmd in ('qa!','qall!'):
            self.running=False
        elif cmd in ('q','quit'):
            if self.buf.modified:
                self.status_msg='No write since last change (add ! to override)'; self.status_err=True
            else:
                self._close_pane()
        elif cmd in ('q!','quit!'):
            self._close_pane(force=True)
        elif re.match(r'^wq?!?$',cmd) or cmd in ('x','wq','x!'):
            parts=cmd.split(None,1); fname=parts[1] if len(parts)>1 else None
            try: self.buf.save(fname); self._close_pane(force=True)
            except Exception as e: self.status_msg=str(e); self.status_err=True
        elif cmd.startswith('w') and (len(cmd)==1 or cmd[1] in ' !'):
            parts=cmd.split(None,1); fname=parts[1] if len(parts)>1 else None
            try:
                self.buf.save(fname)
                self.status_msg=f'"{self.buf.filename}" {self.buf.line_count()}L written'
            except Exception as e: self.status_msg=str(e); self.status_err=True
        elif cmd.startswith('e ') or cmd.startswith('edit '):
            parts=cmd.split(None,1)
            if len(parts)>1: self._load_file(parts[1].strip())
        elif cmd.startswith('r '):
            fname=cmd[2:].strip()
            try:
                with open(fname,errors='replace') as f: lines=f.read().splitlines()
                self.buf.save_undo()
                for i,l in enumerate(lines): self.buf.insert_line(self.cursor.row+1+i,l)
                self.status_msg=f'Read {len(lines)} lines'
            except Exception as e: self.status_msg=str(e); self.status_err=True
        elif re.match(r'^\d+$',cmd):
            self.cursor.row=max(0,min(int(cmd)-1,self.buf.line_count()-1)); self._first_nonblank()
        elif cmd=='$':
            self.cursor.row=self.buf.line_count()-1; self._first_nonblank()
        elif re.match(r'^(%|\.|\d+(,(\d+|\.|\$))?)?s',cmd):
            self._exec_sub(cmd)
        elif cmd.startswith('set ') or cmd=='set':
            self._exec_set(cmd[4:].strip() if cmd.startswith('set ') else '')
        elif cmd in ('noh','nohlsearch'): self.search_pat=''
        elif cmd in ('close','clo'): self._close_pane()
        elif cmd in ('close!','clo!'): self._close_pane(force=True)
        elif cmd=='only':
            while len(self._panes)>1: self._close_pane(force=True)  # keep closing non-active
        elif re.match(r'^[Ee]x(plore)?(\s|$)',cmd) or re.match(r'^[Ll]ex(plore)?(\s|$)',cmd):
            parts=cmd.split(None,1); path=parts[1].strip() if len(parts)>1 else None
            self._open_explorer(path)
        elif re.match(r'^[vV]?sp(lit)?(\s|$)',cmd,re.IGNORECASE):
            m=re.match(r'^([vV]?)sp(?:lit)?\s*(.*)',cmd,re.IGNORECASE)
            if m:
                vertical=bool(m.group(1)); fname=m.group(2).strip() or None
                self._split(vertical=vertical,filename=fname)
        elif cmd.startswith('!'):
            import subprocess
            try:
                r=subprocess.run(cmd[1:],shell=True,capture_output=True,text=True,timeout=30)
                out=(r.stdout+r.stderr).strip()
                self.status_msg=out[:self.width-1] if out else 'Done'
            except Exception as e: self.status_msg=str(e); self.status_err=True
        else:
            self.status_msg=f'E492: Not a command: {cmd}'; self.status_err=True

    def _exec_sub(self, cmd):
        try:
            start_r=end_r=self.cursor.row
            m=re.match(r'^%',cmd)
            if m: cmd=cmd[1:]; start_r,end_r=0,self.buf.line_count()-1
            else:
                m=re.match(r'^(\d+),(\d+)(.*)',cmd)
                if m: start_r=int(m.group(1))-1; end_r=int(m.group(2))-1; cmd=m.group(3)
            m=re.match(r'^s(.)(.*)$',cmd)
            if not m: return
            delim=m.group(1); parts=m.group(2).split(delim,2)
            if len(parts)<2: return
            pat,repl=parts[0],parts[1]; fs=parts[2] if len(parts)>2 else ''
            gf='g' in fs; inf=re.IGNORECASE if 'i' in fs else 0
            n=0; self.buf.save_undo()
            for r in range(start_r,end_r+1):
                line=self.buf.get_line(r)
                nl,cnt=(re.subn(pat,repl,line,flags=inf) if gf
                        else re.subn(pat,repl,line,count=1,flags=inf))
                if cnt: self.buf.set_line(r,nl); n+=cnt
            self.status_msg=(f'{n} substitution{"s" if n!=1 else ""}' if n else 'Pattern not found')
        except re.error as e: self.status_msg=f'Regex error: {e}'; self.status_err=True

    def _exec_set(self, opt):
        if opt in ('nu','number'):         self.show_numbers=True
        elif opt in ('nonu','nonumber'):   self.show_numbers=False
        else: self.status_msg=f'Unknown option: {opt}'

    # ── Search mode ───────────────────────────────────────────────────────────

    def _search_key(self, key):
        if key==27: self.mode=Mode.NORMAL; self.cmd_line=''; self.search_pat=''
        elif key in (10,13):
            pat=self.cmd_line[1:]; self.mode=Mode.NORMAL; self.cmd_line=''
            if pat: self.search_pat=pat; self._search_next(self.search_dir)
        elif key in (curses.KEY_BACKSPACE,127,8):
            if len(self.cmd_line)>1: self.cmd_line=self.cmd_line[:-1]
            else: self.mode=Mode.NORMAL; self.cmd_line=''; self.search_pat=''
        elif 32<=key<=126: self.cmd_line+=chr(key)

    # ── Explorer ──────────────────────────────────────────────────────────────

    def _open_explorer(self, path=None):
        if path:
            target=os.path.abspath(os.path.expanduser(path))
            if os.path.isfile(target): self._load_file(target); return
            if not os.path.isdir(target):
                self.status_msg=f'Not a directory: {target}'; self.status_err=True; return
            self.ex_dir=target
        else:
            self.ex_dir=(os.path.dirname(os.path.abspath(self.buf.filename))
                         if self.buf.filename else os.getcwd())
        self._prev_buf=self.buf; self._prev_cursor=(self.cursor.row,self.cursor.col)
        self._ex_reload(); self.mode=Mode.EXPLORER

    def _ex_reload(self):
        try: entries=os.listdir(self.ex_dir)
        except PermissionError: self.status_msg='Permission denied'; entries=[]
        dirs =sorted([e for e in entries if     os.path.isdir(os.path.join(self.ex_dir,e))],key=str.lower)
        files=sorted([e for e in entries if not os.path.isdir(os.path.join(self.ex_dir,e))],key=str.lower)
        self.ex_entries=[('..',True)]+[(d,True) for d in dirs]+[(f,False) for f in files]
        self.ex_sel=0; self.ex_top=0

    def _draw_explorer(self):
        self.stdscr.erase(); w=self.width
        self._as(0,0,f' {self.ex_dir}'[:w-1].ljust(w-1),curses.color_pair(1)|curses.A_BOLD)
        self._as(1,0,('─'*(w-1)),curses.A_DIM)
        body_h=self.height-4
        if self.ex_sel<self.ex_top: self.ex_top=self.ex_sel
        elif self.ex_sel>=self.ex_top+body_h: self.ex_top=self.ex_sel-body_h+1
        for i in range(body_h):
            idx=self.ex_top+i; sy=2+i
            if idx>=len(self.ex_entries): break
            name,is_dir=self.ex_entries[idx]
            if is_dir:
                display=('  '+name+'/')[:w-1].ljust(w-1)
                attr=(curses.color_pair(8)|curses.A_BOLD if idx==self.ex_sel
                      else curses.color_pair(7)|curses.A_BOLD)
            else:
                try: sz=os.path.getsize(os.path.join(self.ex_dir,name))
                except OSError: sz=0
                display=('  '+f'{sz:>9}  '+name)[:w-1].ljust(w-1)
                attr=(curses.color_pair(8)|curses.A_BOLD if idx==self.ex_sel else 0)
            self._as(sy,0,display,attr)
        footer=' j/k:move  Enter:open  -:up  q:back  R:refresh'
        self._as(self.height-2,0,footer[:w-1].ljust(w-1),curses.color_pair(1))
        if self.status_msg:
            self._as(self.height-1,0,self.status_msg[:w-1],curses.color_pair(4) if self.status_err else 0)
        try: self.stdscr.move(self.height-1,0)
        except curses.error: pass
        self.stdscr.refresh()

    def _explorer_key(self, key):
        ch=chr(key) if 32<=key<=126 else None; n=len(self.ex_entries)
        if   key==curses.KEY_UP   or ch=='k': self.ex_sel=max(0,self.ex_sel-1)
        elif key==curses.KEY_DOWN or ch=='j': self.ex_sel=min(n-1,self.ex_sel+1)
        elif key==6 or key==curses.KEY_NPAGE: self.ex_sel=min(n-1,self.ex_sel+max(1,self.height-6))
        elif key==2 or key==curses.KEY_PPAGE: self.ex_sel=max(0,self.ex_sel-max(1,self.height-6))
        elif key==curses.KEY_HOME: self.ex_sel=0
        elif key==curses.KEY_END:  self.ex_sel=n-1
        elif key in (10,13) or key==curses.KEY_RIGHT: self._ex_open()
        elif ch=='-' or key==curses.KEY_LEFT or key in (curses.KEY_BACKSPACE,127,8): self._ex_up()
        elif ch=='q' or key==27: self._ex_close()
        elif ch==':': self.mode=Mode.COMMAND; self.cmd_line=':'
        elif ch=='R': self._ex_reload(); self.status_msg='Refreshed'
        elif ch=='/': self.mode=Mode.SEARCH; self.search_dir=1; self.cmd_line='/'

    def _ex_open(self):
        if not self.ex_entries: return
        name,is_dir=self.ex_entries[self.ex_sel]
        target=os.path.normpath(os.path.join(self.ex_dir,name))
        if is_dir: self.ex_dir=target; self._ex_reload()
        else: self._load_file(target)

    def _ex_up(self):
        parent=os.path.dirname(self.ex_dir)
        if parent!=self.ex_dir:
            old=os.path.basename(self.ex_dir); self.ex_dir=parent; self._ex_reload()
            for i,(name,_) in enumerate(self.ex_entries):
                if name==old: self.ex_sel=i; break

    def _ex_close(self):
        self.mode=Mode.NORMAL
        if self._prev_buf is not None:
            self.buf=self._prev_buf; self.cursor.row,self.cursor.col=self._prev_cursor
            self._prev_buf=None; self._prev_cursor=None

    def _load_file(self, path):
        self.buf=Buffer.from_file(path); self.cursor=Cursor()
        self.top_row=0; self.left_col=0
        self._prev_buf=None; self._prev_cursor=None
        self.mode=Mode.NORMAL; self.status_msg=f'"{path}"'


def main():
    filename=sys.argv[1] if len(sys.argv)>1 else None
    curses.wrapper(lambda s: Editor(s, filename).run())

if __name__=='__main__':
    main()
