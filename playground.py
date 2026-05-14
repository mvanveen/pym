# pyvim playground — select any block with V..j (visual line), then Ctrl-E
# state persists between evals, so blocks can build on each other
# last run: (eval block 4 to update this)

import math, random, collections, re, datetime

# ── 1. warmup ────────────────────────────────────────────────────────────────
print(f"hello. π={math.pi:.6f}  φ={(1+5**0.5)/2:.6f}  e={math.e:.6f}")

# ── 2. ascii histogram (random gaussian) ─────────────────────────────────────
random.seed(42)
data = [random.gauss(0, 1) for _ in range(400)]
lo, hi, bins = -3.5, 3.5, 14
w = (hi - lo) / bins
counts = [0] * bins
for x in data:
    i = int((x - lo) / w)
    if 0 <= i < bins:
        counts[i] += 1
for i, c in enumerate(counts):
    label = f"{lo + i*w:+.1f}"
    print(f"{label:6s} {'█' * (c // 3)}")

# ── 3. self-aware — reads the live buffer ────────────────────────────────────
lines = buf.lines
print(f"file: {buf.filename}")
print(f"lines: {len(lines)}  words: {sum(len(l.split()) for l in lines)}")
print(f"longest: {max(lines, key=len)!r}")

# ── 4. live header update (buffer surgery) ───────────────────────────────────
buf.save_undo()
buf.lines[2] = f"# last run: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
buf._gen += 1
print("header updated — check line 3")

# ── 5. fibonacci as a bar chart ──────────────────────────────────────────────
def fib(n):
    a, b = 0, 1
    for _ in range(n):
        yield a
        a, b = b, a+b

for n in fib(14):
    bar = '▓' * min(n, 55)
    print(f"{n:5d} {bar}")

# ── 6. markov chain over this file's own source ──────────────────────────────
words = re.findall(r"[a-z_]\w*", ' '.join(buf.lines).lower())
chain = collections.defaultdict(list)
for a, b in zip(words, words[1:]):
    chain[a].append(b)

def markov(seed, n=18):
    w, out = seed, [seed]
    for _ in range(n):
        nxt = chain.get(w)
        if not nxt: break
        w = random.choice(nxt)
        out.append(w)
    return ' '.join(out)

random.seed()
for seed in ('buf', 'print', 'the'):
    print(f"[{seed}] {markov(seed)}")

# ── 7. sierpinski ────────────────────────────────────────────────────────────
def sierpinski(n):
    rows = ['*']
    for _ in range(n):
        rows = [' ' * len(rows[0]) + r for r in rows] + \
               [r + ' ' + r for r in rows]
    return rows

for row in sierpinski(4):
    print(row)

# ── 8. sort selected lines (meta — eval this then visually select lines) ─────
# run this first to define the helper, then:
#   V-select any lines you want sorted, Ctrl-E
def sort_selection(r1=None, r2=None):
    if r1 is None:
        print("call as: sort_selection(r1, r2)")
        return
    buf.save_undo()
    buf.lines[r1:r2+1] = sorted(buf.lines[r1:r2+1])
    buf._gen += 1
    print(f"sorted lines {r1}–{r2}")

print("sort_selection defined — call sort_selection(r1, r2) from :py")
