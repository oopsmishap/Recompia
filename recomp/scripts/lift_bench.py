# SPDX-License-Identifier: GPL-3.0-or-later
"""Measure one lift: its wall time, and the C++ it wrote.

    python recomp/scripts/lift_bench.py --exefs H:/path/exefs --out work/metrics/base [--module main] [--runs 3]

The lift runs the same command the game build does (`nxrecomp lift` with the game's shard count and site list), so the
time is what a player's setup pays. The machine is otherwise idle and the job count is fixed, so wall time tracks the
work the lift does (the Win32 process-time API reports nothing useful for a spawned interpreter here).

The emitted-code counts are the ones the lifter's quality is judged by, and they are exact: the same tree measured twice
gives the same numbers. `c->x[]` and `c->v[]` are the guest context's register traffic, `flag ops` the flag helpers, and
`temps` the block-scoped temporaries the emitter introduces.
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RECOMP = os.path.join(ROOT, 'recomp')
PY = sys.executable

MODULES = {  # name: (shards, base, index), as games/pkp/config.py lists them
    'main': (230, 0x7100000000, 0),
    'sdk': (45, 0x7500000000, 1),
    'subsdk0': (4, 0x7600000000, 2),
    'rtld': (1, 0x7700000000, 3),
}

PATTERNS = {
    'ctx_x': b'c->x[',
    'ctx_v': b'c->v[',
    'flags': b'set_nzcv(',
    'flag_add': b'adds32(',
    'flag_logic': b'logic32(',
    'cond': b'cond(',
    'calls': b'(c);',
    'indirect': b'call_indirect(',
    'mem': b'rd64(',
    'traps': b'guest_trap(',
    'temps': b'uint64_t t',
}


def measure_file(path):
    data = open(path, 'rb').read()
    out = {'bytes': len(data), 'lines': data.count(b'\n'),
           'fns': data.count(b'\nvoid f_') + (1 if data.startswith(b'void f_') else 0)}
    for k, pat in PATTERNS.items():
        out[k] = data.count(pat)
    return out


def measure_dir(path):
    total = {'files': 0}
    for n in sorted(x for x in os.listdir(path) if x.endswith('.cpp')):
        total['files'] += 1
        for k, v in measure_file(os.path.join(path, n)).items():
            total[k] = total.get(k, 0) + v
    return total


def lift_once(module, exefs, out, jobs):
    shards, base, index = MODULES[module]
    sites = os.path.join(RECOMP, 'games', 'pkp', '%s.sites' % module)
    cmd = [PY, '-B', '-m', 'nxrecomp', 'lift', os.path.join(exefs, module), out, '--name', module,
           '--base', hex(base), '--shards', str(shards), '--jobs', str(jobs), '--game', 'pkp',
           '--index', str(index), '--locals', 'lean']
    if os.path.exists(sites):
        cmd += ['--sites', sites]
    env = dict(os.environ, PYTHONPATH=RECOMP)
    t0 = time.time()
    p = subprocess.run(cmd, cwd=RECOMP, env=env, capture_output=True)
    wall = time.time() - t0
    text = (p.stdout + p.stderr).decode('utf-8', 'replace')
    if p.returncode:
        sys.stderr.write(text[-4000:])
        raise SystemExit('lift failed (%d)' % p.returncode)
    insns = 0
    for line in text.splitlines():
        if ' instructions ' in line:
            insns = int(line.split('instructions')[1].split()[0])
    return {'module': module, 'wall': wall, 'instructions': insns}


def report(res, emitted):
    insns = res['instructions'] or 1
    m = emitted
    print('\n%s: %.1fs for %d instructions' % (res['module'], res['wall'], res['instructions']))
    print('emitted: %d files, %.1f MB, %d lines, %d functions' %
          (m['files'], m['bytes'] / 1048576.0, m['lines'], m['fns']))
    print('per instruction: %.2f bytes, %.3f lines, %.3f c->x[], %.3f flag ops, %.3f calls, %.3f mem ops, %.3f temps' %
          (m['bytes'] / insns, m['lines'] / insns, m['ctx_x'] / insns,
           (m['flags'] + m['flag_add'] + m['flag_logic'] + m['cond']) / insns,
           (m['calls'] + m['indirect']) / insns, m['mem'] / insns, m['temps'] / insns))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exefs', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--module', default='main', choices=sorted(MODULES))
    ap.add_argument('--runs', type=int, default=1)
    ap.add_argument('--jobs', type=int, default=23)
    ap.add_argument('--json')
    a = ap.parse_args()

    out_dir = os.path.abspath(a.out)
    runs = []
    for k in range(a.runs):
        r = lift_once(a.module, a.exefs, out_dir, a.jobs)
        runs.append(r)
        print('run %d: wall %.1fs  %d instructions' % (k + 1, r['wall'], r['instructions']), flush=True)
    med = sorted(r['wall'] for r in runs)[len(runs) // 2]
    emitted = measure_dir(out_dir)
    result = {'module': a.module, 'runs': runs, 'median_wall': med, 'emitted': emitted}
    report({'module': a.module, 'wall': med, 'instructions': runs[0]['instructions']}, emitted)
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=1)
        print('wrote %s' % a.json)


if __name__ == '__main__':
    main()
