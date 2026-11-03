#!/usr/bin/env python3
"""Runtime check that layout-constrained snapshots are correct programs.

For each (release, patch) pair of pure-Dart programs: build the release with
link info, build the patch against it (two passes, as for a real patch), and
run both the constrained patch snapshot and a stock snapshot of the patch
source with dartaotruntime. Their outputs must be identical. This validates
the compiler side (class id holes, field offsets, static field ids, pool
placement, selector offsets) independently of the native-reuse runtime.

usage: run_runtime_test.py [--work DIR] RELEASE.dart PATCH.dart [...pairs]
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

WS = Path('/Volumes/TonyCL6APFS/git/shorebird-workspace')
# DART_OUT selects the build (gen_snapshot + dartaotruntime), e.g.
# xcodebuild/ProductARM64C for compressed pointers.
OUT = Path(os.environ.get('DART_OUT', WS / '.agents/wt/linker/dart-sdk/xcodebuild/ProductARM64'))
VM_PLATFORM = WS / '.agents/wt/linker/dart-sdk/xcodebuild/ProductARM64/vm_platform.dill'
HOST_SDK = WS / '.agents/builds/engine-ios-closed-world-20261104/out/host_release_arm64/dart-sdk'
TOOL = WS / 'tools/ios_linker/bin/ios_linker.dart'


def run(cmd, capture=True):
    r = subprocess.run([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if r.returncode:
        sys.stdout.write(r.stdout.decode(errors='replace')[-3000:])
        raise SystemExit(f'failed ({r.returncode}): {cmd[0]}')
    return r.stdout.decode(errors='replace')


def kernel(src, dill, fixture):
    fixture.write_text(Path(src).read_text())
    run([HOST_SDK / 'bin/dartaotruntime', HOST_SDK / 'bin/snapshots/gen_kernel_aot.dart.snapshot',
         '--aot', f'--platform={VM_PLATFORM}', '-Ddart.vm.product=true',
         f'--output={dill}', fixture])


def gen(args):
    return run([OUT / 'gen_snapshot', '--deterministic', '--snapshot_kind=app-aot-elf'] + args)


def runtime_copy(work):
    # macOS kills a binary rebuilt in place whose old signature is cached
    # (SIGKILL, exit -9). Run a fresh copy (new inode), ad-hoc signed.
    rt = work / 'dartaotruntime'
    if rt.exists():
        rt.unlink()
    shutil.copy2(OUT / 'dartaotruntime', rt)
    run(['codesign', '-f', '-s', '-', rt])
    return rt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', default=str(WS / '.agents/ios-linker-20261104/runtime'))
    ap.add_argument('files', nargs='+')
    a = ap.parse_args()
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    fixture = work / 'main.dart'  # one source URI for all versions
    ok = True
    for i in range(0, len(a.files), 2):
        rel_src, patch_src = a.files[i], a.files[i + 1]
        d = work / f'pair{i // 2}'
        d.mkdir(exist_ok=True)
        kernel(rel_src, d / 'rel.dill', fixture)
        kernel(patch_src, d / 'patch.dill', fixture)
        gen([f'--elf={d}/App.aot', f'--print_link_info_to={d}/App.link', d / 'rel.dill'])
        gen([f'--elf={d}/stock.aot', d / 'patch.dill'])
        gen([f'--elf={d}/pass1.aot', f'--link_base={d}/App.link',
             f'--print_link_info_to={d}/pass1.link', d / 'patch.dill'])
        run([HOST_SDK / 'bin/dart', TOOL, 'pool-hints', '--base-link', d / 'App.link',
             '--patch-link', d / 'pass1.link', '--out', d / 'constraints.link'])
        gen([f'--elf={d}/out.aot', f'--link_base={d}/constraints.link',
             f'--print_link_info_to={d}/out.link', d / 'patch.dill'])
        link = run([HOST_SDK / 'bin/dart', TOOL, 'link', '--base', d / 'App.aot',
                    '--base-link', d / 'App.link', '--patch-elf', d / 'out.aot',
                    '--patch-link', d / 'out.link', '--out', d / 'out.vmcode'])
        check = run([HOST_SDK / 'bin/dart', TOOL, 'check', '--vmcode', d / 'out.vmcode',
                     '--base', d / 'App.aot', '--patch-elf', d / 'out.aot',
                     '--base-link', d / 'App.link', '--patch-link', d / 'out.link'])
        rt = runtime_copy(work)
        release_out = run([rt, d / 'App.aot'])
        stock_out = run([rt, d / 'stock.aot'])
        patch_out = run([rt, d / 'out.aot'])
        pass1_out = run([rt, d / 'pass1.aot'])
        same = stock_out == patch_out == pass1_out
        ok &= same
        print(f'{rel_src} -> {patch_src}: outputs {"MATCH" if same else "DIFFER"}; '
              f'{link.splitlines()[0]}; {check.strip()}')
        if not same:
            print('--- stock\n' + stock_out + '--- constrained\n' + patch_out)
        (d / 'outputs.txt').write_text(
            f'release:\n{release_out}\nstock patch:\n{stock_out}\nconstrained patch:\n{patch_out}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
