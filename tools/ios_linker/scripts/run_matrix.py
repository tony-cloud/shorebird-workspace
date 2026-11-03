#!/usr/bin/env python3
"""Controlled release/patch experiments for the iOS native-reuse linker.

Reproduces the graph of docs/AOT_TOOLS_EXPLORATION.md section 3 (80 or 320
non-inlined callers of a model method that calls a shared helper) with our own
gen_snapshot link layout support and tools/ios_linker. For every case:

  release:  gen_snapshot --print_link_info_to (optionally with DD slots)
  patch:    pass 1 --link_base=release; pool-hints; pass 2 --link_base=hints
  link:     ios_linker link -> out.vmcode, report.json, link_table.tsv
  check:    ios_linker check (independent byte/branch verification)
  download: updater `patch` (bidiff + zstd) of out.vmcode against the
            release data+text blob

With --release-format macho the release is built the way flutter_tools
builds an iOS release (assembly snapshot, assembled and linked into
App.framework/App with the iPhoneOS SDK, dSYM extracted, strip -x, then
ad-hoc signed) and the linker reads the release text from that Mach-O. An
ELF from the same compiler flags is built alongside to prove that the code
bytes and the sidecar are identical in both forms.

usage: run_matrix.py [--wide] [--dd] [--release-format elf|macho]
                     [--min-link-percent P] [--work DIR] [CASE...]
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

WS = Path('/Volumes/TonyCL6APFS/git/shorebird-workspace')
ENGINE = WS / '.agents/builds/engine-ios-closed-world-20261104/out'
GEN = Path(os.environ.get('GEN_SNAPSHOT',
                          WS / '.agents/wt/linker/dart-sdk/xcodebuild/ProductARM64/gen_snapshot'))
HOST_SDK = ENGINE / 'host_release_arm64/dart-sdk'
DART = Path(os.environ.get('DART', HOST_SDK / 'bin/dart'))
PLATFORM = ENGINE / 'ios_release/flutter_patched_sdk/platform_strong.dill'
PATCH_TOOL = Path(os.environ.get('PATCH_TOOL', WS / 'updater/target/release/patch'))
TOOL = WS / 'tools/ios_linker/bin/ios_linker.dart'

BASE_SOURCE = '''import 'dart:ui' as ui;

const label = 'Shorebird baseline';
@pragma('vm:never-inline')
int sharedLeaf(int x) => (x * 3 + 1) & 65535;

class Model {
  final int seed;
  Model(this.seed);
  @pragma('vm:never-inline')
  int evaluate(int x) => sharedLeaf(x + seed);
}

// Each caller remains independently observable in the linker graph.
CALLERS

@pragma('vm:never-inline')
int compute(int x) {
  final model = Model(x);
  return SUM;
}

void draw() {
  final view = ui.PlatformDispatcher.instance.views.first;
  final value = compute(DateTime.now().millisecondsSinceEpoch & 65535);
  final text = (ui.ParagraphBuilder(ui.ParagraphStyle(fontSize: 28))
    ..addText('$label: $value')).build()
    ..layout(ui.ParagraphConstraints(width: view.physicalSize.width));
  final recorder = ui.PictureRecorder();
  final canvas = ui.Canvas(recorder);
  canvas.drawColor(const ui.Color(0xFFFFFFFF), ui.BlendMode.src);
  canvas.drawParagraph(text, const ui.Offset(16, 64));
  final scene = (ui.SceneBuilder()..addPicture(ui.Offset.zero, recorder.endRecording())).build();
  view.render(scene);
  scene.dispose();
}

void main() {
  ui.PlatformDispatcher.instance.onDrawFrame = draw;
  ui.PlatformDispatcher.instance.scheduleFrame();
}
'''


def base_source(fanout):
    callers = '\n'.join(
        f"@pragma('vm:never-inline')\nint caller{i}(Model m, int x) => m.evaluate(x + {i});"
        for i in range(fanout))
    total = ' + '.join(f'caller{i}(model, x)' for i in range(fanout))
    return BASE_SOURCE.replace('CALLERS', callers).replace('SUM', total)


def cases(fanout):
    b = base_source(fanout)
    return {
        'p0_identical': b,
        'p1_string': b.replace('Shorebird baseline', 'Shorebird patched!'),
        'p2_one_caller': b.replace('int caller17(Model m, int x) => m.evaluate(x + 17);',
                                   'int caller17(Model m, int x) => m.evaluate(x + 117);'),
        'p3_shared_leaf': b.replace('(x * 3 + 1)', '(x * 7 + 2)'),
        'p4_field_layout': b.replace('final int seed;\n  Model(this.seed);',
                                     'final int seed;\n  final int offset;\n  Model(this.seed) : offset = DateTime.now().second;')
                            .replace('sharedLeaf(x + seed)', 'sharedLeaf(x + seed + offset)'),
        'p5_new_function': b.replace('const label =',
                                     "@pragma('vm:never-inline')\nint newFeature(int x) => x ^ 12345;\n\nconst label =")
                            .replace('final value = compute(', 'final value = newFeature(9) + compute('),
        'p6_remove_caller': b.replace('caller17(model, x) + ', ''),
    }


def run(cmd, log, env=None):
    cmd = [str(c) for c in cmd]
    start = time.monotonic()
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    elapsed = time.monotonic() - start
    with open(log, 'ab') as f:
        f.write(('$ ' + ' '.join(cmd) + f'\n# exit={r.returncode} {elapsed:.2f}s\n').encode())
        f.write(r.stdout)
    if r.returncode:
        sys.stdout.write(r.stdout.decode(errors='replace')[-4000:])
        raise SystemExit(f'command failed ({r.returncode}): {" ".join(cmd[:3])} ...')
    return r.stdout.decode(errors='replace')


def kernel(source, fixture_dir, out_dill, log):
    # One canonical source URI for every variant: identities include it.
    app = fixture_dir / 'app.dart'
    app.write_text(source)
    run([HOST_SDK / 'bin/dartaotruntime', HOST_SDK / 'bin/snapshots/gen_kernel_aot.dart.snapshot',
         '--aot', '--target=flutter', '--target-os=ios', f'--platform={PLATFORM}',
         '-Ddart.vm.product=true', f'--output={out_dill}', app], log)


def gen(args, log):
    return run([GEN, '--deterministic', '--snapshot_kind=app-aot-elf', '--strip'] + args, log)


def tool(args, log):
    return run([DART, TOOL] + args, log)


# flutter_tools (base/build.dart) iOS: --deterministic, assembly, no --strip
# (the framework binary is stripped after dSYM extraction).
IOS_MIN_VERSION = '15.0'  # FlutterDarwinPlatform.ios.deploymentTarget()


def build_release(rel, dill, flags, release_format, log):
    """Builds the release; returns (release binary, release sidecar, proofs)."""
    if release_format == 'elf':
        gen([f'--elf={rel}/App.aot', f'--print_link_info_to={rel}/App.link'] + flags + [dill], log)
        stock = rel / 'App.stock.aot'
        proofs = {'elf_identical_to_stock': stock.exists() and
                  stock.read_bytes() == (rel / 'App.aot').read_bytes()}
        return rel / 'App.aot', rel / 'App.link', proofs
    proofs = {}
    run([GEN, '--deterministic', '--snapshot_kind=app-aot-assembly', f'--assembly={rel}/App.stock.S']
        + flags + [dill], log)
    run([GEN, '--deterministic', '--snapshot_kind=app-aot-assembly', f'--assembly={rel}/App.S',
         f'--print_link_info_to={rel}/App.link'] + flags + [dill], log)
    proofs['assembly_identical_to_stock'] = (rel / 'App.S').read_bytes() == (rel / 'App.stock.S').read_bytes()
    # Same flags as ELF (for the equality proof only).
    run([GEN, '--deterministic', '--snapshot_kind=app-aot-elf', f'--elf={rel}/App.aot',
         f'--print_link_info_to={rel}/App.elf.link'] + flags + [dill], log)
    proofs['sidecar_identical_elf_vs_assembly'] = (rel / 'App.link').read_bytes() == (rel / 'App.elf.link').read_bytes()
    sdk = run(['xcrun', '--sdk', 'iphoneos', '--show-sdk-path'], log).strip()
    common = ['-arch', 'arm64', f'-miphoneos-version-min={IOS_MIN_VERSION}', '-isysroot', sdk]
    run(['xcrun', 'cc'] + common + ['-c', rel / 'App.S', '-o', rel / 'App.o'], log)
    fw = rel / 'App.framework'
    if fw.exists():
        shutil.rmtree(fw)
    fw.mkdir()
    run(['xcrun', 'clang'] + common + ['-dynamiclib', '-Xlinker', '-rpath', '-Xlinker',
         '@executable_path/Frameworks', '-Xlinker', '-rpath', '-Xlinker', '@loader_path/Frameworks',
         '-fapplication-extension', '-install_name', '@rpath/App.framework/App', '-o', fw / 'App',
         rel / 'App.o'], log)
    run(['xcrun', 'dsymutil', '-o', rel / 'App.framework.dSYM', fw / 'App'], log)
    run(['xcrun', 'strip', '-x', fw / 'App', '-o', fw / 'App'], log)
    shutil.copy(fw / 'App', rel / 'App.unsigned')
    # Ad-hoc signature: like distribution signing it only adds LC_CODE_SIGNATURE
    # data in __LINKEDIT; __TEXT is unchanged (checked below).
    run(['codesign', '-f', '-s', '-', fw / 'App'], log)
    out = run([DART, TOOL, 'compare-text', '--a', fw / 'App', '--b', rel / 'App.aot',
               '--link', rel / 'App.link'], log)
    proofs['macho_vs_elf'] = out.strip().splitlines()
    out = run([DART, TOOL, 'compare-text', '--a', fw / 'App', '--b', rel / 'App.unsigned',
               '--link', rel / 'App.link'], log)
    proofs['signed_vs_unsigned'] = out.strip().splitlines()
    return fw / 'App', rel / 'App.link', proofs


def patch_case(out, rel, base, base_link, flags, log, min_link_percent=None):
    """Two-pass patch build against base_link, link, check, download."""
    gen([f'--elf={out}/out.pass1.aot', f'--link_base={base_link}',
         f'--print_link_info_to={out}/out.pass1.link'] + flags + [out / 'app.dill'], log)
    hints = tool(['pool-hints', '--base-link', base_link, '--patch-link',
                  out / 'out.pass1.link', '--out', out / 'constraints.link'], log)
    (out / 'pool_hints.txt').write_text(hints)
    gen([f'--elf={out}/out.aot', f'--link_base={out}/constraints.link',
         f'--print_link_info_to={out}/out.link'] + flags + [out / 'app.dill'], log)
    gate = [] if min_link_percent is None else ['--min-link-percent', str(min_link_percent)]
    tool(['link', '--base', base, '--base-link', base_link,
          '--patch-elf', out / 'out.aot', '--patch-link', out / 'out.link',
          '--out', out / 'out.vmcode', '--report', out / 'report.json',
          '--table', out / 'link_table.tsv'] + gate, log)
    check = tool(['check', '--vmcode', out / 'out.vmcode', '--base', base,
                  '--patch-elf', out / 'out.aot', '--base-link', base_link,
                  '--patch-link', out / 'out.link'] + gate, log)
    (out / 'check.txt').write_text(check)
    run([PATCH_TOOL, rel / 'App.diffbase', out / 'out.vmcode', out / 'out.download'], log)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wide', action='store_true', help='320 callers instead of 80')
    ap.add_argument('--dd', action='store_true', help='release with DD slots')
    ap.add_argument('--dd-max-bytes', type=int, default=10000)
    ap.add_argument('--release-format', choices=['elf', 'macho'], default='elf')
    ap.add_argument('--min-link-percent', type=float)
    ap.add_argument('--work', default=str(WS / '.agents/ios-linker-20261104/matrix'))
    ap.add_argument('cases', nargs='*')
    a = ap.parse_args()
    fanout = 320 if a.wide else 80
    profile = (('dd' if a.dd else 'direct') + ('_wide' if a.wide else '')
               + ('_macho' if a.release_format == 'macho' else ''))
    work = Path(a.work) / profile
    fixture = Path(a.work) / 'fixture'
    work.mkdir(parents=True, exist_ok=True)
    fixture.mkdir(parents=True, exist_ok=True)
    all_cases = cases(fanout)
    selected = a.cases or list(all_cases)

    rel = work / 'release'
    rel.mkdir(exist_ok=True)
    log = rel / 'log.txt'
    log.write_text('')
    kernel(base_source(fanout), fixture, rel / 'app.dill', log)
    gen([f'--elf={rel}/App.stock.aot', rel / 'app.dill'], log)
    base_flags = []
    if a.dd:
        # Pass 1: analysis build; select DD targets from the call graph.
        gen([f'--elf={rel}/App.analysis.aot', f'--print_link_info_to={rel}/App.analysis.link',
             rel / 'app.dill'], log)
        tool(['dd-select', '--base', rel / 'App.analysis.aot', '--link', rel / 'App.analysis.link',
              '--max-bytes', str(a.dd_max_bytes), '--out', rel / 'App.dd'], log)
        base_flags = [f'--link_dd_targets={rel}/App.dd']
    base, base_link, proofs = build_release(rel, rel / 'app.dill', base_flags, a.release_format, log)
    tool(['diff-base', '--base', base, '--link', base_link, '--out', rel / 'App.diffbase'], log)

    results = {}
    for name in selected:
        out = work / name
        out.mkdir(exist_ok=True)
        log = out / 'log.txt'
        log.write_text('')
        (out / 'source.dart').write_text(all_cases[name])
        kernel(all_cases[name], fixture, out / 'app.dill', log)
        patch_case(out, rel, base, base_link, base_flags, log, a.min_link_percent)
        report = json.loads((out / 'report.json').read_text())
        hints = (out / 'pool_hints.txt').read_text()
        check = (out / 'check.txt').read_text()
        row = {
            'link_percent': report['link_percent'],
            'unlinked_entries': report['unlinked_entries'],
            'unlinked_code_bytes': report['unlinked_code_bytes'],
            'patch_code_bytes': report['patch_code_bytes'],
            'download_bytes': (out / 'out.download').stat().st_size,
            'vmcode_bytes': (out / 'out.vmcode').stat().st_size,
            'patch_elf_bytes': (out / 'out.aot').stat().st_size,
            'check': check.strip().splitlines()[0],
            'pool_hints': hints.strip(),
            'unlinked_by_reason': report['unlinked_by_reason'],
        }
        results[name] = row
        print(f"{profile} {name}: {row['link_percent']:.5f}% unlinked={row['unlinked_entries']} "
              f"({row['unlinked_code_bytes']} B) download={row['download_bytes']} "
              f"vmcode={row['vmcode_bytes']} | {row['check']}", flush=True)

    summary = {
        'profile': profile,
        'fanout': fanout,
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
