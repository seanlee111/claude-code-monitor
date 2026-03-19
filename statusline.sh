#!/usr/bin/env bash
INPUT_JSON=$(cat)
export INPUT_JSON

python3 << 'PYEOF'
import os, json, glob, time
from datetime import datetime, timedelta

d = json.loads(os.environ.get('INPUT_JSON', '{}'))

# ── Colors: 24-bit true color ───────────────────────────────────────────────
R  = '\033[0m'
B  = '\033[38;2;59;130;246m'    # #3b82f6 — blue values
D  = '\033[38;2;100;100;120m'   # gray-blue labels

# Thin bar chars (half-height blocks to avoid overlap)
FILL = '\u2584'    # ▄ lower half block — thinner than full █
EMPT = '\u2581'    # ▁ lower one-eighth — very thin empty

SEP = D + ' \u2502 ' + R

# ── Extract ─────────────────────────────────────────────────────────────────
mi = d.get('model', {})
model = mi.get('display_name', '') or mi.get('id', '') or ''
sc = d.get('cost', {}).get('total_cost_usd', 0)
cx = d.get('context_window', {})
cp = min(cx.get('used_percentage', 0), 100)
cs = cx.get('context_window_size', 200000)
ti = cx.get('total_input_tokens', 0)
to = cx.get('total_output_tokens', 0)
cu = cx.get('current_usage', {})
cr = cu.get('cache_read_input_tokens', 0)
cw = cu.get('cache_creation_input_tokens', 0)

def ft(n):
    if n >= 1e6: return '{:.1f}M'.format(n/1e6)
    if n >= 1e3: return '{:.1f}K'.format(n/1e3)
    return str(n)

def fc(v):
    if v >= 1: return '${:.2f}'.format(v)
    if v >= .01: return '${:.3f}'.format(v)
    return '${:.4f}'.format(v)

def bar(pct, w):
    p = min(max(int(pct), 0), 100)
    f = int(p / 100 * w)
    e = w - f
    return B + FILL * f + R + D + EMPT * e + R + ' ' + B + str(p) + '%' + R

tag = 'Opus' if 'opus' in model else 'Sonnet' if 'sonnet' in model else 'Haiku' if 'haiku' in model else model[:8]

# ── Limit reset ─────────────────────────────────────────────────────────────
now = datetime.now()
bnd = [0, 5, 10, 15, 20]
nb = None
for b in bnd:
    if b > now.hour:
        nb = b; break
if nb is None:
    nr = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    ph = 20
else:
    nr = now.replace(hour=nb, minute=0, second=0, microsecond=0)
    ph = bnd[bnd.index(nb) - 1] if bnd.index(nb) > 0 else 0
pr_ = now.replace(hour=ph, minute=0, second=0, microsecond=0)
if pr_ > now: pr_ -= timedelta(days=1)
wt = (nr - pr_).total_seconds()
rp = min((now - pr_).total_seconds() / wt * 100, 100) if wt > 0 else 0
rm = nr - now
h_left = int(rm.total_seconds() // 3600)
m_left = int(rm.total_seconds() % 3600 // 60)
rs = '{}h{:02d}m'.format(h_left, m_left)
# Reset time display: show next boundary time
nr_str = nr.strftime('%H:%M')

# ── All-time cost + model dist (cached 60s) ─────────────────────────────────
cf = '/tmp/claude_sl_cost.txt'
tf = '/tmp/claude_sl_ts.txt'
mf = '/tmp/claude_sl_mdist.json'
nt = int(time.time())
try: lt = int(open(tf).read().strip())
except: lt = 0

if nt - lt > 60:
    px = {
        'opus':  {'i':15,'o':75,'cw':18.75,'cr':1.5},
        'sonnet':{'i':3, 'o':15,'cw':3.75, 'cr':.3},
        'haiku': {'i':.8,'o':4, 'cw':1,    'cr':.08},
    }
    def gp(m):
        for k,v in px.items():
            if k in (m or ''): return v
        return px['sonnet']
    tot = 0.0; md = {}
    for fp in (glob.glob(os.path.expanduser('~/.claude/projects/*/*.jsonl')) +
               glob.glob(os.path.expanduser('~/.claude/projects/*/*/subagents/*.jsonl'))):
        try:
            for ln in open(fp):
                try:
                    r = json.loads(ln)
                    if r.get('type') != 'assistant': continue
                    mg = r.get('message',{}); u = mg.get('usage')
                    if not u: continue
                    mo = mg.get('model','')
                    if mo == '<synthetic>': continue
                    p = gp(mo)
                    tot += (u.get('input_tokens',0)/1e6)*p['i'] + (u.get('output_tokens',0)/1e6)*p['o'] \
                         + (u.get('cache_creation_input_tokens',0)/1e6)*p['cw'] + (u.get('cache_read_input_tokens',0)/1e6)*p['cr']
                    tk = u.get('input_tokens',0)+u.get('output_tokens',0)+u.get('cache_creation_input_tokens',0)+u.get('cache_read_input_tokens',0)
                    sn = 'O' if 'opus' in mo else 'S' if 'sonnet' in mo else 'H' if 'haiku' in mo else '?'
                    md[sn] = md.get(sn,0) + tk
                except: pass
        except: pass
    open(cf,'w').write('{:.4f}'.format(tot))
    open(tf,'w').write(str(nt))
    json.dump(md, open(mf,'w'))

try: at = float(open(cf).read().strip())
except: at = 0.0
try: md = json.load(open(mf))
except: md = {}

# ── Model dist (wider bars: 8 wide) ────────────────────────────────────────
gt = sum(md.values()) or 1
names = {'O':'Opus','S':'Sonnet','H':'Haiku'}
mp = []
for k in sorted(md, key=lambda x: -md[x]):
    p = md[k]/gt*100
    mp.append(B + names.get(k,k) + R + ' ' + bar(p, 8) + ' ' + B + ft(md[k]) + R)

# ── Output ──────────────────────────────────────────────────────────────────
# Line 1: header
print(D + '\u25c7 ' + R + B + tag + R
      + SEP + D + 'Session ' + R + B + fc(sc) + R
      + '  ' + D + 'Total ' + R + B + fc(at) + R)

# Spacer
print('')

# Line 2: limit (moved up)
print(D + 'Limit   ' + R + bar(rp, 10)
      + ' ' + D + 'Reset ' + R + B + rs + R
      + D + ' -> ' + R + B + nr_str + R)

# Spacer
print('')

# Line 3: context (moved down)
print(D + 'Context ' + R + bar(cp, 10)
      + ' ' + B + ft(ti+to) + R + D + '/' + R + B + ft(cs) + R
      + SEP + D + 'I ' + R + B + ft(ti) + R
      + ' ' + D + 'O ' + R + B + ft(to) + R
      + ' ' + D + 'CR ' + R + B + ft(cr) + R
      + ' ' + D + 'CW ' + R + B + ft(cw) + R)

# Spacer
print('')

# Line 4: models
print(D + 'Models  ' + R + '  '.join(mp))
PYEOF
