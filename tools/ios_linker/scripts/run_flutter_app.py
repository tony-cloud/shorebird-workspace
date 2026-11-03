#!/usr/bin/env python3
"""Real Flutter app experiment: testapps/patch_perf_bench as a normal TFA kernel.

Kernels come from the engine's frontend_server_aot with the same flags as a
release build (--aot --tfa, product, iOS). Each patch case edits the app source
in a private copy (same file URIs as the release), then follows the same
release/patch/link/check/download pipeline as run_matrix.py.

usage: run_flutter_app.py [--dd] [--dd-max-bytes N] [--release-format elf|macho]
                          [--min-link-percent P] [--work DIR] [CASE...]
"""
import argparse
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_matrix as m  # noqa: E402

APP_SRC = m.WS / '.agents/size-analysis-20261104/app'  # has a resolved package_config

CASES = {
    'f0_identical': [],
    'f1_kernel_constant': [('lib/kernel.dart', 'return 1; // KERNEL_TAG', 'return 2; // KERNEL_TAG')],
    'f2_ui_string': [('lib/main.dart', "'Item $index  w=$work'", "'Row $index  w=$work'")],
    'f3_virtual_method': [('lib/kernel.dart', 'int area(int k) => k * k + 1;', 'int area(int k) => k * k + 2;')],
    'f4_add_field': [('lib/kernel.dart', 'final Chip chip = Chip();\n}',
                      'final Chip chip = Chip();\n  int bonus = DateTime.now().second;\n}'),
                     ('lib/kernel.dart', 'return 1; // KERNEL_TAG', 'return 1 + w.bonus; // KERNEL_TAG')],
    'f5_widget_padding': [('lib/main.dart', 'padding: const EdgeInsets.all(8),',
                           'padding: const EdgeInsets.all(10),')],
}


def frontend(app, out_dill, log):
    h = m.HOST_SDK
    m.run([h / 'bin/dartaotruntime', h / 'bin/snapshots/frontend_server_aot.dart.snapshot',
           '--sdk-root', m.ENGINE / 'ios_release/flutter_patched_sdk/', '--target=flutter',
           '--no-print-incremental-dependencies', '-DBENCH_VARIANT=V0', '-Ddart.vm.profile=false',
           '-Ddart.vm.product=true', '--delete-tostring-package-uri=dart:ui',
           '--delete-tostring-package-uri=package:flutter', '--aot', '--tfa', '--target-os', 'ios',
           '--packages', app / '.dart_tool/package_config.json', '--output-dill', out_dill,
           '--verbosity=error', 'package:patch_perf_bench/main.dart'], log)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dd', action='store_true')
    ap.add_argument('--dd-max-bytes', type=int, default=10000)
    ap.add_argument('--release-format', choices=['elf', 'macho'], default='elf')
    ap.add_argument('--min-link-percent', type=float)
    ap.add_argument('--work', default=str(m.WS / '.agents/ios-linker-20261104/flutter_app'))
    ap.add_argument('cases', nargs='*')
    a = ap.parse_args()
    work = Path(a.work) / (('dd' if a.dd else 'direct')
                           + ('_macho' if a.release_format == 'macho' else ''))
    app = Path(a.work) / 'app'
    if not app.exists():
        shutil.copytree(APP_SRC, app, symlinks=True)
    pristine = {p: (app / p).read_text() for p in ('lib/kernel.dart', 'lib/main.dart')}
    work.mkdir(parents=True, exist_ok=True)

    rel = work / 'release'
    rel.mkdir(exist_ok=True)
    log = rel / 'log.txt'
    log.write_text('')
    frontend(app, rel / 'app.dill', log)
    m.gen([f'--elf={rel}/App.stock.aot', rel / 'app.dill'], log)
    flags = []
    if a.dd:
        m.gen([f'--elf={rel}/App.analysis.aot', f'--print_link_info_to={rel}/App.analysis.link',
               rel / 'app.dill'], log)
        print(m.tool(['dd-select', '--base', rel / 'App.analysis.aot', '--link', rel / 'App.analysis.link',
                      '--max-bytes', str(a.dd_max_bytes), '--out', rel / 'App.dd'], log).splitlines()[0])
        flags = [f'--link_dd_targets={rel}/App.dd']
    base, base_link, proofs = m.build_release(rel, rel / 'app.dill', flags, a.release_format, log)
    m.tool(['diff-base', '--base', base, '--link', base_link, '--out', rel / 'App.diffbase'], log)

    results = {}
    for name in a.cases or list(CASES):
        out = work / name
        out.mkdir(exist_ok=True)
        log = out / 'log.txt'
        log.write_text('')
        try:
            for path, old, new in CASES[name]:
                text = (app / path).read_text()
                assert old in text, (name, old)
                (app / path).write_text(text.replace(old, new, 1))
            frontend(app, out / 'app.dill', log)
        finally:
            for p, t in pristine.items():
                (app / p).write_text(t)
        m.patch_case(out, rel, base, base_link, flags, log, a.min_link_percent)
        hints = (out / 'pool_hints.txt').read_text()
        check = (out / 'check.txt').read_text()
        r = json.loads((out / 'report.json').read_text())
        row = {k: r[k] for k in ('link_percent', 'unlinked_entries', 'unlinked_code_bytes',
                                 'patch_code_bytes', 'unlinked_by_reason')}
        row.update(download_bytes=(out / 'out.download').stat().st_size,
                   vmcode_bytes=(out / 'out.vmcode').stat().st_size,
                   check=check.strip().splitlines()[0], pool_hints=hints.strip(),
                   top_unlinked=r['unlinked'][:8])
        results[name] = row
        print(f"{work.name} {name}: {row['link_percent']:.4f}% unlinked={row['unlinked_entries']} "
              f"({row['unlinked_code_bytes']} B) download={row['download_bytes']} "
              f"vmcode={row['vmcode_bytes']} | {row['check']}", flush=True)
    summary = {
        'profile': work.name,
        'release_stock_elf': (rel / 'App.stock.aot').stat().st_size,
        'release_format': a.release_format,
        'release_binary': base.stat().st_size,
        'release_proofs': proofs,
        'release_diff_base': (rel / 'App.diffbase').stat().st_size,
        'release_link_info': base_link.stat().st_size,
        'release_code_bytes': sum(int(l.split('\t')[3]) for l in base_link.read_text().splitlines()
                                  if l.startswith('code\t')),
        'cases': results,
    }
    (work / 'summary.json').write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != 'cases'}))


if __name__ == '__main__':
    main()
