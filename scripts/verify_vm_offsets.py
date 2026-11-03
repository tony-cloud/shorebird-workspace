#!/usr/bin/env python3
"""Verify the checked-in VM runtime offsets against the official extractor.

Runs dart-sdk/tools/run_offsets_extractor.dart (a patched copy that restricts
the architecture list and swaps `git cl format` for the checked-out
clang-format) with the GN args the checked-in header assumes
(dart_enable_aot_patching=true dart_enable_shorebird_interpreter=true), then
compares every regenerated block of
runtime/vm/compiler/runtime_offsets_extracted.h block-by-block and the whole of
pkg/native_compiler/lib/runtime/vm_offsets.g.dart.

The header keeps a hand-maintained conditional on AOT_Code_elements_start_offset:

  #if defined(DART_SHOREBIRD_INTERPRETER)  <interpreter value>
  #else                                    <non-interpreter value>
  #endif

The extractor emits one value per build, so the checked-in conditional is
collapsed to its interpreter value before comparing with the interpreter build,
and the non-interpreter value is verified separately with a control build
(--control-arch, interpreter disabled).  Nothing else is normalized except
whitespace and line continuations.
"""
import argparse
import difflib
import json
import os
import pathlib
import platform
import re
import subprocess
import sys

HEADER = 'runtime/vm/compiler/runtime_offsets_extracted.h'
DART_FILE = 'pkg/native_compiler/lib/runtime/vm_offsets.g.dart'
ALL_ARCHS = ['simarm', 'x64', 'ia32', 'simarm64', 'x64c', 'simarm64c',
             'simriscv32', 'simriscv64']
# 12 32-bit blocks (simarm, ia32, simriscv32 x release/product x JIT/AOT) plus
# simarm64, which is also the only arch vm_offsets.g.dart is generated from and
# carries the hand-kept DART_SHOREBIRD_INTERPRETER conditional.
CI_ARCHS = ['simarm', 'ia32', 'simriscv32', 'simarm64']
GN_ARGS = ['dart_dynamic_modules=false', 'dart_enable_aot_patching=true',
           'dart_enable_shorebird_interpreter=true']
CONDITIONAL = re.compile(
    r'= #if defined\(DART_SHOREBIRD_INTERPRETER\) (\S+?); #else (\S+?); #endif')


def split_blocks(text):
    """Returns {(condition, occurrence): block_text} for each PRODUCT/ARCH block."""
    start = text.index('\n#if ') + 1
    end = text.rindex('\n#endif ') + 1
    blocks, cur = {}, None
    seen = {}
    for line in text[start:end].split('\n'):
        if cur is None:
            if line.startswith('#if ') and 'defined(PRODUCT)' in line:
                cur = [line]
            continue
        cur.append(line)
        if re.match(r'^\s+// !?defined\(DART_COMPRESSED_POINTERS\)$', line):
            cond = re.sub(r'\s+', ' ', ' '.join(
                l.strip(' \\') for l in cur[:2] + cur[-3:]))
            n = seen.get(cond, 0)
            seen[cond] = n + 1
            blocks[(cond, n)] = '\n'.join(cur)
            cur = None
    return blocks


def collapse(block):
    return re.sub(r'\s+', ' ', block.replace('\\\n', ' ')).strip()


def normalize_interpreter(block):
    """Collapses the hand-kept conditional to its interpreter value."""
    return CONDITIONAL.sub(r'= \1;', collapse(block))


def non_interpreter_value(block):
    m = CONDITIONAL.search(collapse(block))
    return int(m.group(2).rstrip(';'), 16) if m else None


def lines_of(collapsed):
    return [s.strip() + ';' for s in collapsed.split(';') if s.strip()]


def self_test():
    blk = ('#if !defined(PRODUCT) && defined(TARGET_ARCH_ARM64) &&  \\\n'
           '    !defined(DART_COMPRESSED_POINTERS)\n'
           'static constexpr dart::compiler::target::word AOT_Code_elements_start_offset =\n'
           '#if defined(DART_SHOREBIRD_INTERPRETER)\n    0xa0;\n#else\n    0x98;\n#endif\n'
           'static constexpr dart::compiler::target::word AOT_Code_element_size = 0x4;\n'
           '#endif  // !defined(PRODUCT) &&\n        // defined(TARGET_ARCH_ARM64) &&\n'
           '        // !defined(DART_COMPRESSED_POINTERS)')
    norm = normalize_interpreter(blk)
    assert '= 0xa0;' in norm and '#else' not in norm, norm
    assert non_interpreter_value(blk) == 0x98
    assert 'AOT_Code_elements_start_offset = 0xa0; static constexpr' in norm
    print('verify_vm_offsets self-test passed')


def run(cmd, cwd, **kw):
    print('+', ' '.join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True, **kw)


def gn_flags():
    return [f for a in GN_ARGS for f in ('--gn-args', a)]


