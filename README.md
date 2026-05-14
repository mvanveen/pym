# pyvim

A vim clone in a single Python file, because why not.

```
uv run pyvim.py [file]
```

## What it is

Modal editor — normal, insert, visual, visual-line, command, search — with splits, a file explorer, syntax highlighting, and a live Python eval runtime baked in. Around 1900 lines of stdlib + pygments. No Rust, no Electron, no LSP daemon that eats 400MB of RAM.

The interesting part isn't the vim emulation. It's that the editor is a live Python object you can reach into while it's running.

## Modes

| Mode | Enter | Exit |
|------|-------|------|
| Normal | `Esc` | — |
| Insert | `i a o I A O` | `Esc` |
| Visual | `v` | `Esc` or `v` |
| Visual Line | `V` | `Esc` or `V` |
| Command | `:` | `Enter` / `Esc` |
| Search | `/` `?` | `Enter` / `Esc` |
| Explorer | `:Ex` | `q` / `Enter` |

## Motions and operators

Standard vim motions: `h j k l`, `w W b B e E`, `0 ^ $`, `gg G`, `{ }`, `f F t T ; ,`, `' `` marks`.

Operators: `d c y > < ~ u U` — all work with motions (`dw`, `ci"`, `y3j`, etc.) and in visual mode.

Everything you'd reach for first: `x X dd cc yy D C`, `p P`, `u` undo, `Ctrl-R` redo, `.` repeat, `*` `#` word search.

## Splits

```
:sp            horizontal split
:vsp           vertical split
Ctrl-W h/j/k/l navigate panes
Ctrl-W w       cycle panes
Ctrl-W q       close pane
```

## Commands

```
:w [file]      write
:q             close pane (quit if last)
:qa            quit all (checks for unsaved changes)
:qa!           force quit all
:e <file>      open file
:Ex            file explorer
:set nu        line numbers on/off
:noh           clear search highlight
```

## Syntax highlighting

Pygments — supports every language pygments knows. Full-file tokenization so multi-line tokens (docstrings, block comments) are correct. Cached per buffer generation so re-highlighting only happens on actual edits.

## Markdown rendering

`.md` files render inline: heading markers dim, heading text bold+yellow, `**bold**` is bold, `*italic*` is underlined, `` `code` `` is green, `[links](url)` show the text in cyan with the URL dimmed, fenced code blocks are colored through.

The cursor line always shows raw markdown so editing column positions are exact. Move away and it renders. Move back and it opens up. Feels like rich-text editing with a vim brain.

## Python eval

This is the part that got out of hand.

**`Ctrl-E`** in normal mode evals the current line. In visual/visual-line mode it evals the selection. Output is inserted as `# >> ...` comment lines immediately below, with full undo support.

**`:py`** drops into an interactive `code.interact()` REPL with `ed`, `buf`, and `pane` in scope. `Ctrl-D` returns to the editor.

**`:py <expr>`** evals an expression inline and shows the result in the status bar.

Before running a selection, pyvim does static analysis (via `ast`) to figure out what the selection needs — imports, function definitions, class definitions — and auto-runs those first. You can `Ctrl-E` any block in a Python file without having to manually eval the imports section first.

The eval namespace persists for the whole session. State accumulates. Define a function in one `Ctrl-E`, call it from another.

### What you can do in the REPL

```python
# inspect the live buffer
>>> buf.lines[:5]
>>> buf.filename

# edit with undo
>>> buf.save_undo()
>>> buf.set_line(3, 'x = 99')
>>> buf.save()

# or reach directly into the list
>>> buf.lines[0] = '#!/usr/bin/env python3'
>>> buf._gen += 1   # invalidate highlight cache

# jump cursor
>>> pane.cursor.row = 42

# run a transformation
>>> for i, l in enumerate(buf.lines):
...     buf.lines[i] = l.rstrip()
>>> buf._gen += 1; buf.modified = True

# AST-level refactoring
>>> import ast
>>> tree = ast.parse('\n'.join(buf.lines))
>>> # mutate the tree
>>> buf.lines = ast.unparse(tree).splitlines(); buf._gen += 1
```

The editor resumes on `Ctrl-D` and picks up all mutations on the next draw.

### The eval playground

`playground.py` has a set of self-contained blocks to try — gaussian histogram, fibonacci bar chart, markov chain trained on the file's own source, a `sort_selection(r1, r2)` helper, and a block that edits the file's own header timestamp. Open it, `V`-select any block, `Ctrl-E`.

## Architecture

```
Buffer          list[str] + undo stack + edit generation counter
Pane            Buffer + cursor + scroll + screen geometry (y/x/h/w)
Layout tree     _Leaf / _Split BSP tree, recomputed each draw
Editor          mode state machine + all keybindings + draw loop
```

Syntax highlighting: `_pg_highlight(buf)` tokenizes the full buffer once with pygments, maps character offsets to (row, col), caches by `(id(buf), buf._gen)`.

Markdown rendering: `_md_highlight(buf, cursor_row)` — separate path from pygments, cursor-aware so the active line is always raw.

Python eval: `_resolve_deps(r1, r2)` does AST analysis to find what a selection's free variables need, then runs exactly those imports/defs in order.

## Running

```bash
# with uv (recommended)
uv run pyvim.py
uv run pyvim.py somefile.py

# or directly if pygments is installed
python3 pyvim.py
```

Requires Python 3.9+ (uses `ast.unparse`). The only dependency is `pygments`, which is optional — syntax highlighting degrades gracefully without it if you remove the import.

## Known gaps

- No mouse support
- No macros (`q` register) yet
- No `%` bracket matching
- Ctrl-W navigation has been through several iterations of geometry bugs and may still have edge cases
- The debug log `/tmp/pyvim_nav.log` is still being written on every pane navigation (`_goto_pane_dir`) — remove the logging calls if it bothers you
- Concealment in markdown mode (truly hiding `**` markers) is not implemented; markers are dimmed but visible
