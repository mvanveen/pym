#!/usr/bin/env python3
"""pyvim - A pure Python vim-like editor"""
import ast
import csv
import curses
import difflib
import os
import pathlib
import re
import sys
import copy
import select
import termios
import time
import tomllib
import tty
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
    HISTORY = auto()
    PICKER = auto()

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
        self._disk_mtime = 0.0
        self._nb_meta = None  # notebook JSON metadata, set for .ipynb files

    def save_undo(self):
        self._undo.append((copy.deepcopy(self.lines), time.time()))
        self._redo.clear()
        if len(self._undo) > 500: self._undo.pop(0)

    def undo(self):
        if not self._undo: return False
        self._redo.append((copy.deepcopy(self.lines), time.time()))
        self.lines, _ = self._undo.pop(); self.modified = True; self._gen += 1; return True

    def redo(self):
        if not self._redo: return False
        self._undo.append((copy.deepcopy(self.lines), time.time()))
        self.lines, _ = self._redo.pop(); self.modified = True; self._gen += 1; return True

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
            if filename and filename.endswith('.ipynb'):
                import json
                with open(filename, 'r', errors='replace') as f:
                    nb = json.load(f)
                b._nb_meta = nb
                lines = []
                for cell in nb.get('cells', []):
                    ct = cell.get('cell_type', 'code')
                    lines.append(f'# %% {ct}')
                    src = cell.get('source', [])
                    if isinstance(src, list): src = ''.join(src)
                    lines.extend(src.splitlines())
                    for output in cell.get('outputs', []):
                        for ol in _nb_output_lines(output):
                            lines.append('# >> ' + ol)
                b.lines = lines if lines else ['# %% code', '']
            else:
                with open(filename, 'r', errors='replace') as f: content = f.read()
                lines = content.splitlines(); b.lines = lines if lines else ['']
            b._disk_mtime = os.path.getmtime(filename)
        except FileNotFoundError: b.lines = ['# %% code', ''] if (filename and filename.endswith('.ipynb')) else ['']
        b.modified = False; return b

    @classmethod
    def from_string(cls, content, filename=None):
        b = cls(); b.filename = filename
        lines = content.splitlines(); b.lines = lines if lines else ['']
        b.modified = False; return b

    def save(self, filename=None):
        fname = filename or self.filename
        if not fname: raise ValueError('No file name')
        if fname.endswith('.ipynb'):
            _nb_save(self, fname)
        else:
            with open(fname, 'w') as f: f.write('\n'.join(self.lines) + '\n')
        self.filename = fname; self.modified = False
        self._disk_mtime = os.path.getmtime(fname)

class Pane:
    def __init__(self, buf=None):
        self.buf    = buf or Buffer()
        self.cursor = Cursor()
        self.top_row = 0; self.left_col = 0
        self.wrap   = False
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

# ── Color schemes ─────────────────────────────────────────────────────────────
# Roles map to color pairs 9-15 in order.
_SYNTAX_ROLES = ('keyword', 'string', 'comment', 'number', 'type', 'builtin', 'decorator')
_SYNTAX_PAIR  = {r: 9 + i for i, r in enumerate(_SYNTAX_ROLES)}

# Each scheme: role → (fg_index, curses_attr).  fg values > 7 need HAS_256COLOR.
_SCHEMES: dict = {
    'default': {
        'keyword':   (curses.COLOR_YELLOW,   curses.A_BOLD),
        'string':    (curses.COLOR_GREEN,    0),
        'comment':   (curses.COLOR_WHITE,    curses.A_DIM),
        'number':    (curses.COLOR_MAGENTA,  0),
        'type':      (curses.COLOR_CYAN,     0),
        'builtin':   (curses.COLOR_BLUE,     0),
        'decorator': (curses.COLOR_YELLOW,   curses.A_BOLD),
    },
    'monokai': {
        'keyword':   (197, curses.A_BOLD),   # #F92672 pink
        'string':    (148, 0),               # #A6E22E green
        'comment':   (243, curses.A_DIM),    # #75715E gray
        'number':    (141, 0),               # #AE81FF purple
        'type':      (81,  0),               # #66D9EF cyan
        'builtin':   (81,  0),
        'decorator': (148, curses.A_BOLD),
    },
    'nord': {
        'keyword':   (110, curses.A_BOLD),   # #81A1C1 blue
        'string':    (143, 0),               # #A3BE8C sage
        'comment':   (241, curses.A_DIM),    # #4C566A dark gray
        'number':    (139, 0),               # #B48EAD purple
        'type':      (109, 0),               # #88C0D0 light blue
        'builtin':   (153, 0),               # #8FBCBB teal
        'decorator': (173, curses.A_BOLD),   # #D08770 orange
    },
    'gruvbox': {
        'keyword':   (167, curses.A_BOLD),   # #FB4934 red
        'string':    (142, 0),               # #B8BB26 yellow-green
        'comment':   (245, curses.A_DIM),    # #928374 gray
        'number':    (175, 0),               # #D3869B pink
        'type':      (108, 0),               # #8EC07C green
        'builtin':   (109, 0),               # #83A598 cyan
        'decorator': (214, curses.A_BOLD),   # #FABD2F yellow
    },
    'solarized': {
        'keyword':   (64,  curses.A_BOLD),   # #859900 olive
        'string':    (37,  0),               # #2AA198 teal
        'comment':   (66,  curses.A_DIM),    # #657B83 gray
        'number':    (162, 0),               # #D33682 magenta
        'type':      (32,  0),               # #268BD2 blue
        'builtin':   (61,  0),               # #6C71C4 violet
        'decorator': (166, curses.A_BOLD),   # #CB4B16 orange
    },
    'dracula': {
        'keyword':   (141, curses.A_BOLD),   # #BD93F9 purple
        'string':    (84,  0),               # #50FA7B green
        'comment':   (61,  curses.A_DIM),    # #6272A4 muted blue
        'number':    (212, 0),               # #FF79C6 pink
        'type':      (117, 0),               # #8BE9FD cyan
        'builtin':   (117, 0),
        'decorator': (215, curses.A_BOLD),   # #FFB86C orange
    },
}

# Active attrs read by _pg_attr; updated by _apply_scheme after curses starts.
_ACTIVE_SYNTAX_ATTRS: dict = {
    'keyword':   (9,  curses.A_BOLD),
    'string':    (10, 0),
    'comment':   (11, curses.A_DIM),
    'number':    (12, 0),
    'type':      (13, 0),
    'builtin':   (14, 0),
    'decorator': (15, curses.A_BOLD),
}
# Diff-bg variants — same fg, but bg=dark-green / dark-red so syntax colors
# stay visible inside +/- diff lines. Populated only on 256-color terminals;
# otherwise these fall back to the normal attrs (no bg).
_ACTIVE_SYNTAX_ATTRS_ADD: dict = dict(_ACTIVE_SYNTAX_ATTRS)
_ACTIVE_SYNTAX_ATTRS_REM: dict = dict(_ACTIVE_SYNTAX_ATTRS)

def _load_pyvim_config() -> dict:
    """Read [tool.pyvim] from the nearest pyproject.toml."""
    for d in [pathlib.Path.cwd(), *pathlib.Path.cwd().parents]:
        p = d / 'pyproject.toml'
        if p.exists():
            try:
                return tomllib.loads(p.read_text()).get('tool', {}).get('pyvim', {})
            except Exception:
                pass
    return {}

def _apply_scheme(name: str, overrides: dict | None = None) -> None:
    """Init curses pairs 9-15 for the named scheme and update _ACTIVE_SYNTAX_ATTRS.

    Falls back to 'default' when the scheme needs 256 colors and the terminal
    doesn't support them.  Call only after curses.start_color().
    """
    global _ACTIVE_SYNTAX_ATTRS, _ACTIVE_SYNTAX_ATTRS_ADD, _ACTIVE_SYNTAX_ATTRS_REM
    scheme = dict(_SCHEMES.get(name) or _SCHEMES['default'])
    needs_256 = any(v[0] > 7 for v in scheme.values())
    if needs_256 and not HAS_256COLOR:
        scheme = dict(_SCHEMES['default'])
    if overrides:
        for role, fg in overrides.items():
            if role in scheme and isinstance(fg, int):
                scheme[role] = (fg, scheme[role][1])
    new_attrs = {}
    add_attrs = {}
    rem_attrs = {}
    for role in _SYNTAX_ROLES:
        fg, attr = scheme.get(role, _SCHEMES['default'][role])
        idx = _SYNTAX_PAIR[role]
        curses.init_pair(idx, fg, -1)
        new_attrs[role] = (idx, attr)
        if HAS_256COLOR:
            add_idx = 23 + (idx - 9)   # 23..29
            rem_idx = 30 + (idx - 9)   # 30..36
            try:
                curses.init_pair(add_idx, fg, 22)   # role fg on dark green
                curses.init_pair(rem_idx, fg, 52)   # role fg on dark red
                add_attrs[role] = (add_idx, attr)
                rem_attrs[role] = (rem_idx, attr)
            except Exception: pass
    _ACTIVE_SYNTAX_ATTRS = new_attrs
    _ACTIVE_SYNTAX_ATTRS_ADD = add_attrs or new_attrs
    _ACTIVE_SYNTAX_ATTRS_REM = rem_attrs or new_attrs
    _pg_cache.clear()
    _diff_cache.clear()

# pygments ttype → (color_pair_index, extra_attr) from active scheme
def _pg_attr(ttype, attrs=None):
    a = attrs if attrs is not None else _ACTIVE_SYNTAX_ATTRS
    if ttype in Token.Keyword:                                   return a['keyword']
    if ttype in Token.Literal.String or ttype in Token.String:  return a['string']
    if ttype in Token.Comment:                                   return a['comment']
    if ttype in Token.Literal.Number or ttype in Token.Number:  return a['number']
    if ttype in Token.Name.Class or ttype in Token.Name.Exception \
            or ttype in Token.Name.Namespace:                    return a['type']
    if ttype in Token.Name.Function:                             return a['type']
    if ttype in Token.Name.Builtin:                              return a['builtin']
    if ttype in Token.Name.Decorator:                            return a['decorator']
    if ttype in Token.Operator.Word:                             return a['keyword']
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

def _is_notebook(filename):
    return bool(filename and filename.endswith('.ipynb'))

def _is_csv(filename):
    return bool(filename and filename.rsplit('.', 1)[-1].lower() in ('csv', 'tsv'))

# ── Unified diff highlighter ──────────────────────────────────────────────────
# Parses unified diff format (git/svn/standard) and colorizes by line role.
# Reuses color pairs 20/21/22 set up in Editor._setup_colors (full-line bg on
# 256-color terms, fg-only fallback otherwise).

_DIFF_HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@')
_DIFF_META_PREFIXES = (
    'index ', 'new file mode', 'deleted file mode', 'old mode', 'new mode',
    'similarity index', 'dissimilarity index', 'rename from', 'rename to',
    'copy from', 'copy to', 'Binary files', 'Only in ', 'GIT binary patch',
)

def _is_diff(buf):
    if buf.filename:
        ext = buf.filename.rsplit('.', 1)[-1].lower()
        if ext in ('diff', 'patch'): return True
    for line in buf.lines[:40]:
        if line.startswith('diff --git ') or line.startswith('Index: '):
            return True
        if _DIFF_HUNK_RE.match(line):
            return True
    return False