def patched_extractor(sdk, archs):
    src = (sdk / 'tools/run_offsets_extractor.dart').read_text()
    arch_list = ',\n'.join(f"    '{a}'" for a in archs)
    out, n = re.subn(r'final archs = \[[^\]]*\];',
                     f'final archs = [\n{arch_list},\n  ];', src)
    assert n == 1, 'could not restrict the extractor arch list'
    plat = {'Linux': 'linux-x64', 'Darwin': 'mac'}[platform.system()]
    fmt = ("await run(['buildtools/%s/clang-format', '-style=file', "
           "'-assume-filename=%s', '-i', extractedOffsetsFile]);" % (plat, HEADER))
    out, n = re.subn(r"await run\(\['git', 'cl', 'format', extractedOffsetsFile\]\);",
                     fmt, out)
    assert n == 1, 'could not replace git cl format'
    dst = sdk / 'tools/run_offsets_extractor_ci.dart'
    dst.write_text(out)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--self-test', action='store_true')
    ap.add_argument('--dart-sdk', default='dart-sdk')
    ap.add_argument('--archs', default=','.join(CI_ARCHS),
                    help='comma list, or "all" for the official 8 archs')
    ap.add_argument('--control-arch', default='simarm64',
                    help='64-bit arch rebuilt without the interpreter to verify '
                         'the #else values ("" to skip)')
    ap.add_argument('--dart', default='tools/sdks/dart-sdk/bin/dart')
    a = ap.parse_args()
    if a.self_test:
        self_test()
        return 0

    sdk = pathlib.Path(a.dart_sdk).resolve()
    archs = ALL_ARCHS if a.archs == 'all' else a.archs.split(',')
    header, dart_file = sdk / HEADER, sdk / DART_FILE
    orig_header, orig_dart = header.read_text(), dart_file.read_text()
    orig_blocks = split_blocks(orig_header)

    script = patched_extractor(sdk, archs)
    try:
        run([a.dart, 'pub', 'get'], sdk)
        run([a.dart, str(script.relative_to(sdk))] + gn_flags(), sdk)
    finally:
        script.unlink()
    new_header, new_dart = header.read_text(), dart_file.read_text()
    # Leave the checked-in files untouched in the working tree.
    header.write_text(orig_header)
    dart_file.write_text(orig_dart)

    failures = []
    new_blocks = split_blocks(new_header)
    expected = len(archs) * 2 * 2
    if len(new_blocks) != expected:
        failures.append(f'expected {expected} generated blocks, got {len(new_blocks)}')
    for key, blk in new_blocks.items():
        if key not in orig_blocks:
            failures.append(f'generated block missing from checked-in header: {key}')
            continue
        want, got = normalize_interpreter(orig_blocks[key]), collapse(blk)
        if want != got:
            diff = '\n'.join(difflib.unified_diff(
                lines_of(want), lines_of(got), 'checked-in (normalized)',
                'extractor', lineterm='', n=1))
            failures.append(f'block differs: {key}\n{diff}')
    if new_dart != orig_dart:
        diff = '\n'.join(list(difflib.unified_diff(
            orig_dart.splitlines(), new_dart.splitlines(), 'checked-in',
            'extractor', lineterm='', n=0))[:80])
        failures.append(f'{DART_FILE} differs\n{diff}')

    if a.control_arch:
        ext = '.exe' if platform.system() == 'Windows' else ''
        for mode in ('release', 'product'):
            build = ['python3', 'tools/build.py', f'-a{a.control_arch}', f'-m{mode}',
                     '--no-rbe', '--gn-args', 'dart_dynamic_modules=false',
                     '--gn-args', 'dart_enable_aot_patching=true',
                     '--gn-args', 'dart_enable_shorebird_interpreter=false',
                     'offsets_extractor_aotruntime']
            run(build, sdk)
            exe = sdk / 'out' / f'{mode.capitalize()}{a.control_arch.upper()}' / ('offsets_extractor_aotruntime' + ext)
            cfg = json.loads(subprocess.run([str(exe)], cwd=sdk, check=True,
                                            capture_output=True, text=True).stdout)
            code = next(o for o in cfg['offsets']
                        if o['kind'] == 'array' and o['class'] == 'Code')
            got = int(code['startOffset'])
            arch = cfg['arch'].upper()
            prod = 'defined(PRODUCT)' if cfg['product'] else '!defined(PRODUCT)'
            cands = [b for (c, n), b in orig_blocks.items()
                     if n == 1 and c.startswith(f'#if {prod} && defined(TARGET_ARCH_{arch}) &&')
                     and c.split(' // ')[0].endswith('&& !defined(DART_COMPRESSED_POINTERS)')]
            want = {non_interpreter_value(b) for b in cands} - {None}
            print(f'control {mode}/{a.control_arch}: non-interpreter Code start '
                  f'offset extractor={got:#x} checked-in #else={sorted(map(hex, want))}')
            if want != {got}:
                failures.append(f'#else value for {mode}/{a.control_arch} is '
                                f'{sorted(map(hex, want))}, extractor says {got:#x}')

    if failures:
        print('\n'.join(failures), file=sys.stderr)
        print(f'FAIL: {len(failures)} offsets mismatch(es); regenerate with '
              'tools/run_offsets_extractor.dart (all archs).', file=sys.stderr)
        return 1
    print(f'OK: {len(new_blocks)} blocks and {DART_FILE} match the extractor')
    return 0


if __name__ == '__main__':
    sys.exit(main())
