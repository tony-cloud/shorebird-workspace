#!/usr/bin/env python3
"""Negative controls for the link gates and the sidecar format/identity.

Uses the outputs of `run_matrix.py --release-format macho` (a Mach-O release
and its p3_shared_leaf patch, linked below 100%) and, for the cross-target
identity check, a release sidecar written by a different gen_snapshot
configuration (e.g. the compressed-pointer build).

usage: run_negative_tests.py --profile DIR --other-link FILE --gen GEN_SNAPSHOT
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_matrix as m  # noqa: E402


def call(cmd):
    r = subprocess.run([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # gen_snapshot FATAL aborts: SIGABRT is -6 here, 134 in a shell.
    rc = 134 if r.returncode == -6 else r.returncode
    return rc, r.stdout.decode(errors='replace')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--profile', required=True, help='run_matrix.py macho profile dir')
    ap.add_argument('--other-link', required=True, help='sidecar from another gen_snapshot config')
    ap.add_argument('--gen', default=str(m.GEN))
    ap.add_argument('--case', default='p3_shared_leaf')
    a = ap.parse_args()
    prof = Path(a.profile)
    rel, pat = prof / 'release', prof / a.case
    out = prof / 'negative'
    out.mkdir(exist_ok=True)
    base = rel / 'App.framework/App'
    tool = [m.DART, m.TOOL]
    link_args = ['--base', base, '--base-link', rel / 'App.link', '--patch-elf', pat / 'out.aot',
                 '--patch-link', pat / 'out.link']
    percent = json.loads((pat / 'report.json').read_text())['link_percent']
    above, below = percent + 0.5, percent - 0.5
    results = []

    def expect(name, want, got, extra=''):
        ok = got == want
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'} {name}: exit {got} (want {want}) {extra}".rstrip())

    for f in out.glob('*.vmcode'):
        f.unlink()
    rc, _ = call(tool + ['link'] + link_args + ['--out', out / 'hi.vmcode', '--min-link-percent', above])
    expect(f'link --min-link-percent {above:.3f} (actual {percent:.3f})', 3, rc,
           f"vmcode written: {(out / 'hi.vmcode').exists()}")
    results.append(not (out / 'hi.vmcode').exists())
    rc, _ = call(tool + ['link'] + link_args + ['--out', out / 'lo.vmcode', '--min-link-percent', below])
    expect(f'link --min-link-percent {below:.3f}', 0, rc)
    same = (out / 'lo.vmcode').read_bytes() == (pat / 'out.vmcode').read_bytes()
    results.append(same)
    print(f"{'PASS' if same else 'FAIL'} gated vmcode identical to ungated vmcode")
    check = ['check', '--vmcode', out / 'lo.vmcode', '--base', base, '--patch-elf', pat / 'out.aot',
             '--base-link', rel / 'App.link', '--patch-link', pat / 'out.link']
    rc, _ = call(tool + check + ['--min-link-percent', above])
    expect(f'check --min-link-percent {above:.3f}', 3, rc)
    rc, _ = call(tool + check + ['--min-link-percent', below])
    expect(f'check --min-link-percent {below:.3f}', 0, rc)

    # Runtime gate budget: refuse exactly when the runtime would.
    need = json.loads((pat / 'report.json').read_text())['gates_needed']
    rc, o = call(tool + ['link'] + link_args + ['--out', out / 'g.vmcode', '--gate-count', need - 1])
    expect(f'link --gate-count {need - 1} (needs {need})', 5, rc,
           f"vmcode written: {(out / 'g.vmcode').exists()}")
    results.append(not (out / 'g.vmcode').exists())
    rc, _ = call(tool + ['link'] + link_args + ['--out', out / 'g.vmcode', '--gate-count', need])
    expect(f'link --gate-count {need}', 0, rc)
    rc, _ = call(tool + check + ['--gate-count', need - 1])
    expect(f'check --gate-count {need - 1}', 5, rc)

    # Unstripped Mach-O (gen_snapshot app-aot-macho-dylib keeps local code
    # symbols): the release text is bounded by the sidecar size.
    dylib = out / 'App.dylib'
    rc, o = call([a.gen, '--deterministic', '--snapshot_kind=app-aot-macho-dylib', '--strip',
                  f'--macho={dylib}', f'--print_link_info_to={out}/dylib.link', rel / 'app.dill'])
    expect('gen_snapshot app-aot-macho-dylib', 0, rc)
    rc, o = call(tool + ['compare-text', '--a', dylib, '--b', rel / 'App.aot', '--link', rel / 'App.link'])
    expect('unstripped dylib text vs ELF, all code entries equal', 0, rc,
           next((l.strip() for l in o.splitlines() if l.startswith('code entries')), o[-200:]))

    tampered = bytearray((out / 'lo.vmcode').read_bytes())
    tampered[120] ^= 0x10  # linked code bytes
    (out / 'tampered.vmcode').write_bytes(bytes(tampered))
    rc, o = call(tool + ['check', '--vmcode', out / 'tampered.vmcode'] + check[3:])
    expect('check with tampered header linked bytes', 1, rc,
           next((l.strip() for l in o.splitlines() if 'FAIL' in l), ''))
    rc, o = call(tool + ['check', '--vmcode', out / 'lo.vmcode', '--base', rel / 'App.aot',
                         '--patch-elf', pat / 'out.aot'])
    expect('check against the ELF instead of the shipped Mach-O', 1, rc,
           next((l.strip() for l in o.splitlines() if 'FAIL' in l), ''))

    v1 = out / 'v1.link'
    lines = (rel / 'App.link').read_text().split('\n')
    v1.write_text('\n'.join(['#osb-link-info 1'] + lines[1:]))
    rc, o = call(tool + ['link', '--base', base, '--base-link', v1, '--patch-elf', pat / 'out.aot',
                         '--patch-link', pat / 'out.link', '--out', out / 'x.vmcode'])
    expect('link with a format-1 sidecar', 4, rc, o.strip().splitlines()[-1][:120])
    rc, o = call(tool + ['link', '--base', base, '--base-link', a.other_link, '--patch-elf',
                         pat / 'out.aot', '--patch-link', pat / 'out.link', '--out', out / 'x.vmcode'])
    expect('link with a release sidecar from another gen_snapshot', 4, rc,
           o.strip().splitlines()[-1][:160])
    gen = [a.gen, '--deterministic', '--snapshot_kind=app-aot-elf', '--strip', f'--elf={out}/x.aot']
    rc, o = call(gen + [f'--link_base={v1}', pat / 'app.dill'])
    expect('gen_snapshot --link_base format-1 sidecar', 134, rc,
           next((l[l.find('error'):][:140] for l in o.splitlines() if 'error' in l), ''))
    rc, o = call(gen + [f'--link_base={a.other_link}', pat / 'app.dill'])
    expect('gen_snapshot --link_base sidecar from another config', 134, rc,
           next((l[l.find('error'):][:200] for l in o.splitlines() if 'error' in l), ''))
    print(f'negative tests: {sum(results)}/{len(results)} passed')
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