def _diff_file_lexer(path):
    """Resolve a diff header path (a/foo, b/foo, /dev/null) to a pygments lexer."""
    if not path or path == '/dev/null': return None
    if path.startswith('a/') or path.startswith('b/'): path = path[2:]
    try:
        return get_lexer_for_filename(path, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None

def _pg_line_spans(lexer, text, start_col, attrs_dict=None):
    """Tokenize one line, emit (start, end, (pair, attr)) spans offset by start_col."""
    if not lexer or not text: return []
    out = []
    col = start_col
    try:
        for ttype, value in lex(text, lexer):
            # Take only the first segment of any embedded newlines (single-line scope)
            part = value.split('\n', 1)[0]
            if part:
                pg = _pg_attr(ttype, attrs_dict)
                if pg:
                    out.append((col, col + len(part), pg))
                col += len(part)
            if '\n' in value: break
    except Exception: pass
    return out

# GitHub-style intra-line ("word") diff: within a hunk, pair each removed line
# with the corresponding added line and emphasize only the substrings that
# actually differ, so a 1-character change stands out from the line tint.
_WORD_RE = re.compile(r'\w+|\s+|[^\w\s]')

def _word_diff_ranges(old, new):
    """Char ranges that differ between two strings, at word granularity.

    Returns (old_ranges, new_ranges), each a list of (start, end) half-open
    spans into `old` / `new` respectively. Returns (None, None) when the two
    lines are too dissimilar to be a meaningful edit (treat as a full rewrite —
    the whole-line tint already conveys that).
    """
    o_toks = _WORD_RE.findall(old)
    n_toks = _WORD_RE.findall(new)
    o_off = []; c = 0
    for t in o_toks: o_off.append(c); c += len(t)
    n_off = []; c = 0
    for t in n_toks: n_off.append(c); c += len(t)
    sm = difflib.SequenceMatcher(None, o_toks, n_toks, autojunk=False)
    o_ranges = []; n_ranges = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal': continue
        if i2 > i1:
            o_ranges.append((o_off[i1], o_off[i2-1] + len(o_toks[i2-1])))
        if j2 > j1:
            n_ranges.append((n_off[j1], n_off[j2-1] + len(n_toks[j2-1])))
    changed = sum(e - s for s, e in o_ranges) + sum(e - s for s, e in n_ranges)
    if changed > 0.7 * ((len(old) + len(new)) or 1):
        return None, None
    return o_ranges, n_ranges

def _compute_word_hi(lines):
    """Map row index → list of (start, end) char ranges to emphasize.

    Pairs maximal runs of removed lines with the following run of added lines
    inside each hunk (the i-th '-' with the i-th '+'), as git/GitHub do.
    """
    hi: dict = {}
    in_hunk = False
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        c0 = line[:1]
        if not in_hunk:
            if line and _DIFF_HUNK_RE.match(line): in_hunk = True
            i += 1; continue
        if c0 == '-':
            j = i
            while j < n and lines[j][:1] == '-': j += 1
            k = j
            while k < n and lines[k][:1] == '+': k += 1
            if k > j:  # a '-' run followed by a '+' run → pair them up
                for idx in range(min(j - i, k - j)):
                    old = lines[i + idx][1:]; new = lines[j + idx][1:]
                    o_r, n_r = _word_diff_ranges(old, new)
                    if o_r: hi[i + idx] = [(s + 1, e + 1) for s, e in o_r]
                    if n_r: hi[j + idx] = [(s + 1, e + 1) for s, e in n_r]
            i = k; continue
        if c0 in '+ \\':
            i += 1; continue
        in_hunk = False  # '@@'/'diff'/blank ends the hunk — reprocess this line
    return hi

_diff_cache: dict = {}

def _diff_highlight(buf):
    key = (id(buf), buf._gen)
    if key in _diff_cache: return _diff_cache[key]
    stale = [k for k in _diff_cache if k[0] == id(buf)]
    for k in stale: del _diff_cache[k]
    if len(_diff_cache) > 40: _diff_cache.clear()

    ADD_BASE = (20, 0)
    REM_BASE = (21, 0)
    ADD_HI   = (37, curses.A_BOLD)   # changed substring on a '+' line
    REM_HI   = (38, curses.A_BOLD)   # changed substring on a '-' line
    HUNK     = (22, curses.A_BOLD)
    PATH     = (5,  curses.A_BOLD)
    META     = (0,  curses.A_DIM)
    use_bg   = HAS_256COLOR  # full-line bg compositing requires extra pairs

    word_hi = _compute_word_hi(buf.lines) if use_bg else {}

    by_row: list[list] = []
    in_hunk = False
    old_path = None; new_path = None
    cur_lexer = None

    for ri, line in enumerate(buf.lines):
        spans = []
        if not line:
            by_row.append(spans); in_hunk = False; continue
        c0 = line[0]
        if in_hunk:
            if c0 in '+-':
                is_add = (c0 == '+')
                base   = ADD_BASE if is_add else REM_BASE
                attrs  = (_ACTIVE_SYNTAX_ATTRS_ADD if is_add
                          else _ACTIVE_SYNTAX_ATTRS_REM)
                if use_bg:
                    # Paint the full line with the diff bg; pygments spans
                    # overlay per-cell via last-wins in the render loop.
                    spans.append((0, len(line), base))
                else:
                    # No bg compositing — just bold the prefix char.
                    spans.append((0, 1, (base[0], curses.A_BOLD)))
                spans.extend(_pg_line_spans(cur_lexer, line[1:], 1, attrs))
                # Brighten only the changed substrings (last-wins over syntax).
                for hs, he in word_hi.get(ri, ()):
                    spans.append((hs, he, ADD_HI if is_add else REM_HI))
            elif c0 == '\\':
                spans.append((0, len(line), META))
            elif c0 == ' ':
                spans.extend(_pg_line_spans(cur_lexer, line[1:], 1))
            else:
                in_hunk = False  # exited hunk — re-classify as header
        if not in_hunk and not spans:
            if _DIFF_HUNK_RE.match(line):
                spans.append((0, len(line), HUNK)); in_hunk = True
            elif line.startswith('diff --git ') or line.startswith('Index: '):
                spans.append((0, len(line), PATH))
                old_path = new_path = None; cur_lexer = None
            elif line.startswith('--- ') or line.startswith('+++ '):
                rest = line[4:].split('\t', 1)[0].rstrip()
                if line.startswith('--- '): old_path = rest
                else:                       new_path = rest
                cur_lexer = None
                for p in (new_path, old_path):
                    if p and p != '/dev/null':
                        cur_lexer = _diff_file_lexer(p)
                        if cur_lexer: break
                spans.append((0, len(line), PATH))
            elif any(line.startswith(p) for p in _DIFF_META_PREFIXES):
                spans.append((0, len(line), META))
        by_row.append(spans)

    _diff_cache[key] = by_row
    return by_row

# ── Notebook (.ipynb) helpers ─────────────────────────────────────────────────

_ANSI_ESC = re.compile(r'\x1b(?:\[[0-9;]*[A-Za-z]|\([A-Z]|[^[])')

def _nb_output_lines(output):
    """Extract plain-text lines from a notebook output cell dict.

    Handles stream, execute_result, display_data (skips binary image data),
    and error (strips ANSI escape codes from tracebacks).
    """
    ot = output.get('output_type', '')
    if ot == 'stream':
        text = output.get('text', [])
        if isinstance(text, list): text = ''.join(text)
        return text.rstrip('\n').splitlines()
    if ot in ('execute_result', 'display_data'):
        data = output.get('data', {})
        text = data.get('text/plain', '')
        if isinstance(text, list): text = ''.join(text)
        if text.strip():
            return text.rstrip('\n').splitlines()
        # Binary-only outputs (image/png, image/svg+xml, etc.) — show placeholder
        if any(k.startswith('image/') or k == 'application/pdf' for k in data):
            mime = next((k for k in data if k.startswith('image/') or k == 'application/pdf'), '?')
            return [f'[{mime} output]']
        return []
    if ot == 'error':
        tb = output.get('traceback', [])
        if tb:
            lines = []
            for tl in tb:
                cleaned = _ANSI_ESC.sub('', tl)
                lines.extend(cleaned.splitlines())
            return lines
        return [(output.get('ename', '') + ': ' + output.get('evalue', '')).strip()]
    return []

def _nb_cells(buf):
    """Return list of (sep_row, src_start, src_end, cell_type) for each cell.
    src_end < src_start means the cell has no source lines."""
    sep_rows = [r for r, l in enumerate(buf.lines) if l.startswith('# %% ')]
    n = len(buf.lines)
    result = []
    for i, sep in enumerate(sep_rows):
        ct = buf.lines[sep][5:].strip() or 'code'
        src_start = sep + 1
        next_sep = sep_rows[i+1] if i+1 < len(sep_rows) else n
        src_end = sep  # sentinel: empty cell
        for r in range(src_start, next_sep):
            if not buf.lines[r].startswith('# >> '):
                src_end = r
        result.append((sep, src_start, src_end, ct))
    return result

def _nb_cell_idx_at(cells_list, row):
    """Return index into cells_list for the cell that contains row, or None."""
    for i, (sep, _, _, _) in enumerate(cells_list):
        next_sep = cells_list[i+1][0] if i+1 < len(cells_list) else float('inf')
        if sep <= row < next_sep:
            return i
    return None

def _nb_save(buf, filename):
    """Reconstruct .ipynb JSON from the flat buffer and write to disk."""
    import json
    cells_info = _nb_cells(buf)
    n = len(buf.lines)
    cells = []
    for i, (sep, src_start, src_end, ct) in enumerate(cells_info):
        next_sep = cells_info[i+1][0] if i+1 < len(cells_info) else n
        src_lines = [buf.lines[r] for r in range(src_start, next_sep)
                     if not buf.lines[r].startswith('# >> ')]
        if src_lines:
            src_nb = [l + '\n' for l in src_lines[:-1]] + [src_lines[-1]]
        else:
            src_nb = []
        cell_dict: dict = {'cell_type': ct, 'source': src_nb, 'metadata': {}}
        if ct == 'code':
            cell_dict['outputs'] = []
            cell_dict['execution_count'] = None
        cells.append(cell_dict)
    meta = dict(getattr(buf, '_nb_meta', {}) or {})
    meta['cells'] = cells
    meta.setdefault('nbformat', 4)
    meta.setdefault('nbformat_minor', 5)
    meta.setdefault('metadata', {})
    with open(filename, 'w') as f:
        json.dump(meta, f, indent=1)
        f.write('\n')

class _FakeBuf:
    """Minimal buffer-like used to run per-cell highlighting on a slice of lines."""
    __slots__ = ('lines', 'filename', '_gen')
    def __init__(self, lines, filename, gen):
        self.lines = lines; self.filename = filename; self._gen = gen
    def line_count(self): return len(self.lines)
    def get_line(self, r): return self.lines[r] if 0 <= r < len(self.lines) else ''

_nb_hl_cache: dict = {}

def _nb_highlight(buf):
    """Compute per-row highlight info for a notebook buffer.

    Returns list[(display_override | None, spans)] indexed by buf row.
    display_override is None for code cell lines (raw text + python spans);
    for markdown cell lines it's the rendered text (may differ from raw).
    Separator and output lines keep (None, []) — caller handles them separately.
    """
    key = (id(buf), buf._gen)
    if key in _nb_hl_cache: return _nb_hl_cache[key]
    stale = [k for k in _nb_hl_cache if k[0] == id(buf)]
    for k in stale: del _nb_hl_cache[k]
    if len(_nb_hl_cache) > 20: _nb_hl_cache.clear()

    cells_list = _nb_cells(buf)
    n = len(buf.lines)
    result: list = [(None, []) for _ in range(n)]

    for ci, (sep, src_start, src_end, ct) in enumerate(cells_list):
        if src_start > src_end: continue  # empty cell
        src_lines = buf.lines[src_start:src_end + 1]
        if ct == 'code':
            fake = _FakeBuf(src_lines, 'cell.py', (id(buf), ci, buf._gen))
            try:
                hl = _pg_highlight(fake)
                for r, spans in enumerate(hl):
                    if r < len(src_lines):
                        result[src_start + r] = (None, spans)
            except Exception:
                pass
        elif ct == 'markdown':
            fake = _FakeBuf(src_lines, 'cell.md', (id(buf), ci, buf._gen))
            try:
                md_tbl = _md_highlight(fake)
                for r in range(len(src_lines)):
                    display, spans = md_tbl[r] if r < len(md_tbl) else (src_lines[r], [])
                    result[src_start + r] = (display, spans)
            except Exception:
                pass

    _nb_hl_cache[key] = result
    return result

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
_md_img_cache: dict = {}   # same key as _md_cache → {row: (abs_path, alt)}
_sixel_cache: dict = {}      # (path, mtime, max_w, max_h) → (sixel_str, w_cells, h_cells)
_colortext_cache: dict = {}  # same key → (lines, w_cells, h_cells)
_TABLE_SEP_RE = re.compile(r'^\s*\|[-:| ]+\|\s*$')
_TASK_RE      = re.compile(r'^(\s*)([-*+]|\d+\.)\s+\[([ xX])\] ?')

def _task_state(line):
    """Return 'checked', 'unchecked', or None."""
    m = _TASK_RE.match(line)
    if not m: return None
    return 'checked' if m.group(3).lower() == 'x' else 'unchecked'


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


def _extract_img_src(children, buf_filename) -> tuple[str, str] | None:
    """Return (abs_path, alt) for the first image token, or None.
    Records the path even if the file doesn't exist yet — encoder will fail gracefully."""
    for tok in (children or []):
        if tok.type == 'image':
            src = (tok.attrs or {}).get('src', '') if tok.attrs else ''
            alt = tok.content or ''
            if not src or src.startswith(('http://', 'https://', 'data:')):
                continue
            if buf_filename:
                base = os.path.dirname(os.path.abspath(buf_filename))
                path = os.path.normpath(os.path.join(base, src))
            else:
                path = os.path.abspath(src)
            return (path, alt)   # let encoder handle missing-file case
    return None


def _encode_sixel(path: str, max_w: int, max_h: int):
    """Encode image as DCS sixel string.  Returns (sixel_str, w_cells, h_cells) or None.
    max_w/max_h are in terminal cells; assumes 8×16 px per cell."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    key = (path, mtime, max_w, max_h)
    if key in _sixel_cache:
        return _sixel_cache[key]
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        CW, CH = 8, 16
        img = Image.open(path).convert('RGB')
        img.thumbnail((max_w * CW, max_h * CH), Image.LANCZOS)
        w, h = img.size
        w_cells = max(1, (w + CW - 1) // CW)
        h_cells = max(1, (h + CH - 1) // CH)

        qimg = img.quantize(colors=256)
        pal  = qimg.getpalette()
        data = list(qimg.getdata())  # type: ignore[attr-defined]

        out = ['\033Pq']
        for ci in range(min(256, len(pal) // 3)):
            r = pal[ci*3] * 100 // 255
            g = pal[ci*3+1] * 100 // 255
            b = pal[ci*3+2] * 100 // 255
            out.append(f'#{ci};2;{r};{g};{b}')

        for y0 in range(0, h, 6):
            bh = min(6, h - y0)
            col_bits: dict = {}
            for x in range(w):
                for dy in range(bh):
                    ci = data[(y0 + dy) * w + x]
                    if ci not in col_bits:
                        col_bits[ci] = bytearray(w)
                    col_bits[ci][x] |= 1 << dy
            first = True
            for ci, bits in col_bits.items():
                if not first: out.append('$')
                first = False
                out.append(f'#{ci}')
                prev = None; run = 0
                for v in bits:
                    c = chr(63 + v)
                    if c == prev:
                        run += 1
                    else:
                        if prev: out.append(f'!{run}{prev}' if run > 3 else prev * run)
                        prev, run = c, 1
                if prev: out.append(f'!{run}{prev}' if run > 3 else prev * run)
            out.append('-')

        out.append('\033\\')
        result = (''.join(out), w_cells, h_cells)
        _sixel_cache[key] = result
        return result
    except Exception:
        return None


def _rgb_to_256(r: int, g: int, b: int) -> int:
    """Nearest xterm-256 index using the 6x6x6 color cube (indices 16-231)."""
    return 16 + 36 * round(r * 5 / 255) + 6 * round(g * 5 / 255) + round(b * 5 / 255)


def _rgb_to_8(r: int, g: int, b: int) -> int:
    """Nearest ANSI 8-color index (0-7) for terminals without 256-color support."""
    bright = (r * 299 + g * 587 + b * 114) // 1000
    if bright < 48:  return 0  # black
    if bright > 200: return 7  # white
    hi = max(r, g, b)
    rd = r > hi * 0.5
    gr = g > hi * 0.5
    bl = b > hi * 0.5
    if rd and gr and bl: return 7   # white-ish
    if rd and gr:        return 3   # yellow
    if rd and bl:        return 5   # magenta
    if gr and bl:        return 6   # cyan
    if rd:               return 1
    if gr:               return 2
    if bl:               return 4
    return 0


def _encode_color_text(path: str, max_w: int, max_h: int):
    """Encode image as ANSI-colored ▀ half-block lines (2px per cell row).
    Returns (lines, w_cells, h_cells) or None.  Each line is a self-contained
    ANSI string; caller positions with ESC[y;xH before printing each one."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    key = (path, mtime, max_w, max_h, HAS_256COLOR)
    if key in _colortext_cache:
        return _colortext_cache[key]
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(path).convert('RGB')
        img.thumbnail((max_w, max_h * 2), Image.LANCZOS)
        w, h = img.size
        if h % 2:
            pad = Image.new('RGB', (w, h + 1), (0, 0, 0))
            pad.paste(img, (0, 0))
            img = pad; h += 1
        pix = img.load()
        lines = []
        for row in range(0, h, 2):
            parts = []
            for col in range(w):
                tr, tg, tb = pix[col, row]
                br2, bg2, bb2 = pix[col, row + 1]
                if HAS_256COLOR:
                    tc = _rgb_to_256(tr, tg, tb)
                    bc = _rgb_to_256(br2, bg2, bb2)
                    parts.append(f'\033[38;5;{tc};48;5;{bc}m▀')
                else:
                    tc = _rgb_to_8(tr, tg, tb)
                    bc = _rgb_to_8(br2, bg2, bb2)
                    parts.append(f'\033[3{tc};4{bc}m▀')
            parts.append('\033[0m')
            lines.append(''.join(parts))
        result = (lines, w, h // 2)
        _colortext_cache[key] = result
        return result
    except Exception:
        return None


def _md_images(buf) -> dict:
    """Return {row: (abs_path, alt)} for the last-parsed generation of buf."""
    key = (id(buf), buf._gen)
    return _md_img_cache.get(key, {})


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
            src = (tok.attrs or {}).get('src', '') if tok.attrs else ''
            alt = tok.content or (tok.attrs or {}).get('alt', '') if tok.attrs else tok.content or ''
            label = alt or (os.path.basename(src) if src else 'image')
            st  = cur()
            for ch in f'[{label}]': chars.append(ch); styles.append(st)
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


# ── CSV / TSV column-aligned rendering ───────────────────────────────────────
# Renders .csv/.tsv as an aligned grid (same box-drawing style as md tables).
# Row 0 is treated as a header (bold); columns cycle through syntax colors so
# fields stay visually distinct. The cursor row is shown raw by the caller so
# the buffer column maps 1:1 to the underlying text for editing.

_csv_cache: dict = {}
_CSV_COL_PAIRS = [(14, 0), (13, 0), (10, 0), (12, 0), (9, 0), (11, 0)]
_CSV_MAX_W = 32   # cap per-column display width; longer cells get truncated with …

def _csv_delim(filename):
    return '\t' if (filename or '').rsplit('.', 1)[-1].lower() == 'tsv' else ','

def _csv_parse_line(line, delim):
    """Parse one buffer line into fields, honouring quoted delimiters."""
    try:
        return next(csv.reader([line], delimiter=delim))
    except Exception:
        return line.split(delim)

def _csv_delim_cols(line, delim):
    """Char indices of top-level (unquoted) delimiters in `line`."""
    out = []; inq = False; i = 0; n = len(line)
    while i < n:
        c = line[i]
        if c == '"':
            if inq and i + 1 < n and line[i + 1] == '"':
                i += 2; continue       # escaped "" inside a quoted field
            inq = not inq
        elif c == delim and not inq:
            out.append(i)
        i += 1
    return out

def _csv_cells(line, delim):
    """Return [(start, end)] char ranges for each field (raw, untrimmed)."""
    ds = _csv_delim_cols(line, delim)
    bounds = [-1] + ds + [len(line)]
    return [(a + 1, b) for a, b in zip(bounds, bounds[1:])]

def _csv_cell_at(line, col, delim):
    """Return (cell_idx, start, end) for the field containing `col`, or None."""
    for i, (a, b) in enumerate(_csv_cells(line, delim)):
        if a <= col <= b:
            return (i, a, b)
    return None

def _csv_highlight(buf):
    key = (id(buf), buf._gen)
    if key in _csv_cache: return _csv_cache[key]
    stale = [k for k in _csv_cache if k[0] == id(buf)]
    for k in stale: del _csv_cache[k]
    if len(_csv_cache) > 20: _csv_cache.clear()

    DIM   = (11, curses.A_DIM)
    delim = _csv_delim(buf.filename)
    rows  = [_csv_parse_line(ln, delim) for ln in buf.lines]
    ncols = max((len(r) for r in rows), default=0)
    widths = [1] * ncols
    for r in rows:
        for ci, c in enumerate(r[:ncols]):
            widths[ci] = max(widths[ci], min(len(c), _CSV_MAX_W))

    by_row = []
    for ri, cells in enumerate(rows):
        # Leave genuinely blank lines untouched (no grid box around nothing).
        if buf.lines[ri] == '':
            by_row.append((buf.lines[ri], [])); continue
        parts = ['│']; spans = [(0, 1, DIM)]; pos = 1
        for ci in range(ncols):
            cell = cells[ci] if ci < len(cells) else ''
            if len(cell) > widths[ci]:
                cell = cell[:max(0, widths[ci] - 1)] + '…'
            padded = ' ' + cell.ljust(widths[ci]) + ' '
            pair, extra = _CSV_COL_PAIRS[ci % len(_CSV_COL_PAIRS)]
            if ri == 0: extra |= curses.A_BOLD
            spans.append((pos, pos + len(padded), (pair, extra)))
            parts.append(padded); pos += len(padded)
            parts.append('│'); spans.append((pos, pos + 1, DIM)); pos += 1
        by_row.append((''.join(parts), spans))

    _csv_cache[key] = by_row
    return by_row


def _md_highlight(buf):
    key = (id(buf), buf._gen)
    if key in _md_cache: return _md_cache[key]
    stale = [k for k in _md_cache if k[0] == id(buf)]
    for k in stale:
        del _md_cache[k]
        _md_img_cache.pop(k, None)
    if len(_md_cache) > 20: _md_cache.clear(); _md_img_cache.clear()

    lines  = buf.lines
    by_row = [(ln, []) for ln in lines]   # default: raw, no styling
    images: dict = {}                      # row → (abs_path, alt)

    if _MD is None:
        _md_cache[key] = by_row; _md_img_cache[key] = images; return by_row

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
            is_fence = tok.type == 'fence'
            lang = tok.info.strip() if (is_fence and tok.info) else ''
            LB = (13, curses.A_BOLD)   # language badge: cyan bold
            for r in range(r0, min(r1, len(by_row))):
                if is_fence and r == r0:
                    label = f' {lang}' if lang else ''
                    vis = '╭─' + label
                    spans = [(0, 2, DM)]
                    if lang: spans.append((2, len(vis), LB))
                    by_row[r] = (vis, spans)
                elif is_fence and r == r1 - 1:
                    by_row[r] = ('╰─', [(0, 2, DM)])
                else:
                    vis = '│ ' + lines[r]
                    by_row[r] = (vis, [(0, 2, DM), (2, len(vis), FC)])
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
                # Record image metadata before rendering the placeholder
                img_info = _extract_img_src(group, buf.filename)
                if img_info:
                    images[r] = img_info
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
                    task_m = re.match(r'^\[( |x|X)\] ?', vis) if marker in '-*+' else None
                    if task_m:
                        done   = task_m.group(1).lower() == 'x'
                        box    = '■ ' if done else '□ '
                        rest   = vis[len(task_m.group(0)):]
                        delta  = len(box) - len(task_m.group(0))
                        # Shift existing spans, clamp negatives
                        new_sp = []
                        for s, e, st in spans:
                            ns, ne = max(0, s + delta), max(0, e + delta)
                            if ne > ns: new_sp.append((ns, ne, st))
                        # Checkbox glyph span
                        box_st = (10, 0) if done else (9, curses.A_BOLD)
                        new_sp = [(0, len(box), box_st)] + new_sp
                        # Dim the text of completed items
                        if done and rest:
                            new_sp.append((len(box), len(box) + len(rest), (0, curses.A_DIM)))
                        vis = box + rest; spans = new_sp
                        pre = indent  # no bullet — checkbox is the marker
                    else:
                        pre = indent + ('• ' if marker in '-*+' else marker + ' ')
                    vis   = pre + vis
                    spans = [(s + len(pre), e + len(pre), st) for s, e, st in spans]
                elif gi > 0:
                    # Soft-wrapped continuation line: keep the raw line's
                    # leading whitespace so wrapped bullet text stays aligned
                    # under its marker.
                    ws = re.match(r'^\s*', raw).group(0)
                    if ws:
                        vis   = ws + vis
                        spans = [(s + len(ws), e + len(ws), st) for s, e, st in spans]
                by_row[r] = (vis, spans)
            i += 1; continue

        i += 1

    _md_cache[key] = by_row
    _md_img_cache[key] = images
    return by_row

_WRAP_MARKER_RE = re.compile(r'^(\s*)([-*+•□■▢]|\d+\.) ')

def _wrap_segments(display, width):
    """Split `display` into wrapped segments fitting `width` cells each.

    Returns a list of (seg_start, seg_end, hang_prefix). First segment has
    hang_prefix='' and covers display[0:width]. Continuation segments get a
    hanging-indent prefix of spaces matching the leading whitespace +
    bullet/quote marker width, so wrapped bullet text stays under the marker.
    """
    if width <= 0 or len(display) <= width:
        return [(0, len(display), '')]
    m = _WRAP_MARKER_RE.match(display)
    if m:
        hang_w = m.end()
    elif display.startswith('│ ') or display.startswith('> '):
        hang_w = 2
    else:
        ws = re.match(r'^\s*', display)
        hang_w = ws.end() if ws else 0
    if hang_w >= width - 1:
        hang_w = 0
    hang = ' ' * hang_w
    seg_w = width - hang_w
    segs = [(0, width, '')]
    pos = width
    while pos < len(display):
        end = min(pos + seg_w, len(display))
        segs.append((pos, end, hang))
        pos = end
    return segs

# ── JSON structure preview ───────────────────────────────────────────────────
# A position-aware JSON parser: every value node records its (start, end) char
# span in the source text so we can find the smallest node under the cursor and
# pretty-print just that subtree into the live side-preview pane.  stdlib `json`
# has no source positions, so we tokenize + recursive-descent ourselves.

import json as _json

_JSON_TOK_RE = re.compile(r'''
      [ \t\r\n]+
    | (?P<punct>[{}\[\]:,])
    | (?P<str>"(?:\\.|[^"\\\x00-\x1f])*")
    | (?P<num>-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)
    | (?P<lit>true|false|null)
''', re.VERBOSE)

class _JsonNode:
    __slots__ = ('start', 'end', 'kind', 'path', 'members')
    # kind: 'object' | 'array' | 'scalar'
    # members: object -> [(key_start, key_end, value_node)]; array -> [value_node]
    def __init__(self, start, kind, path):
        self.start = start; self.end = start
        self.kind = kind; self.path = path; self.members = None

def _json_tokenize(text):
    """Return [(kind, text, start, end)] or None if `text` isn't well-formed."""
    toks = []; i = 0; n = len(text)
    for m in _JSON_TOK_RE.finditer(text):
        if m.start() != i: return None        # gap = unexpected character
        i = m.end()
        if m.lastgroup is None: continue       # whitespace
        toks.append((m.lastgroup, m.group(), m.start(), m.end()))
    return toks if i == n else None

def _json_parse(text):
    """Parse `text` into a span-annotated _JsonNode tree, or None if invalid."""
    toks = _json_tokenize(text)
    if toks is None: return None
    pos = 0
    def parse_value(path):
        nonlocal pos
        if pos >= len(toks): raise ValueError
        kind, txt, s, e = toks[pos]
        if kind == 'punct' and txt == '{':
            node = _JsonNode(s, 'object', path); node.members = []; pos += 1
            if pos < len(toks) and toks[pos][1] == '}':
                node.end = toks[pos][3]; pos += 1; return node
            while True:
                if pos >= len(toks) or toks[pos][0] != 'str': raise ValueError
                ks, ke = toks[pos][2], toks[pos][3]; key = _json.loads(toks[pos][1]); pos += 1
                if pos >= len(toks) or toks[pos][1] != ':': raise ValueError
                pos += 1
                child = parse_value(path + [key])
                node.members.append((ks, ke, child))
                if pos < len(toks) and toks[pos][1] == ',': pos += 1; continue
                if pos < len(toks) and toks[pos][1] == '}':
                    node.end = toks[pos][3]; pos += 1; return node
                raise ValueError
        if kind == 'punct' and txt == '[':
            node = _JsonNode(s, 'array', path); node.members = []; pos += 1
            if pos < len(toks) and toks[pos][1] == ']':
                node.end = toks[pos][3]; pos += 1; return node
            idx = 0
            while True:
                node.members.append(parse_value(path + [idx])); idx += 1
                if pos < len(toks) and toks[pos][1] == ',': pos += 1; continue
                if pos < len(toks) and toks[pos][1] == ']':
                    node.end = toks[pos][3]; pos += 1; return node
                raise ValueError
        if kind in ('str', 'num', 'lit'):
            node = _JsonNode(s, 'scalar', path); node.end = e; pos += 1; return node
        raise ValueError
    try:
        root = parse_value([])
        return root if pos == len(toks) else None
    except (ValueError, IndexError):
        return None

def _json_node_at(node, off):
    """Smallest value node whose span contains `off`. Hovering a key → its value."""
    if node is None: return None
    if node.kind == 'object':
        for ks, ke, child in node.members:
            if ks <= off <= ke: return child                  # on the key
            if child.start <= off <= child.end: return _json_node_at(child, off)
    elif node.kind == 'array':
        for child in node.members:
            if child.start <= off <= child.end: return _json_node_at(child, off)
    return node

def _json_path_str(path):
    s = '$'
    for p in path:
        if isinstance(p, int): s += f'[{p}]'
        elif re.match(r'^[A-Za-z_]\w*$', p): s += '.' + p
        else: s += '[' + _json.dumps(p) + ']'
    return s

_json_cache: dict = {}
def _json_parse_cached(buf):
    key = (id(buf), buf._gen)
    if key not in _json_cache:
        for k in [k for k in _json_cache if k[0] == id(buf)]: del _json_cache[k]
        if len(_json_cache) > 8: _json_cache.clear()
        _json_cache[key] = _json_parse('\n'.join(buf.lines))
    return _json_cache[key]

# ── Editor ───────────────────────────────────────────────────────────────────

class Editor:
    def __init__(self, stdscr, filename=None, stdin_content=None, follow=False, follow_fd=None):
        self.stdscr = stdscr
        self.mode   = Mode.NORMAL
        self.height = 0
        self.width  = 0

        # Pane management
        if stdin_content is not None:
            initial_buf = Buffer.from_string(stdin_content, filename='[stdin]')
        elif filename:
            initial_buf = Buffer.from_file(filename)
        else:
            initial_buf = Buffer()
        p = Pane(initial_buf)
        self._layout  = _Leaf(p)
        self._panes   = [p]
        self._pane_i  = 0

        # Editor-global state
        self.register_text    = ''
        self.register_linewise= False
        self.register_cell    = None   # list[str] of yanked/cut cell lines
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
        self._pending_sixels: list = []   # [(scr_y, scr_x, path, max_w, max_h)]
        self._confirm_cb  = None
        self._confirm_msg = ''
        self.hist_sel = 0
        self.hist_top = 0
        self.hist_preview_scroll = 0
        self.hist_diff_mode = True
        self._hist_git_cache: list = []
        self._hist_preview_cache: dict = {}

        # Explorer state
        self.ex_dir     = os.getcwd()
        self.ex_entries = []
        self.ex_sel     = 0
        self.ex_top     = 0
        self._prev_buf    = None
        self._prev_cursor = None
        self.ex_search_query  = ''
        self.ex_searching     = False

        # JSON side-preview: (source_pane, preview_pane) or None
        self._json_preview = None

        # Jump stack + picker state
        self._jump_stack    = []   # list of (row, col) for Ctrl-O
        self.picker_entries = []   # list of (label, row, col)
        self.picker_sel     = 0
        self.picker_top     = 0
        self.picker_title   = ''
        self.picker_cb      = None  # if set, called with label on Enter instead of jumping
        self.picker_live    = False # interactive type-to-filter picker (Ctrl-P)
        self.picker_query   = ''    # current live-filter text
        self.picker_source  = []    # full label list for live filtering

        # Follow mode (tail -f)
        self._follow         = follow
        self._follow_fd      = follow_fd  # duped non-blocking stdin fd, or None
        self._follow_file    = None       # open file handle for file-follow
        self._follow_partial = ''         # incomplete last line not yet ended with \n
        if follow:
            stdscr.timeout(250)           # getch returns -1 after 250 ms
            if follow_fd is not None:
                self.buf.filename = '[stdin]'
            elif filename:
                try:
                    self._follow_file = open(filename, 'r', errors='replace')
                    self._follow_file.seek(0, 2)   # start at current EOF
                except OSError:
                    pass

        curses.start_color()
        curses.use_default_colors()
        global HAS_256COLOR
        HAS_256COLOR = curses.COLORS >= 256
        curses.init_pair(1,  curses.COLOR_BLACK,  curses.COLOR_CYAN)
        curses.init_pair(2,  curses.COLOR_BLACK,  curses.COLOR_YELLOW)
        curses.init_pair(3,  curses.COLOR_BLACK,  curses.COLOR_WHITE)
        curses.init_pair(4,  curses.COLOR_RED,    -1)
        curses.init_pair(5,  curses.COLOR_CYAN,   -1)
        curses.init_pair(6,  curses.COLOR_WHITE,  -1)
        curses.init_pair(7,  curses.COLOR_BLUE,   -1)
        curses.init_pair(8,  curses.COLOR_BLACK,  curses.COLOR_CYAN)
        # Syntax pairs — loaded from colorscheme config
        cfg = _load_pyvim_config()
        _apply_scheme(cfg.get('colorscheme', 'default'), cfg.get('colors'))
        # Diff viewer: green-bg for added, red-bg for removed
        _diff_256=False
        if HAS_256COLOR:
            try:
                curses.init_pair(20, 255, 22)   # white on dark green
                curses.init_pair(21, 255, 52)   # white on dark red
                curses.init_pair(22, 255, 17)   # white on dark blue (hunk header)
                curses.init_pair(37, 255, 34)   # white on vivid green (changed word)
                curses.init_pair(38, 255, 124)  # white on vivid red   (changed word)
                _diff_256=True
            except Exception: pass
        if not _diff_256:
            curses.init_pair(20, curses.COLOR_GREEN,   -1)
            curses.init_pair(21, curses.COLOR_RED,     -1)
            curses.init_pair(22, curses.COLOR_CYAN,    -1)

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
        if self.mode == Mode.HISTORY:
            self._draw_history(); return
        if self.mode == Mode.PICKER:
            self._draw_picker(); return
        self._pending_sixels = []
        if self._json_preview: self._update_json_preview()
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
        self._flush_images()

    def _flush_images(self):
        if not self._pending_sixels:
            return
        buf = []
        for scr_y, scr_x, path, max_w, max_h in self._pending_sixels:
            if HAS_SIXEL:
                result = _encode_sixel(path, max_w, max_h)
                if result:
                    data, _wc, _hc = result
                    buf.append(f'\033[{scr_y + 1};{scr_x + 1}H{data}')
            else:
                result = _encode_color_text(path, max_w, max_h)
                if result:
                    lines, _wc, _hc = result
                    for i, line in enumerate(lines):
                        buf.append(f'\033[{scr_y + 1 + i};{scr_x + 1}H{line}')
        if buf:
            os.write(sys.stdout.fileno(), ''.join(buf).encode('utf-8'))

    def _draw_pane(self, pane, is_active):
        lnw = self._pane_lnw(pane)
        tw  = pane.width - lnw
        lc  = 0 if pane.wrap else pane.left_col

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
        is_nb = _is_notebook(pane.buf.filename)
        is_csv = not (is_md or is_nb) and _is_csv(pane.buf.filename)
        is_diff = not (is_md or is_nb or is_csv) and _is_diff(pane.buf)
        in_insert = is_active and self.mode == Mode.INSERT
        md_table = (_md_highlight(pane.buf) if is_md else None)
        csv_table = (_csv_highlight(pane.buf) if is_csv else None)
        img_map  = (_md_images(pane.buf) if is_md else {})
        hl_table = (None if is_md or is_nb or is_csv else
                    _diff_highlight(pane.buf) if is_diff else
                    _pg_highlight(pane.buf) if _detect_lang(pane.buf.filename) else None)
        _nb_clist = _nb_cells(pane.buf) if is_nb else []
        _nb_cursor_ci = _nb_cell_idx_at(_nb_clist, pane.cursor.row) if is_nb else None
        nb_hl = _nb_highlight(pane.buf) if is_nb else None

        try:
            spat = re.compile(self.search_pat,
                re.IGNORECASE if self.search_pat == self.search_pat.lower() else 0
            ) if self.search_pat else None
        except re.error:
            spat = None

        sy = 0
        br = pane.top_row
        pending_segs = []      # remaining wrap segments for current br
        pending_display = ''
        pending_hl_row  = []

        while sy < pane.height:
            scr_y = pane.y + sy
            scr_x = pane.x

            if br >= pane.buf.line_count():
                self._as(scr_y, scr_x, '~', curses.A_DIM)
                sy += 1; br += 1
                continue

            line = pane.buf.get_line(br)

            # Notebook: cell separator — ╭── [type] ──────────────────────╮
            if is_nb and line.startswith('# %% '):
                ct = line[5:].strip() or 'code'
                label = f' {ct} '
                inner = max(0, pane.width - 2 - len(label) - 2)
                bar = ('╭─ ' + label + '─' * inner + '─╮')[:pane.width]
                my_ci = _nb_cell_idx_at(_nb_clist, br)
                if is_active and my_ci == _nb_cursor_ci:
                    attr = curses.color_pair(1) | curses.A_BOLD
                else:
                    attr = curses.color_pair(7) | curses.A_DIM
                self._as(scr_y, scr_x, bar, attr)
                sy += 1; br += 1
                continue

            # Notebook: output lines — comment color, │ prefix gutter (no wrap)
            if is_nb and line.startswith('# >> '):
                out_attr = curses.color_pair(_SYNTAX_PAIR['comment']) | curses.A_DIM
                if lnw > 0:
                    self._as(scr_y, scr_x, ' ' * (lnw - 1) + '│', out_attr)
                self._as(scr_y, scr_x + lnw, (' ' + line[5:])[lc:lc+tw], out_attr)
                sy += 1; br += 1
                continue

            # First visual row of this br: compute display + segments and draw gutter
            is_first = not pending_segs
            if is_first:
                if self.show_numbers:
                    ns = str(br+1).rjust(lnw-1) + ' '
                    na = (curses.color_pair(6)|curses.A_BOLD
                          if is_active and br == pane.cursor.row
                          else curses.color_pair(5))
                    self._as(scr_y, scr_x, ns, na)

                if is_nb and lnw > 0:
                    my_ci = _nb_cell_idx_at(_nb_clist, br)
                    if is_active and my_ci == _nb_cursor_ci:
                        ba = curses.color_pair(1) | curses.A_BOLD
                    else:
                        ba = curses.color_pair(7) | curses.A_DIM
                    self._as(scr_y, scr_x + lnw - 1, '│', ba)

                if br in img_map and not (is_active and br == pane.cursor.row):
                    path, _alt = img_map[br]
                    self._pending_sixels.append(
                        (scr_y, scr_x + lnw, path, pane.width - lnw, pane.height // 2))

                if is_nb and nb_hl is not None:
                    nb_rendered, hl_row = nb_hl[br] if br < len(nb_hl) else (None, [])
                    if nb_rendered is not None:
                        is_cur = is_active and br == pane.cursor.row
                        show_raw = (is_cur and (in_insert or not _is_table_row(line))) or br in vis_rows
                        display = line if show_raw else nb_rendered
                        if show_raw: hl_row = []
                    else:
                        display = line  # code cell: raw text, python spans in hl_row
                elif md_table:
                    is_cur = is_active and br == pane.cursor.row
                    show_raw_cur = is_cur and (in_insert or not _is_table_row(line))
                    raw = show_raw_cur or br in vis_rows
                    display, hl_row = (line, []) if raw else (
                        md_table[br] if br < len(md_table) else (line, []))
                elif csv_table:
                    # Grid stays rendered under the cursor in normal mode (the
                    # cursor is remapped to the rendered cell in _place_cursor);
                    # only insert mode / visual selection drop back to raw text.
                    is_cur = is_active and br == pane.cursor.row
                    show_raw_cur = is_cur and (in_insert or line.strip() == '')
                    raw = show_raw_cur or br in vis_rows
                    display, hl_row = (line, []) if raw else (
                        csv_table[br] if br < len(csv_table) else (line, []))
                else:
                    display = line
                    hl_row = (hl_table[br] if hl_table and br < len(hl_table) else [])

                pending_display = display
                pending_hl_row  = hl_row
                pending_segs    = (list(_wrap_segments(display, tw))
                                   if pane.wrap else [(0, len(display), '')])
            else:
                # Continuation row: blank line-number gutter, keep nb │ border
                if self.show_numbers:
                    self._as(scr_y, scr_x, ' ' * lnw, curses.color_pair(5))
                if is_nb and lnw > 0:
                    my_ci = _nb_cell_idx_at(_nb_clist, br)
                    ba = (curses.color_pair(1) | curses.A_BOLD
                          if is_active and my_ci == _nb_cursor_ci
                          else curses.color_pair(7) | curses.A_DIM)
                    self._as(scr_y, scr_x + lnw - 1, '│', ba)

            seg_start, seg_end, hang = pending_segs.pop(0)
            self._render_line(scr_y, scr_x + lnw, pending_display, lc, tw,
                              vis_rows, vis_range, br, spat, pending_hl_row, is_active,
                              seg_start=seg_start, seg_end=seg_end, hang=hang)

            sy += 1
            if not pending_segs:
                br += 1

    def _render_line(self, sy, sx, line, lc, tw, vis_rows, vis_range, br,
                     spat, hl_row, is_active, seg_start=0, seg_end=None, hang=''):
        if seg_end is None: seg_end = len(line)
        # Build span list against the full `line`: (start, end, attr)
        spans = []
        for ts, te, pg in hl_row:
            spans.append((ts, te, curses.color_pair(pg[0]) | pg[1]))
        if spat:
            for m in spat.finditer(line):
                spans.append((m.start(), m.end(), curses.color_pair(2)))
        if is_active and br in vis_rows:
            rng = vis_range.get(br)
            if rng is None:
                spans.append((0, len(line), curses.color_pair(3)))
            else:
                sc2, ec2 = rng
                spans.append((sc2, ec2+1, curses.color_pair(3)))

        # Visible-row layout: [hang_prefix][body slice of line[slice_start:slice_end]]
        # Non-wrap: seg_start=0, seg_end=len(line), hang='' → body = line[lc:lc+tw]
        # Wrap:     lc=0, seg covers a wrap chunk → body = line[seg_start:seg_end]
        hl = len(hang)
        body_budget = max(0, tw - hl)
        slice_start = seg_start + lc
        slice_end   = min(seg_end, slice_start + body_budget)
        body = line[slice_start:slice_end] if slice_end > slice_start else ''
        visible = hang + body

        # Map spans (line coords) → visible row cells [0, tw)
        attrs = [0] * tw
        for ts, te, attr in spans:
            a = max(0, ts - slice_start) + hl
            b = min(tw, te - slice_start + hl)
            for i in range(a, b):
                attrs[i] = attr

        scr_col = sx
        i = 0
        n = len(visible)
        while i < n:
            a = attrs[i]
            j = i + 1
            while j < n and attrs[j] == a:
                j += 1
            seg = visible[i:j]
            if a: self._as(sy, scr_col, seg, a)
            else: self._as(sy, scr_col, seg)
            scr_col += len(seg); i = j

    def _draw_statusbar(self):
        _tbl_nm = (self.mode == Mode.NORMAL and self._on_cell_row())
        _nb_nav = (self.mode == Mode.NORMAL and _is_notebook(self.buf.filename) and
                   self.buf.get_line(self.cursor.row).startswith('# %% '))
        if _nb_nav:
            _nb_cells_list = _nb_cells(self.buf)
            _nb_ci = (_nb_cell_idx_at(_nb_cells_list, self.cursor.row) or 0) + 1
            ml = f' CELL {_nb_ci}/{len(_nb_cells_list)} '
        else:
            ml = {
                Mode.NORMAL:' TABLE  ' if _tbl_nm else ' NORMAL ',
                Mode.INSERT:' INSERT ',
                Mode.VISUAL:' VISUAL ', Mode.VISUAL_LINE:' V-LINE ',
                Mode.COMMAND:' COMMAND', Mode.SEARCH:' SEARCH ',
            }.get(self.mode, ' NORMAL ')
        fname = self.buf.filename or '[No Name]'
        mod   = ' [+]' if self.buf.modified else ''
        pos   = f' {self.cursor.row+1}:{self.cursor.col+1} '

        if self._confirm_cb is not None:
            self._as(self.height-1, 0, (self._confirm_msg+' '*self.width)[:self.width-1], curses.color_pair(4))
        elif self.mode in (Mode.COMMAND, Mode.SEARCH):
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

    def _display_col(self, p):
        """Cursor column in rendered-display coords.

        For markdown/csv grids the on-screen line is box-drawing with cells
        padded to equal widths, so a raw buffer column (which can be huge for a
        long JSON cell) must be mapped to the much smaller grid column.  Both the
        cursor placement and the horizontal-scroll math must use this, or they
        disagree and the grid scrolls off-screen.
        """
        col = p.cursor.col
        # The grid renders in every mode except insert / visual (where the
        # cursor row drops to raw text), so remap in normal/command/search too —
        # otherwise pressing ':' would let left_col run away and blank the grid.
        if self.mode in (Mode.INSERT, Mode.VISUAL, Mode.VISUAL_LINE):
            return col
        raw = p.buf.get_line(p.cursor.row)
        vis_line = info = None
        if _is_markdown(p.buf.filename) and _is_table_row(raw):
            md = _md_highlight(p.buf)
            if p.cursor.row < len(md):
                vis_line = md[p.cursor.row][0]; info = _table_cell_at(raw, col)
        elif _is_csv(p.buf.filename) and raw.strip() != '':
            cv = _csv_highlight(p.buf)
            if p.cursor.row < len(cv):
                vis_line = cv[p.cursor.row][0]
                info = _csv_cell_at(raw, col, _csv_delim(p.buf.filename))
        if vis_line is not None and info is not None:
            pipes = [i for i, c in enumerate(vis_line) if c == '│']
            if info[0] + 1 < len(pipes):
                return pipes[info[0]] + 2  # land after '│ '
        return col

    def _place_cursor(self):
        if self.mode in (Mode.COMMAND, Mode.SEARCH):
            try: self.stdscr.move(self.height-1, min(len(self.cmd_line), self.width-1))
            except curses.error: pass
            return
        p = self._pane
        lnw = self._pane_lnw(p)
        sy  = p.y + self._cursor_visual_offset(p)
        col = self._display_col(p)
        # Notebook separator: cursor sits at left edge of the bar
        if _is_notebook(p.buf.filename) and p.buf.get_line(p.cursor.row).startswith('# %% '):
            try: self.stdscr.move(max(p.y, min(sy, p.y+p.height-1)), p.x)
            except curses.error: pass
            return
        if p.wrap:
            line = p.buf.get_line(p.cursor.row)
            segs = _wrap_segments(line, p.width - lnw)
            seg_idx = self._cursor_seg_idx(p)
            seg_start, _seg_end, hang = segs[seg_idx]
            sx = p.x + lnw + (col - seg_start) + len(hang)
        else:
            sx = p.x + lnw + col - p.left_col
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

    def _row_visual_count(self, p, br):
        if not p.wrap: return 1
        line = p.buf.get_line(br)
        tw = p.width - self._pane_lnw(p)
        return len(_wrap_segments(line, tw))

    def _cursor_seg_idx(self, p):
        """Index of the wrap segment containing the cursor on its own row."""
        if not p.wrap: return 0
        line = p.buf.get_line(p.cursor.row)
        tw = p.width - self._pane_lnw(p)
        segs = _wrap_segments(line, tw)
        for i, (_s, e, _h) in enumerate(segs):
            if p.cursor.col < e: return i
        return len(segs) - 1

    def _cursor_visual_offset(self, p):
        """Visual rows from top_row to the cursor's visual row (inclusive 0-based)."""
        if not p.wrap: return p.cursor.row - p.top_row
        off = 0
        for br in range(p.top_row, p.cursor.row):
            off += self._row_visual_count(p, br)
        return off + self._cursor_seg_idx(p)

    def _scroll(self):
        p = self._pane
        th = p.height
        lnw = self._pane_lnw(p)
        tw  = p.width - lnw
        if p.cursor.row < p.top_row:
            p.top_row = p.cursor.row
        elif not p.wrap:
            if p.cursor.row >= p.top_row + th:
                p.top_row = p.cursor.row - th + 1
        else:
            # Advance top_row until cursor's visual row fits in the pane.
            while p.top_row < p.cursor.row and self._cursor_visual_offset(p) >= th:
                p.top_row += 1
        if p.wrap:
            p.left_col = 0
        else:
            # Use display coords so a long raw cell (e.g. a big JSON value in a
            # csv column) doesn't scroll the aligned grid off-screen.
            ccol = self._display_col(p)
            if ccol < p.left_col: p.left_col = max(0, ccol-5)
            elif ccol >= p.left_col + tw: p.left_col = ccol - tw + 6

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

    # ── Cell geometry dispatch (markdown tables ‖ csv/tsv grids) ───────────────
    # The cell-navigation operators below are shared between markdown pipe tables
    # and csv/tsv files; these helpers pick the right delimiter geometry so the
    # same h/j/k/l/Tab bindings drive both — vim motions over a spreadsheet grid.

    def _cells_at_row(self, row):
        line = self.buf.get_line(row)
        if _is_csv(self.buf.filename):
            return [] if line.strip() == '' else _csv_cells(line, _csv_delim(self.buf.filename))
        return _table_cells(line)

    def _cell_at_in_row(self, row, col):
        line = self.buf.get_line(row)
        if _is_csv(self.buf.filename):
            if line.strip() == '': return None
            return _csv_cell_at(line, col, _csv_delim(self.buf.filename))
        return _table_cell_at(line, col)

    def _is_cell_sep_row(self, row):
        # csv grids have no separator row; markdown tables do.
        if _is_csv(self.buf.filename): return False
        return bool(_TABLE_SEP_RE.match(self.buf.get_line(row)))

    def _on_cell_row(self, row=None):
        r = self.cursor.row if row is None else row
        if _is_csv(self.buf.filename):
            return self.buf.get_line(r).strip() != ''
        return _is_table_row(self.buf.get_line(r))

    def _cell_next(self):
        cells = self._cells_at_row(self.cursor.row)
        if not cells: return
        info = self._cell_at_in_row(self.cursor.row, self.cursor.col)
        idx = info[0] if info else -1
        if idx + 1 < len(cells):
            self.cursor.col = cells[idx + 1][0]
        else:
            nr = self.cursor.row + 1
            if nr < self.buf.line_count():
                self.cursor.row = nr
                c2 = self._cells_at_row(nr)
                if c2: self.cursor.col = c2[0][0]

    def _cell_prev(self):
        cells = self._cells_at_row(self.cursor.row)
        if not cells: return
        info = self._cell_at_in_row(self.cursor.row, self.cursor.col)
        idx = info[0] if info else len(cells)
        if idx > 0:
            self.cursor.col = cells[idx - 1][0]
        else:
            nr = self.cursor.row - 1
            if nr >= 0:
                self.cursor.row = nr
                c2 = self._cells_at_row(nr)
                if c2: self.cursor.col = c2[-1][0]

    def _cell_down(self):
        info = self._cell_at_in_row(self.cursor.row, self.cursor.col)
        idx = info[0] if info else 0
        nr = self.cursor.row + 1
        while nr < self.buf.line_count():
            if self._is_cell_sep_row(nr): nr += 1; continue
            c2 = self._cells_at_row(nr)
            if c2:
                self.cursor.row = nr
                self.cursor.col = c2[min(idx, len(c2) - 1)][0]
                return
            break
        self._move(1, 0)

    def _cell_up(self):
        info = self._cell_at_in_row(self.cursor.row, self.cursor.col)
        idx = info[0] if info else 0
        nr = self.cursor.row - 1
        while nr >= 0:
            if self._is_cell_sep_row(nr): nr -= 1; continue
            c2 = self._cells_at_row(nr)
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

    # ── JSON side preview ──────────────────────────────────────────────────────
    def _toggle_json_preview(self):
        if self._json_preview:
            _src, prev = self._json_preview
            self._json_preview = None
            if prev in self._panes and len(self._panes) > 1:
                self._layout = _lc_close(self._layout, prev) or self._layout
                self._panes = [x for x in self._panes if x is not prev]
                self._pane_i = min(self._pane_i, len(self._panes) - 1)
            self.status_msg = 'json preview off'
            return
        fn = self.buf.filename or ''
        if not (fn.endswith('.json') or _is_csv(fn)):
            self.status_msg = 'json preview needs a .json or .csv/.tsv buffer'; self.status_err = True; return
        src = self._pane
        prev_buf = Buffer(['']); prev_buf.filename = '[json-preview].json'
        prev_pane = Pane(prev_buf)
        prev_pane.wrap = True        # wrap long values so row ends aren't clipped
        orig_i = self._pane_i
        self._layout = _lc_split(self._layout, src, prev_pane, True)  # vertical split
        self._panes.append(prev_pane)
        self._pane_i = orig_i        # keep focus in the source buffer
        self._json_preview = (src, prev_pane)
        self.status_msg = ('json preview on — move over a cell' if _is_csv(fn)
                           else 'json preview on — move the cursor over any value')

    def _abs_offset(self, buf, row, col):
        off = 0
        for i in range(min(row, len(buf.lines))):
            off += len(buf.lines[i]) + 1
        return off + col

    def _set_preview_lines(self, prev, lines):
        if prev.buf.lines != lines:
            prev.buf.lines = lines; prev.buf._gen += 1
            prev.cursor.row = prev.cursor.col = 0; prev.top_row = 0

    def _update_json_preview(self):
        src, prev = self._json_preview
        if src not in self._panes or prev not in self._panes:
            self._json_preview = None; return
        # While the preview pane itself is focused, leave it alone so the user
        # can scroll/search it with normal vim bindings without it resetting.
        if self._pane is prev:
            return
        buf = src.buf
        if _is_csv(buf.filename):
            self._update_json_preview_cell(src, prev); return
        if not (buf.filename and buf.filename.endswith('.json')):
            self._set_preview_lines(prev, ['// source is not a .json or .csv buffer']); return
        root = _json_parse_cached(buf)
        if root is None:
            self._set_preview_lines(prev, ['// invalid JSON']); return
        # Snap to the first meaningful char on the line so hovering anywhere on a
        # row (including its leading indent) resolves to that row's value, not root.
        ln = buf.get_line(src.cursor.row); col = min(src.cursor.col, len(ln))
        snap = next((i for i in range(col, len(ln)) if ln[i] not in ' \t'), None)
        if snap is None:
            snap = next((i for i in range(col - 1, -1, -1) if ln[i] not in ' \t'), col)
        off  = self._abs_offset(buf, src.cursor.row, snap)
        node = _json_node_at(root, off) or root
        raw  = '\n'.join(buf.lines)[node.start:node.end]
        try:    pretty = _json.dumps(_json.loads(raw), indent=2, ensure_ascii=False)
        except Exception: pretty = raw
        self._set_preview_lines(prev, pretty.split('\n'))
        if not self.status_msg:
            self.status_msg = 'json  ' + _json_path_str(node.path)

    def _update_json_preview_cell(self, src, prev):
        """Preview the JSON content of the CSV/TSV cell under the cursor."""
        buf   = src.buf
        delim = _csv_delim(buf.filename)
        raw   = buf.get_line(src.cursor.row)
        cells = _csv_parse_line(raw, delim)
        info  = _csv_cell_at(raw, src.cursor.col, delim)
        ci    = info[0] if info else 0
        hdr   = _csv_parse_line(buf.lines[0], delim) if buf.lines else []
        name  = hdr[ci] if ci < len(hdr) else f'col {ci}'
        text  = (cells[ci] if ci < len(cells) else '').strip()
        if not text:
            self._set_preview_lines(prev, ['// empty cell'])
            if not self.status_msg: self.status_msg = f'json  {name}: (empty)'
            return
        try:
            pretty = _json.dumps(_json.loads(text), indent=2, ensure_ascii=False)
            self._set_preview_lines(prev, pretty.split('\n'))
            if not self.status_msg: self.status_msg = f'json  {name}'
        except Exception:
            # Not JSON — still show the raw value so the pane is useful.
            self._set_preview_lines(prev, ['// cell is not JSON', text])
            if not self.status_msg: self.status_msg = f'json  {name}: not JSON'

    def _rotate_panes(self, delta=1):
        ordered = _lc_panes(self._layout)
        n = len(ordered)
        if n < 2: self.status_msg = 'Need 2+ panes to rotate (use ^W v or ^W s to split)'; return
        attrs = [(p.buf, p.cursor, p.top_row, p.left_col) for p in ordered]
        d = delta % n
        rotated = attrs[-d:] + attrs[:-d]
        cur_i = ordered.index(self._pane)
        for p, (buf, cur, tr, lc) in zip(ordered, rotated):
            p.buf = buf; p.cursor = cur; p.top_row = tr; p.left_col = lc
        new_active = ordered[(cur_i + delta) % n]
        self._pane_i = self._panes.index(new_active)
        self.status_msg = f'Rotated {n} panes'

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
            if key == -1:
                if self._follow: self._follow_check()
                continue
            self.status_msg=''; self.status_err=False
            self._dispatch(key)

    def _apply_follow_text(self, new_text):
        """Append new_text to the buffer using the partial-line state machine.

        _follow_partial tracks any incomplete last line (no trailing \\n yet).
        When non-empty it is always the last element of buf.lines.
        """
        if not new_text: return
        near_bottom = self.cursor.row >= self.buf.line_count() - 2
        # Clear the initial one-line empty-buffer placeholder on first write.
        if not self._follow_partial and self.buf.lines == ['']:
            self.buf.lines = []
        full = self._follow_partial + new_text
        parts = full.split('\n')
        new_partial = parts[-1]   # '' when new_text ended with \n
        complete    = parts[:-1]  # lines that ended with \n
        if self._follow_partial and self.buf.lines:
            self.buf.lines.pop()  # remove old partial; will be replaced below
        self.buf.lines.extend(complete)
        if new_partial:
            self.buf.lines.append(new_partial)
        if not self.buf.lines:
            self.buf.lines = ['']
        self._follow_partial = new_partial
        self.buf._gen += 1
        if near_bottom:
            self.cursor.row = self.buf.line_count() - 1; self._clamp()

    def _follow_check(self):
        new_text = ''
        if self._follow_file:
            chunk = self._follow_file.read()
            if chunk: new_text = chunk
        elif self._follow_fd is not None:
            r, _, _ = select.select([self._follow_fd], [], [], 0)
            if r:
                raw_chunks = []
                try:
                    while True:
                        chunk = os.read(self._follow_fd, 65536)
                        if not chunk:
                            os.close(self._follow_fd); self._follow_fd = None; break
                        raw_chunks.append(chunk)
                except BlockingIOError:
                    pass
                except OSError:
                    pass
                if raw_chunks:
                    new_text = b''.join(raw_chunks).decode('utf-8', errors='replace')
        self._apply_follow_text(new_text)

    def _dispatch(self, key):
        if self._confirm_cb is not None:
            if key in (ord('y'), ord('Y')):
                cb=self._confirm_cb; self._confirm_cb=None; self._confirm_msg=''; cb()
            else:
                self._confirm_cb=None; self._confirm_msg=''
                self.status_msg='Cancelled'; self.status_err=False
            return
        if   self.mode==Mode.NORMAL:      self._normal(key)
        elif self.mode==Mode.INSERT:      self._insert(key)
        elif self.mode==Mode.VISUAL:      self._visual(key)
        elif self.mode==Mode.VISUAL_LINE: self._visual_line(key)
        elif self.mode==Mode.COMMAND:     self._command_key(key)
        elif self.mode==Mode.SEARCH:      self._search_key(key)
        elif self.mode==Mode.EXPLORER:    self._explorer_key(key)
        elif self.mode==Mode.HISTORY:     self._history_key(key)
        elif self.mode==Mode.PICKER:      self._picker_key(key)

    # ── Normal mode ───────────────────────────────────────────────────────────

    def _normal(self, key):
        ch=chr(key) if 32<=key<=126 else None
        if ch and ch.isdigit() and (ch!='0' or self.pending_count):
            self.pending_count+=ch; return
        count=int(self.pending_count) if self.pending_count else 1
        self.pending_count=''

        _on_tbl = self._on_cell_row()
        _on_nb  = _is_notebook(self.buf.filename)
        if   key in (curses.KEY_LEFT,)  or ch=='h':
            if _on_tbl: [self._cell_prev() for _ in range(count)]
            else: [self._move(0,-1) for _ in range(count)]
        elif key in (curses.KEY_RIGHT,) or ch=='l':
            if _on_tbl: [self._cell_next() for _ in range(count)]
            else: [self._move(0,1) for _ in range(count)]
        elif key in (curses.KEY_UP,)    or ch=='k':
            if _on_tbl: [self._cell_up() for _ in range(count)]
            elif _on_nb: self._nb_move_up(count)
            else: [self._move(-1,0) for _ in range(count)]
        elif key in (curses.KEY_DOWN,)  or ch=='j':
            if _on_tbl: [self._cell_down() for _ in range(count)]
            elif _on_nb: self._nb_move_down(count)
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
        elif ch=='i': self._nb_enter_cell(); self._enter_insert()
        elif ch=='I': self._nb_enter_cell(); self._first_nonblank(); self._enter_insert()
        elif ch=='a':
            self._nb_enter_cell()
            line=self.buf.get_line(self.cursor.row)
            self._enter_insert(1 if line else 0)
        elif ch=='A':
            self._nb_enter_cell()
            line=self.buf.get_line(self.cursor.row)
            self.cursor.col=len(line); self._enter_insert()
        elif ch=='o':
            if _on_nb and self._nb_on_sep():
                self._nb_insert_cell(below=True)
            else:
                self.buf.save_undo(); ind=self._indent(self.cursor.row)
                self.buf.insert_line(self.cursor.row+1,ind)
                self.cursor.row+=1; self.cursor.col=len(ind); self.mode=Mode.INSERT
        elif ch=='O':
            if _on_nb and self._nb_on_sep():
                self._nb_insert_cell(below=False)
            else:
                self.buf.save_undo(); ind=self._indent(self.cursor.row)
                self.buf.insert_line(self.cursor.row,ind)
                self.cursor.col=len(ind); self.mode=Mode.INSERT
        elif ch=='x':
            if _on_nb and self._nb_on_sep():
                self._nb_cut_cell()
            else:
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
        elif ch=='p':
            if _on_nb and self._nb_on_sep() and self.register_cell: self._nb_paste_cell(below=True)
            else: self._paste(True)
        elif ch=='P':
            if _on_nb and self._nb_on_sep() and self.register_cell: self._nb_paste_cell(below=False)
            else: self._paste(False)
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
        elif ch==']':
            k2 = self.stdscr.getch()
            if k2 == ord('c') and _is_notebook(self.buf.filename): self._nb_cell_next()
        elif ch=='[':
            k2 = self.stdscr.getch()
            if k2 == ord('c') and _is_notebook(self.buf.filename): self._nb_cell_prev()
        elif ch==':': self.mode=Mode.COMMAND; self.cmd_line=':'
        elif ch=='z': self._normal_z()
        elif key==5:  # Ctrl-E: run cell (notebook) or eval current line (other files)
            if _is_notebook(self.buf.filename): self._nb_eval_cell()
            else: self._eval_region(self.cursor.row, self.cursor.row)
        elif key==15: self._jump_back()   # Ctrl-O
        elif key==16:                  # Ctrl-P: fuzzy file finder
            self._push_jump(); self._cmd_find('')
        elif key==23: self._ctrl_w()   # Ctrl-W
        elif key==26:                  # Ctrl-Z suspend
            import signal
            curses.endwin()
            os.kill(os.getpid(), signal.SIGTSTP)
            self.stdscr.refresh()
        elif key==27:
            self.search_pat=''; self.status_msg=''
            # Notebook: Esc from in-cell → pop back to cell separator
            if _on_nb and not self._nb_on_sep():
                cells = _nb_cells(self.buf)
                idx = _nb_cell_idx_at(cells, self.cursor.row)
                if idx is not None:
                    self.cursor.row = cells[idx][0]
                    self.cursor.col = 0; self.cursor.col_want = 0
        elif key==curses.KEY_HOME: self.cursor.col=0
        elif key==curses.KEY_END:
            self.cursor.col=max(0,len(self.buf.get_line(self.cursor.row))-1)
        elif key in (10,13):  # Enter
            line=self.buf.get_line(self.cursor.row)
            if _on_nb and line.startswith('# %% '):
                self._nb_enter_cell()  # land on first source line of this cell
            elif _is_markdown(self.buf.filename) and _task_state(line):
                self._toggle_task()
            else:
                self._move(1,0); self._first_nonblank()

    def _toggle_task(self):
        """Flip [ ] ↔ [x] on the current line."""
        row=self.cursor.row; line=self.buf.get_line(row)
        m=_TASK_RE.match(line)
        if not m: return
        i=m.start(3)
        new_char=' ' if line[i].lower()=='x' else 'x'
        self.buf.save_undo()
        self.buf.set_line(row, line[:i]+new_char+line[i+1:])

    # ── Jump stack ────────────────────────────────────────────────────────────

    def _push_jump(self):
        self._jump_stack.append((self.cursor.row, self.cursor.col))

    def _jump_back(self):
        if self._jump_stack:
            row, col = self._jump_stack.pop()
            self.cursor.row = row; self.cursor.col = col; self._clamp()
        else:
            self.status_msg = 'Already at oldest position'

    # ── AST navigation ────────────────────────────────────────────────────────

    def _ast_defs(self, name, src):
        try: tree = ast.parse(src)
        except SyntaxError: return []
        results = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == name:
                    results.append((node.lineno-1, node.col_offset))
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == name:
                        results.append((t.lineno-1, t.col_offset))
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                t = node.target
                if isinstance(t, ast.Name) and t.id == name:
                    results.append((t.lineno-1, t.col_offset))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    n = alias.asname or alias.name.split('.')[0]
                    if n == name:
                        results.append((node.lineno-1, node.col_offset))
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    n = alias.asname or alias.name
                    if n == name:
                        results.append((node.lineno-1, node.col_offset))
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                t = node.target
                if isinstance(t, ast.Name) and t.id == name:
                    results.append((t.lineno-1, t.col_offset))
            elif isinstance(node, ast.NamedExpr):
                if node.target.id == name:
                    results.append((node.target.lineno-1, node.target.col_offset))
            elif isinstance(node, ast.comprehension):
                t = node.target
                if isinstance(t, ast.Name) and t.id == name:
                    results.append((t.lineno-1, t.col_offset))
        results.sort()
        return list(dict.fromkeys(results))

    def _ast_refs(self, name, src):
        try: tree = ast.parse(src)
        except SyntaxError: return []
        results = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == name:
                results.append((node.lineno-1, node.col_offset))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == name:
                    results.append((node.lineno-1, node.col_offset))
        results.sort()
        return list(dict.fromkeys(results))

    def _py_file(self):
        return bool(self.buf.filename and self.buf.filename.endswith(('.py', '.pyw')))

    def _gd(self):
        if not self._py_file():
            self.status_msg = 'gd: Python files only'; self.status_err = True; return
        word = self._word_under_cursor()
        if not word: self.status_msg = 'No word under cursor'; return
        src = '\n'.join(self.buf.lines)
        defs = self._ast_defs(word, src)
        if not defs:
            self.status_msg = f'No definition: {word!r}'; self.status_err = True; return
        self._push_jump()
        if len(defs) == 1:
            self.cursor.row, self.cursor.col = defs[0]; self._first_nonblank(); return
        entries = [(f'L{r+1}: {self.buf.get_line(r).strip()[:60]}', r, 0) for r, _ in defs]
        self._open_picker(f'Definitions of {word!r}', entries)

    def _gr(self):
        if not self._py_file():
            self.status_msg = 'gr: Python files only'; self.status_err = True; return
        word = self._word_under_cursor()
        if not word: self.status_msg = 'No word under cursor'; return
        src = '\n'.join(self.buf.lines)
        refs = self._ast_refs(word, src)
        if not refs:
            self.status_msg = f'No references: {word!r}'; self.status_err = True; return
        entries = [(f'L{r+1}: {self.buf.get_line(r).strip()[:60]}', r, c) for r, c in refs]
        self._push_jump()
        self._open_picker(f'References to {word!r} ({len(refs)})', entries)

    # ── Picker (multi-result navigation) ─────────────────────────────────────

    def _open_picker(self, title, entries, cb=None, live=False, source=None):
        self.picker_title = title
        self.picker_entries = entries
        self.picker_sel = 0; self.picker_top = 0
        self.picker_cb = cb
        self.picker_live = live          # interactive (type-to-filter) picker
        self.picker_query = ''           # current filter text
        self.picker_source = source or []  # full label list for live filtering
        self.mode = Mode.PICKER

    def _picker_refilter(self):
        """Rebuild picker_entries from picker_source using the current query."""
        q = self.picker_query
        labels = ([f for f in self.picker_source if self._fuzzy_match(q, f)]
                  if q else self.picker_source)
        self.picker_entries = [(f, 0, 0) for f in labels[:1000]]
        self.picker_sel = 0; self.picker_top = 0

    def _draw_picker(self):
        self.stdscr.erase(); w = self.width
        n = len(self.picker_entries)
        if self.picker_live:
            title = f' {self.picker_title} ({n}) '
            self._as(0, 0, title[:w-1].ljust(w-1), curses.color_pair(1)|curses.A_BOLD)
            self._as(1, 0, ('> '+self.picker_query)[:w-1].ljust(w-1), curses.A_BOLD)
        else:
            self._as(0, 0, f' {self.picker_title} '[:w-1].ljust(w-1), curses.color_pair(1)|curses.A_BOLD)
            self._as(1, 0, '─'*(w-1), curses.A_DIM)
        body_h = self.height - 4
        if self.picker_sel < self.picker_top: self.picker_top = self.picker_sel
        elif self.picker_sel >= self.picker_top+body_h: self.picker_top = self.picker_sel-body_h+1
        for i in range(body_h):
            idx = self.picker_top+i
            if idx >= n: break
            label, row, col = self.picker_entries[idx]
            display = ('  '+label)[:w-1].ljust(w-1)
            attr = (curses.color_pair(8)|curses.A_BOLD if idx == self.picker_sel else 0)
            self._as(2+i, 0, display, attr)
        footer = (' type:filter  ↑↓/Ctrl-N/P:move  Enter:open  Esc:close '
                  if self.picker_live else
                  ' j/k:move  Enter:jump  q/Esc:close  Ctrl-O:back ')
        self._as(self.height-2, 0, footer[:w-1].ljust(w-1), curses.color_pair(1))
        if self.status_msg:
            self._as(self.height-1, 0, self.status_msg[:w-1],
                     curses.color_pair(4) if self.status_err else 0)
        self.stdscr.refresh()

    def _picker_key(self, key):
        live = self.picker_live
        ch = chr(key) if 32<=key<=126 else None
        n = len(self.picker_entries)
        if   key==curses.KEY_UP   or key==16 or (not live and ch=='k'):
            self.picker_sel = max(0, self.picker_sel-1)
        elif key==curses.KEY_DOWN or key==14 or (not live and ch=='j'):
            self.picker_sel = min(n-1, self.picker_sel+1)
        elif key==6  or key==curses.KEY_NPAGE: self.picker_sel=min(n-1,self.picker_sel+max(1,self.height-6))
        elif (key==2 and not live) or key==curses.KEY_PPAGE: self.picker_sel=max(0,self.picker_sel-max(1,self.height-6))
        elif key==curses.KEY_HOME: self.picker_sel = 0
        elif key==curses.KEY_END:  self.picker_sel = n-1
        elif key in (10, 13) or (key==curses.KEY_RIGHT and not live):
            if self.picker_entries:
                label, row, col = self.picker_entries[self.picker_sel]
                self.mode = Mode.NORMAL
                if self.picker_cb:
                    cb = self.picker_cb; self.picker_cb = None; cb(label)
                else:
                    self.cursor.row = row; self.cursor.col = col; self._clamp()
        elif key==27 or (not live and (ch=='q' or key==15)):   # Esc (always) / q / Ctrl-O
            self.picker_cb = None; self.picker_live = False; self.mode = Mode.NORMAL; self._jump_back()
        elif live and key in (curses.KEY_BACKSPACE, 127, 8):
            self.picker_query = self.picker_query[:-1]; self._picker_refilter()
        elif live and key==21:   # Ctrl-U: clear query
            self.picker_query = ''; self._picker_refilter()
        elif live and ch is not None:
            self.picker_query += ch; self._picker_refilter()

    def _normal_g(self, count):
        k2=self.stdscr.getch(); ch2=chr(k2) if 32<=k2<=126 else None
        if ch2=='g': self.cursor.row=max(0,count-1); self._first_nonblank()
        elif ch2=='_':
            line=self.buf.get_line(self.cursor.row); self.cursor.col=max(0,len(line.rstrip())-1)
        elif ch2=='e': self._word_end(False)
        elif ch2=='E': self._word_end(True)
        elif ch2=='d': self._gd()
        elif ch2=='r': self._gr()
        elif ch2=='j': [self._visual_move(1)  for _ in range(max(1,count))]
        elif ch2=='k': [self._visual_move(-1) for _ in range(max(1,count))]

    def _visual_move(self, dr):
        """Move cursor one visual (wrapped) row. With wrap off, equivalent to j/k."""
        p = self._pane
        if not p.wrap:
            self._move(dr, 0); return
        tw = p.width - self._pane_lnw(p)
        line = p.buf.get_line(p.cursor.row)
        segs = _wrap_segments(line, tw)
        seg_idx = self._cursor_seg_idx(p)
        seg_start, _seg_end, hang = segs[seg_idx]
        want = p.cursor.col_want - seg_start + len(hang)  # visual column want
        if dr > 0 and seg_idx + 1 < len(segs):
            ns, ne, nh = segs[seg_idx + 1]
            p.cursor.col = max(ns, min(ne - 1, ns + max(0, want - len(nh))))
        elif dr < 0 and seg_idx > 0:
            ns, ne, nh = segs[seg_idx - 1]
            p.cursor.col = max(ns, min(ne - 1, ns + max(0, want - len(nh))))
        else:
            # Cross row boundary — fall back to buffer-line move, landing on the
            # appropriate end segment of the new row.
            self._move(dr, 0)
            if not p.wrap: return
            line2 = p.buf.get_line(p.cursor.row)
            segs2 = _wrap_segments(line2, tw)
            if dr > 0:
                target = segs2[0]
            else:
                target = segs2[-1]
            ns, ne, nh = target
            p.cursor.col = max(ns, min(ne - (0 if self.mode == Mode.INSERT else 1),
                                       ns + max(0, want - len(nh))))

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
        elif ch2=='r': self._rotate_panes(1)
        elif ch2=='R': self._rotate_panes(-1)
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
            if _is_notebook(self.buf.filename) and self._nb_on_sep():
                if op=='d': self._nb_cut_cell()
                elif op=='y': self._nb_yank_cell()
                return
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
            if self._on_cell_row(): self._cell_next()
            else:
                for _ in range(4): self.buf.insert_char(self.cursor.row,self.cursor.col,' '); self.cursor.col+=1
        elif key==353:  # Shift-Tab
            if self._on_cell_row(): self._cell_prev()
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
            if _is_notebook(self.buf.filename): self._nb_eval_range(sr, er)
            else: self._eval_region(sr, er)
            self._enter_normal(); return
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
            if _is_notebook(self.buf.filename): self._nb_eval_range(r1, r2)
            else: self._eval_region(r1, r2)
            self._enter_normal(); return

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

    # ── Notebook cell operations ──────────────────────────────────────────────

    def _nb_on_sep(self):
        """True when cursor is on a notebook cell separator (cell-nav mode)."""
        return self.buf.get_line(self.cursor.row).startswith('# %% ')

    def _nb_move_down(self, count=1):
        """j in notebook.
        On separator (cell-nav): jump to next cell's separator.
        On source line (in-cell): move down within cell, clamped at last source line.
        """
        for _ in range(count):
            cells = _nb_cells(self.buf)
            idx = _nb_cell_idx_at(cells, self.cursor.row)
            if idx is None: self._move(1, 0); continue
            if self._nb_on_sep():
                if idx + 1 < len(cells):
                    self.cursor.row = cells[idx+1][0]
                    self.cursor.col = 0; self.cursor.col_want = 0
            else:
                _, src_start, src_end, _ = cells[idx]
                if self.cursor.row < src_end:
                    self.cursor.row += 1
                    line = self.buf.get_line(self.cursor.row)
                    self.cursor.col = min(self.cursor.col_want, max(0, len(line)-1))

    def _nb_move_up(self, count=1):
        """k in notebook.
        On separator (cell-nav): jump to previous cell's separator.
        On source line (in-cell): move up within cell, clamped at first source line.
        """
        for _ in range(count):
            cells = _nb_cells(self.buf)
            idx = _nb_cell_idx_at(cells, self.cursor.row)
            if idx is None: self._move(-1, 0); continue
            if self._nb_on_sep():
                if idx > 0:
                    self.cursor.row = cells[idx-1][0]
                    self.cursor.col = 0; self.cursor.col_want = 0
            else:
                _, src_start, src_end, _ = cells[idx]
                if self.cursor.row > src_start:
                    self.cursor.row -= 1
                    line = self.buf.get_line(self.cursor.row)
                    self.cursor.col = min(self.cursor.col_want, max(0, len(line)-1))

    def _nb_enter_cell(self):
        """If cursor is on a separator line, jump to the first source line of that cell."""
        if not _is_notebook(self.buf.filename): return
        line = self.buf.get_line(self.cursor.row)
        if not line.startswith('# %% '): return
        cells = _nb_cells(self.buf)
        idx = _nb_cell_idx_at(cells, self.cursor.row)
        if idx is None: return
        _, src_start, _, _ = cells[idx]
        if src_start < self.buf.line_count():
            self.cursor.row = src_start
            self.cursor.col = 0; self.cursor.col_want = 0

    def _nb_eval_range(self, r1, r2):
        """Ctrl-E over a visual selection: run every code cell that overlaps [r1, r2].
        Markdown cells in the selection are silently skipped."""
        cells = _nb_cells(self.buf)
        for i, (sep, src_start, src_end, ct) in enumerate(cells):
            next_sep = cells[i+1][0] if i+1 < len(cells) else len(self.buf.lines)
            cell_end = next_sep - 1
            # Cell overlaps selection and is a code cell with source
            if ct == 'code' and src_start <= src_end and sep <= r2 and cell_end >= r1:
                # Temporarily move cursor into this cell so _nb_eval_cell works
                saved = self.cursor.row
                self.cursor.row = src_start
                self._nb_eval_cell()
                # _nb_eval_cell may have inserted output lines; recompute cells
                cells = _nb_cells(self.buf)
                self.cursor.row = saved

    def _nb_eval_cell(self):
        """Run the current notebook code cell; replace its output lines.

        Execution runs in a daemon thread so Ctrl-C (^C) can interrupt it
        without killing the editor.
        """
        import io, contextlib, traceback, threading, ctypes
        cells = _nb_cells(self.buf)
        idx = _nb_cell_idx_at(cells, self.cursor.row)
        if idx is None:
            self.status_msg = 'Not in a notebook cell'; return
        sep, src_start, src_end, ct = cells[idx]
        if ct != 'code':
            self.status_msg = 'Not a code cell'; return
        if src_start > src_end:
            self.status_msg = 'Empty cell'; return
        self.buf.save_undo()
        # Remove old output lines between src_end and next separator
        next_sep = cells[idx+1][0] if idx+1 < len(cells) else len(self.buf.lines)
        r = src_end + 1
        while r < next_sep:
            if self.buf.lines[r].startswith('# >> '):
                self.buf.delete_line(r); next_sep -= 1
            else:
                r += 1
        ns = self._eval_ns()
        src_lines = [self.buf.get_line(r) for r in range(src_start, src_end+1)]
        code = '\n'.join(src_lines)

        # ── run in a thread so Ctrl-C can interrupt ───────────────────────────
        result_box = [None, None]   # [output_str, err_msg_or_None]

        def _run():
            out = io.StringIO()
            last_val = None
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    last_line = src_lines[-1] if src_lines else ''
                    try:
                        last_expr = compile(last_line, '<expr>', 'eval')
                        rest = '\n'.join(src_lines[:-1])
                        if rest.strip():
                            exec(compile(rest, '<nb>', 'exec'), ns)
                        last_val = eval(last_expr, ns)
                    except SyntaxError:
                        exec(compile(code, '<nb>', 'exec'), ns)
                result = out.getvalue()
                if last_val is not None:
                    result += repr(last_val) + '\n'
                result_box[0] = result
            except KeyboardInterrupt:
                result_box[0] = out.getvalue()
                result_box[1] = 'KeyboardInterrupt'
            except Exception:
                result_box[0] = out.getvalue() + traceback.format_exc()
                result_box[1] = traceback.format_exc().splitlines()[-1]

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        self.status_msg = '▶ running…  (^C to interrupt)'
        self.status_err = False
        self.stdscr.timeout(100)          # fast poll while waiting
        try:
            while t.is_alive():
                self._clamp(); self._scroll(); self.draw()
                k = self.stdscr.getch()
                if k == 3:               # Ctrl-C → raise KI in the worker thread
                    ctypes.pythonapi.PyThreadState_SetAsyncExc(
                        ctypes.c_ulong(t.ident),
                        ctypes.py_object(KeyboardInterrupt))
                    t.join(timeout=2.0)
                    break
        finally:
            self.stdscr.timeout(250)     # restore normal 250 ms timeout
        t.join(timeout=0)

        result  = result_box[0] or ''
        err_msg = result_box[1]
        if err_msg:
            self.status_err = True
            self.status_msg = err_msg
        else:
            self.status_msg = 'cell ok'; self.status_err = False
        if result.strip():
            lines = result.rstrip('\n').splitlines()
            for i, line in enumerate(lines):
                self.buf.insert_line(src_end + 1 + i, '# >> ' + line)

    def _nb_cell_next(self):
        """Jump to the first source line of the next notebook cell."""
        cells = _nb_cells(self.buf)
        idx = _nb_cell_idx_at(cells, self.cursor.row)
        if idx is None or idx + 1 >= len(cells):
            self.status_msg = 'No next cell'; return
        _, src_start, _, _ = cells[idx+1]
        self.cursor.row = min(src_start, len(self.buf.lines)-1)
        self.cursor.col = 0; self.cursor.col_want = 0

    def _nb_cell_prev(self):
        """Jump to the first source line of the previous notebook cell."""
        cells = _nb_cells(self.buf)
        idx = _nb_cell_idx_at(cells, self.cursor.row)
        if idx is None or idx == 0:
            self.status_msg = 'No previous cell'; return
        _, src_start, _, _ = cells[idx-1]
        self.cursor.row = min(src_start, len(self.buf.lines)-1)
        self.cursor.col = 0; self.cursor.col_want = 0

    def _nb_insert_cell(self, below=True):
        """Insert a new empty code cell above or below the current cell."""
        self.buf.save_undo()
        cells = _nb_cells(self.buf)
        idx = _nb_cell_idx_at(cells, self.cursor.row)
        if idx is None: return
        sep, src_start, src_end, ct = cells[idx]
        if below:
            # Insert after all output lines (before the next separator or EOF)
            insert_at = cells[idx+1][0] if idx+1 < len(cells) else len(self.buf.lines)
        else:
            insert_at = sep
        self.buf.insert_line(insert_at, '')            # empty source line
        self.buf.insert_line(insert_at, '# %% code')  # separator
        self.cursor.row = insert_at + 1
        self.cursor.col = 0; self.cursor.col_want = 0
        self.mode = Mode.INSERT

    def _nb_yank_cell(self):
        """Yank the current cell (separator + source + output) into register_cell."""
        cells = _nb_cells(self.buf)
        idx = _nb_cell_idx_at(cells, self.cursor.row)
        if idx is None: return
        sep = cells[idx][0]
        end = (cells[idx+1][0] if idx+1 < len(cells) else len(self.buf.lines)) - 1
        self.register_cell = list(self.buf.lines[sep:end+1])
        self.status_msg = f'yanked {len(self.register_cell)} lines'

    def _nb_cut_cell(self):
        """Cut the current cell into register_cell and remove it from the buffer."""
        cells = _nb_cells(self.buf)
        idx = _nb_cell_idx_at(cells, self.cursor.row)
        if idx is None: return
        sep = cells[idx][0]
        end = (cells[idx+1][0] if idx+1 < len(cells) else len(self.buf.lines)) - 1
        self.register_cell = list(self.buf.lines[sep:end+1])
        self.buf.save_undo()
        for r in range(end, sep-1, -1):
            self.buf.delete_line(r)
        if self.buf.line_count() == 0:
            self.buf.insert_line(0, '# %% code')
            self.buf.insert_line(1, '')
        new_row = min(sep, self.buf.line_count()-1)
        while new_row > 0 and not self.buf.lines[new_row].startswith('# %% '):
            new_row -= 1
        self.cursor.row = new_row
        self.cursor.col = 0; self.cursor.col_want = 0
        self.status_msg = f'cut {len(self.register_cell)} lines'

    def _nb_paste_cell(self, below=True):
        """Paste register_cell above or below the current cell."""
        if not self.register_cell: return
        cells = _nb_cells(self.buf)
        idx = _nb_cell_idx_at(cells, self.cursor.row)
        if idx is None: return
        sep = cells[idx][0]
        if below:
            insert_at = cells[idx+1][0] if idx+1 < len(cells) else len(self.buf.lines)
        else:
            insert_at = sep
        self.buf.save_undo()
        for i, line in enumerate(self.register_cell):
            self.buf.insert_line(insert_at + i, line)
        self.cursor.row = insert_at
        self.cursor.col = 0; self.cursor.col_want = 0

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

    def _disk_changed(self, fname=None):
        """Return True if the file on disk has been modified since we last read/wrote it."""
        target=fname or self.buf.filename
        if not target: return False
        try: return os.path.getmtime(target) != self.buf._disk_mtime
        except OSError: return False

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
        elif re.match(r'^wq!?$',cmd) or cmd in ('x','x!'):
            parts=cmd.split(None,1); fname=parts[1] if len(parts)>1 else None
            force='!' in cmd or cmd=='x'
            if not force and self._disk_changed(fname):
                def _do_wq(f=fname):
                    try: self.buf.save(f); self._close_pane(force=True)
                    except Exception as e: self.status_msg=str(e); self.status_err=True
                self._confirm_cb=_do_wq
                self._confirm_msg=f'"{fname or self.buf.filename}" changed on disk. Overwrite? [y/N] '
            else:
                try: self.buf.save(fname); self._close_pane(force=True)
                except Exception as e: self.status_msg=str(e); self.status_err=True
        elif cmd.startswith('w') and (len(cmd)==1 or cmd[1] in ' !'):
            parts=cmd.split(None,1); fname=parts[1] if len(parts)>1 else None
            force='!' in cmd
            if not force and self._disk_changed(fname):
                def _do_w(f=fname):
                    try:
                        self.buf.save(f)
                        self.status_msg=f'"{self.buf.filename}" {self.buf.line_count()}L written'
                    except Exception as e: self.status_msg=str(e); self.status_err=True
                self._confirm_cb=_do_w
                self._confirm_msg=f'"{fname or self.buf.filename}" changed on disk. Overwrite? [y/N] '
            else:
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
        elif re.match(r'^fin?d?\b', cmd):
            parts = cmd.split(None, 1)
            self._cmd_find(parts[1].strip() if len(parts) > 1 else '')
        elif cmd.startswith('set ') or cmd=='set':
            self._exec_set(cmd[4:].strip() if cmd.startswith('set ') else '')
        elif re.match(r'^(%|\.|\d+(,(\d+|\.|\$))?)?s',cmd):
            self._exec_sub(cmd)
        elif cmd.startswith('colorscheme') or cmd.startswith('colo'):
            parts=cmd.split(None,1)
            if len(parts)<2:
                self.status_msg=f'colorscheme: {", ".join(_SCHEMES)}'; self.status_err=False
            else:
                name=parts[1].strip()
                if name not in _SCHEMES:
                    self.status_msg=f'Unknown colorscheme: {name}'; self.status_err=True
                else:
                    _apply_scheme(name)
                    self.status_msg=f'colorscheme: {name}'; self.status_err=False
        elif cmd in ('noh','nohlsearch'): self.search_pat=''
        elif cmd in ('jpreview','jp','JsonPreview','jsonpreview'): self._toggle_json_preview()
        elif cmd in ('close','clo'): self._close_pane()
        elif cmd in ('close!','clo!'): self._close_pane(force=True)
        elif cmd=='only':
            while len(self._panes)>1: self._close_pane(force=True)  # keep closing non-active
        elif re.match(r'^[Ee]x(plore)?(\s|$)',cmd) or re.match(r'^[Ll]ex(plore)?(\s|$)',cmd):
            parts=cmd.split(None,1); path=parts[1].strip() if len(parts)>1 else None
            self._open_explorer(path)
        elif cmd in ('history','historyw','undolist'):
            self.hist_sel=0; self.hist_top=0; self.hist_preview_scroll=0
            self._hist_git_cache=self._git_hist(); self._hist_preview_cache={}
            self.mode=Mode.HISTORY
        elif re.match(r'^earlier\b',cmd):
            self._cmd_earlier_later(cmd, forward=False)
        elif re.match(r'^later\b',cmd):
            self._cmd_earlier_later(cmd, forward=True)
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

    def _collect_project_files(self):
        """Return (files, root): project files (git-tracked from repo top, else os.walk)."""
        import subprocess as _sp
        # Prefer git ls-files (tracked files only — no untracked noise)
        try:
            res = _sp.run(['git', 'ls-files', '--cached'],
                          capture_output=True, text=True, cwd=os.getcwd(), timeout=5)
            root = _sp.run(['git', 'rev-parse', '--show-toplevel'],
                           capture_output=True, text=True, cwd=os.getcwd()).stdout.strip()
            files = res.stdout.splitlines() if res.returncode == 0 else None
        except Exception:
            files = None; root = os.getcwd()
        if not files:
            root = os.getcwd(); files = []
            for dp, dns, fns in os.walk(root):
                dns[:] = [d for d in dns if not d.startswith('.')]
                for f in fns:
                    files.append(os.path.relpath(os.path.join(dp, f), root))
        # Drop any path whose components include hidden dirs/files
        files = [f for f in files if not any(p.startswith('.') for p in f.replace('\\', '/').split('/'))]
        return sorted(files), (root or os.getcwd())

    @staticmethod
    def _fuzzy_match(pattern, path):
        """True if every char of pattern appears in path in order (case-insensitive)."""
        s = path.lower(); i = 0
        for c in pattern.lower():
            i = s.find(c, i)
            if i < 0: return False
            i += 1
        return True

    def _cmd_find(self, pattern):
        """Fuzzy file finder. With a pattern, filters and opens a static picker;
        with none, opens an interactive Ctrl-P style picker that filters as you type."""
        files, root = self._collect_project_files()
        if not pattern:
            self._open_file_picker(files, root); return
        files = [f for f in files if self._fuzzy_match(pattern, f)][:1000]
        if not files:
            self.status_msg = f'No files matching {pattern!r}'; self.status_err = True; return
        entries = [(f, 0, 0) for f in files]
        _root = root
        def _open(label):
            self._load_file(os.path.join(_root, label))
        self._open_picker(f'find: {pattern}', entries, cb=_open)

    def _open_file_picker(self, files, root):
        """Open an interactive (live-filtering) file picker rooted at root."""
        _root = root
        def _open(label):
            self._load_file(os.path.join(_root, label))
        self._open_picker('Find file', [(f, 0, 0) for f in files], cb=_open,
                          live=True, source=files)

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
        elif opt in ('wrap', 'nowrap', 'wrap!'):
            self._pane.wrap = (opt == 'wrap' if opt != 'wrap!' else not self._pane.wrap)
            if self._pane.wrap: self._pane.left_col = 0
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

    # ── History ───────────────────────────────────────────────────────────────

    def _cmd_earlier_later(self, cmd, forward):
        parts=cmd.split(None,1); arg=parts[1].strip() if len(parts)>1 else '1'
        m=re.match(r'^(\d+)([smh]?)$',arg)
        if not m: self.status_msg=f'Invalid argument: {arg}'; self.status_err=True; return
        n=int(m.group(1)); unit=m.group(2)
        if unit:
            secs={'s':1,'m':60,'h':3600}[unit]*n; target=time.time()-secs; steps=0
            if forward:
                while self.buf._redo and self.buf._redo[-1][1]>target: self.buf.redo(); steps+=1
            else:
                while self.buf._undo and self.buf._undo[-1][1]>=target: self.buf.undo(); steps+=1
            self.status_msg=f'{"later" if forward else "earlier"} {n}{unit}: {steps} change{"s" if steps!=1 else ""}'
        else:
            steps=0
            for _ in range(n):
                ok=self.buf.redo() if forward else self.buf.undo()
                if not ok: break
                steps+=1
            self.status_msg=f'{"Later" if forward else "Earlier"} {steps} change{"s" if steps!=1 else ""}'
        self._clamp()

    @staticmethod
    def _rel_time(ts):
        d=int(time.time()-ts)
        if d<60:    return f'{d}s ago'
        if d<3600:  return f'{d//60}m ago'
        if d<86400: return f'{d//3600}h ago'
        return f'{d//86400}d ago'

    def _git_root_relpath(self):
        """Return (git_root, relpath) for the current buffer, or (None, None)."""
        fname=self.buf.filename
        if not fname: return None,None
        import subprocess
        try:
            r=subprocess.run(['git','rev-parse','--show-toplevel'],
                capture_output=True,text=True,timeout=3,
                cwd=os.path.dirname(os.path.abspath(fname)) or '.')
            if r.returncode!=0: return None,None
            root=r.stdout.strip()
            return root,os.path.relpath(os.path.abspath(fname),root)
        except Exception: return None,None

    def _git_file_at(self, commit_hash):
        """Return list[str] of the current file's lines at commit_hash, or []."""
        if commit_hash in self._hist_preview_cache:
            return self._hist_preview_cache[commit_hash]
        root,relpath=self._git_root_relpath()
        if root is None: return []
        import subprocess
        try:
            r=subprocess.run(['git','show',f'{commit_hash}:{relpath}'],
                capture_output=True,text=True,timeout=5,cwd=root)
            lines=r.stdout.splitlines() if r.returncode==0 else []
        except Exception: lines=[]
        self._hist_preview_cache[commit_hash]=lines
        return lines

    def _git_hist(self):
        fname=self.buf.filename
        if not fname: return []
        import subprocess
        try:
            r=subprocess.run(
                ['git','log','--pretty=format:%h\t%at\t%s','--follow','--',fname],
                capture_output=True,text=True,timeout=3,
                cwd=os.path.dirname(os.path.abspath(fname)) or '.')
            if r.returncode!=0 or not r.stdout.strip(): return []
            out=[]
            for line in r.stdout.strip().splitlines():
                parts=line.split('\t',2)
                if len(parts)==3:
                    out.append({'hash':parts[0],'ts':float(parts[1]),'subject':parts[2]})
            return out
        except Exception: return []

    def _git_load(self, commit_hash):
        lines=self._git_file_at(commit_hash)
        if not lines and not self.buf.filename: return
        import subprocess
        root,relpath=self._git_root_relpath()
        if root is None:
            self.status_msg='Not in a git repo'; self.status_err=True; return
        self.buf.save_undo()
        self.buf.lines=lines or ['']
        self.buf._gen+=1; self.buf.modified=True
        self.status_msg=f'Loaded {commit_hash}'

    def _hist_entries(self):
        entries=[]
        entries.append({'kind':'undo','step':0,'nlines':len(self.buf.lines),'ts':time.time()})
        for i,(snap,ts) in enumerate(reversed(self.buf._undo),1):
            entries.append({'kind':'undo','step':i,'nlines':len(snap),'ts':ts})
        if self._hist_git_cache:
            entries.append({'kind':'divider','label':'── git history '})
            for g in self._hist_git_cache:
                entries.append({'kind':'git','hash':g['hash'],'ts':g['ts'],'subject':g['subject']})
        return entries

    def _hist_preview_lines(self, entry):
        """Return (lines, is_diff) for the preview pane."""
        try:
            if entry['kind']=='undo':
                step=entry['step']
                if step==0:
                    if self.hist_diff_mode and self.buf._undo:
                        # Show what changed to get to current state
                        prev_lines=list(self.buf._undo[-1][0])
                        diff=list(difflib.unified_diff(prev_lines,self.buf.lines,
                            fromfile='prev',tofile='now',lineterm='',n=3))
                        return diff or ['(no changes)'],True
                    return list(self.buf.lines),False
                idx=len(self.buf._undo)-step
                target=list(self.buf._undo[idx][0]) if 0<=idx<len(self.buf._undo) else list(self.buf.lines)
            elif entry['kind']=='git':
                target=self._git_file_at(entry['hash'])
            else:
                return [],False
            if self.hist_diff_mode:
                diff=list(difflib.unified_diff(target,self.buf.lines,
                    fromfile=entry.get('hash',f'step {entry.get("step","")}'),tofile='now',
                    lineterm='',n=3))
                return diff or ['(no changes)'],True
            return target,False
        except Exception as exc:
            return [f'Error: {exc}'],False

    def _draw_history(self):
        self.stdscr.erase(); w=self.width; h=self.height
        entries=self._hist_entries(); n=len(entries); body_h=h-4

        # Layout: left list | divider | right preview (only when wide enough)
        has_preview=(w>=55)
        left_w=min(36,w//2) if has_preview else w
        rx=left_w+1; right_w=w-rx

        # Header
        fname=self.buf.filename or '[No Name]'
        mode_lbl='diff' if self.hist_diff_mode else 'content'
        self._as(0,0,f' History — {fname}  [{mode_lbl}]'[:w-1].ljust(w-1),curses.color_pair(1)|curses.A_BOLD)
        self._as(1,0,'─'*(w-1),curses.A_DIM)

        # Scroll list
        if self.hist_sel<self.hist_top: self.hist_top=self.hist_sel
        elif self.hist_sel>=self.hist_top+body_h: self.hist_top=self.hist_sel-body_h+1

        # Left: history list
        for i in range(body_h):
            idx=self.hist_top+i; sy=2+i
            if idx>=n: break
            e=entries[idx]; sel=(idx==self.hist_sel); lw=left_w-1
            if e['kind']=='divider':
                self._as(sy,0,(e['label']+'─'*w)[:lw].ljust(lw),curses.A_DIM)
            elif e['kind']=='undo':
                ts_str=time.strftime('%H:%M:%S',time.localtime(e['ts']))
                if e['step']==0:
                    row=f'  {"now":<6}  {ts_str}  {e["nlines"]}L'
                else:
                    prev=entries[e['step']-1]['nlines']
                    d=e['nlines']-prev
                    row=f'  {e["step"]:<6}  {ts_str}  {e["nlines"]}L'+(f'  {"+"+str(d) if d>0 else str(d)}' if d else '')
                self._as(sy,0,row[:lw].ljust(lw),curses.color_pair(8)|curses.A_BOLD if sel else 0)
            elif e['kind']=='git':
                rel=self._rel_time(e['ts'])
                row=f'  {e["hash"]}  {rel:<9}  {e["subject"]}'
                self._as(sy,0,row[:lw].ljust(lw),curses.color_pair(8)|curses.A_BOLD if sel else curses.color_pair(7))

        # Right: preview pane
        if has_preview:
            for sy in range(2,h-2): self._as(sy,left_w,'│',curses.A_DIM)
            cur_e=entries[self.hist_sel] if 0<=self.hist_sel<n else None
            preview,is_diff=(self._hist_preview_lines(cur_e) if cur_e and cur_e['kind']!='divider' else ([],False))
            max_scroll=max(0,len(preview)-body_h)
            self.hist_preview_scroll=min(self.hist_preview_scroll,max_scroll)
            def _diff_style(line):
                """Return (line_attr, prefix_attr) for a unified diff line."""
                if not line: return curses.A_DIM, 0
                if line.startswith('---') or line.startswith('+++'):
                    return curses.A_DIM, curses.A_DIM
                c=line[0]
                if c=='+': return curses.color_pair(20), curses.color_pair(20)|curses.A_BOLD
                if c=='-': return curses.color_pair(21), curses.color_pair(21)|curses.A_BOLD
                if c=='@': return curses.color_pair(22)|curses.A_BOLD, curses.color_pair(22)|curses.A_BOLD
                return curses.A_DIM, curses.A_DIM  # context
            for i in range(body_h):
                sy=2+i; pidx=self.hist_preview_scroll+i
                if pidx<len(preview):
                    line=preview[pidx]
                    if is_diff:
                        la,pa=_diff_style(line)
                        pad=right_w-1
                        if line and len(line)>1 and pa!=la:
                            self._as(sy,rx,line[0],pa)
                            self._as(sy,rx+1,(line[1:pad]).ljust(pad-1),la)
                        else:
                            self._as(sy,rx,line[:pad].ljust(pad),la)
                    else:
                        self._as(sy,rx,line[:right_w-1].ljust(right_w-1),0)
                else:
                    self._as(sy,rx,' '*(right_w-1))

        footer=' j/k:history  J/K:scroll preview  d:diff/content  Enter:restore  q:close'
        self._as(h-2,0,footer[:w-1].ljust(w-1),curses.color_pair(1))
        try: self.stdscr.move(h-1,0)
        except curses.error: pass
        self.stdscr.refresh()

    def _history_key(self, key):
        ch=chr(key) if 32<=key<=126 else None
        entries=self._hist_entries(); n=len(entries)
        def _skip(idx,step):
            while 0<=idx<n and entries[idx]['kind']=='divider': idx+=step
            return max(0,min(n-1,idx))
        def _nav(new_sel):
            self.hist_sel=new_sel; self.hist_preview_scroll=0
        if   key==curses.KEY_UP   or ch=='k': _nav(_skip(self.hist_sel-1,-1))
        elif key==curses.KEY_DOWN or ch=='j': _nav(_skip(self.hist_sel+1,1))
        elif key==6  or key==curses.KEY_NPAGE: _nav(_skip(self.hist_sel+max(1,self.height-6),1))
        elif key==2  or key==curses.KEY_PPAGE: _nav(_skip(self.hist_sel-max(1,self.height-6),-1))
        elif key==curses.KEY_HOME: _nav(0)
        elif key==curses.KEY_END:  _nav(_skip(n-1,-1))
        elif ch=='J': self.hist_preview_scroll+=3
        elif ch=='K': self.hist_preview_scroll=max(0,self.hist_preview_scroll-3)
        elif ch=='d': self.hist_diff_mode=not self.hist_diff_mode; self.hist_preview_scroll=0
        elif key in (10,13):
            e=entries[self.hist_sel]
            if e['kind']=='undo':
                steps=e['step']
                for _ in range(steps): self.buf.undo()
                self._clamp(); self.mode=Mode.NORMAL
                self.status_msg=f'Restored {steps} step{"s" if steps!=1 else ""} back' if steps else ''
            elif e['kind']=='git':
                self._git_load(e['hash']); self._clamp(); self.mode=Mode.NORMAL
        elif ch=='q' or key==27: self.mode=Mode.NORMAL

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
        self.ex_sel=0; self.ex_top=0; self.ex_searching=False

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
        footer=' j/k:move  Enter:open  -:up  q:back  /:search  n/N:next/prev  R:refresh'
        self._as(self.height-2,0,footer[:w-1].ljust(w-1),curses.color_pair(1))
        if self.ex_searching:
            prompt=('/'+self.ex_search_query+' '*w)[:w-1]
            self._as(self.height-1,0,prompt)
            try: self.stdscr.move(self.height-1,min(1+len(self.ex_search_query),w-1))
            except curses.error: pass
        elif self.status_msg:
            self._as(self.height-1,0,self.status_msg[:w-1],curses.color_pair(4) if self.status_err else 0)
            try: self.stdscr.move(self.height-1,0)
            except curses.error: pass
        else:
            try: self.stdscr.move(self.height-1,0)
            except curses.error: pass
        self.stdscr.refresh()

    def _ex_search_jump(self, pat, start, forward=True):
        """Jump ex_sel to the next entry whose name matches pat, wrapping around."""
        if not pat: return
        n=len(self.ex_entries)
        step=1 if forward else -1
        for i in range(1,n+1):
            idx=(start+i*step)%n
            name,_=self.ex_entries[idx]
            if pat.lower() in name.lower(): self.ex_sel=idx; return
        self.status_msg='(no match)'; self.status_err=True

    def _explorer_key(self, key):
        ch=chr(key) if 32<=key<=126 else None; n=len(self.ex_entries)
        if self.ex_searching:
            if key==27:                                          # Esc — cancel
                self.ex_searching=False; self.ex_search_query=''
            elif key in (10,13):                                 # Enter — confirm
                self.ex_searching=False
            elif key in (curses.KEY_BACKSPACE,127,8):
                self.ex_search_query=self.ex_search_query[:-1]
                self._ex_search_jump(self.ex_search_query,len(self.ex_entries)-1,True)
            elif 32<=key<=126:
                self.ex_search_query+=ch
                self._ex_search_jump(self.ex_search_query,len(self.ex_entries)-1,True)
            return
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
        elif ch=='/': self.ex_searching=True; self.ex_search_query=''
        elif ch=='n' and self.ex_search_query: self._ex_search_jump(self.ex_search_query,self.ex_sel,True)
        elif ch=='N' and self.ex_search_query: self._ex_search_jump(self.ex_search_query,self.ex_sel,False)

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


def _probe_sixel() -> bool:
    """Send DA1 (ESC[c) and return True if the terminal reports sixel support
    (capability code 4 in the response: ESC[?<n>;4;...c).
    Must be called before curses.wrapper() takes over the terminal."""
    if not sys.stdout.isatty():
        return False
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            sys.stdout.write('\033[c')
            sys.stdout.flush()
            resp = ''
            while True:
                r, _, _ = select.select([fd], [], [], 0.3)
                if not r:
                    break
                ch = os.read(fd, 1).decode('latin-1')
                resp += ch
                if ch == 'c':
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        m = re.search(r'\[\?([0-9;]+)c', resp)
        if m:
            return '4' in m.group(1).split(';')
    except Exception:
        pass
    return False

HAS_SIXEL:    bool = False   # populated by _probe_sixel() in main()
HAS_256COLOR: bool = False   # populated by Editor.__init__ after curses.start_color()

def main():
    global HAS_SIXEL
    import argparse, fcntl
    parser = argparse.ArgumentParser(prog='pym', description='pyvim editor')
    parser.add_argument('filename', nargs='?', default=None)
    parser.add_argument('-f', '--follow', action='store_true',
                        help='follow mode: tail new content as it arrives')
    args = parser.parse_args()

    stdin_content = None
    follow_fd = None

    if not sys.stdin.isatty():
        if args.follow:
            # Dup the pipe fd before redirecting stdin to /dev/tty
            follow_fd = os.dup(sys.stdin.fileno())
            flags = fcntl.fcntl(follow_fd, fcntl.F_GETFL)
            fcntl.fcntl(follow_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        else:
            stdin_content = sys.stdin.buffer.read().decode('utf-8', errors='replace')
        # Hand /dev/tty to curses for keyboard input
        try:
            tty_fd = os.open('/dev/tty', os.O_RDONLY)
            os.dup2(tty_fd, sys.stdin.fileno())
            os.close(tty_fd)
            sys.stdin = open('/dev/tty', 'r')
        except OSError as e:
            sys.exit(f'pym: cannot open /dev/tty: {e}')

    HAS_SIXEL = _probe_sixel()
    curses.wrapper(lambda s: Editor(s, args.filename,
                                    stdin_content=stdin_content,
                                    follow=args.follow,
                                    follow_fd=follow_fd).run())

if __name__=='__main__':
    main()
