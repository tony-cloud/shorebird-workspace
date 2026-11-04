#!/usr/bin/env python3
"""Verify the checked-in VM runtime offsets against the official extractor.

Runs dart-sdk/tools/run_offsets_extractor.dart (a patched copy that restricts
the architecture list and skips `git cl format`, which needs clang-format) with the GN args the checked-in header assumes
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
whitespace/layout (the header is compared as a token stream, so clang-format is
not needed) and line continuations.
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
    r'= #if defined \( DART_SHOREBIRD_INTERPRETER \) (\S+) ; #else (\S+) ; #endif')


TOKEN = re.compile(r'#\s*\w+|//|0[xX][0-9a-fA-F]+|\w+|[^\s\w]')
BLOCK = re.compile(
    r'#if (!? ?defined \( PRODUCT \) & & defined \( TARGET_ARCH_\w+ \) & & '
    r'!? ?defined \( DART_COMPRESSED_POINTERS \)) (.*?)'  # body may be empty
    r'#endif // !? ?defined \( PRODUCT \) & & // defined \( TARGET_ARCH_\w+ \) '
    r'& & // !? ?defined \( DART_COMPRESSED_POINTERS \)')


def collapse(text):
    """Token-normalizes C text: line continuations, whitespace and clang-format
    layout (line wrapping, spacing around punctuation) do not matter."""
    return ' '.join(TOKEN.findall(text.replace('\\\n', ' ')))


def split_blocks(text):
    """Returns {(condition, occurrence): collapsed_block} for each block."""
    start = text.index('\n#if ') + 1
    end = text.rindex('\n#endif ') + 1
    blocks, seen = {}, {}
    for m in BLOCK.finditer(collapse(text[start:end])):
        cond = m.group(1)
        n = seen.get(cond, 0)
        seen[cond] = n + 1
        blocks[(cond, n)] = m.group(0)
    return blocks


def normalize_interpreter(block):
    """Collapses the hand-kept conditional to its interpreter value."""
    return CONDITIONAL.sub(r'= \1 ;', collapse(block))


def non_interpreter_value(block):
    m = CONDITIONAL.search(collapse(block))
    return int(m.group(2), 16) if m else None


def lines_of(collapsed):
    return [s.strip() + ';' for s in collapsed.split(';') if s.strip()]


def header_text(body_lines, wrapped):
    """Builds a one-block header; wrapped=True mimics clang-format layout."""
    cond = '!defined(PRODUCT) && defined(TARGET_ARCH_ARM64) && !defined(DART_COMPRESSED_POINTERS)'
    if wrapped:
        top = ('#if !defined(PRODUCT) && defined(TARGET_ARCH_ARM64) &&  \\\n'
               '    !defined(DART_COMPRESSED_POINTERS)')
        end = ('#endif  // !defined(PRODUCT) &&\n        // defined(TARGET_ARCH_ARM64) &&\n'
               '        // !defined(DART_COMPRESSED_POINTERS)')
    else:
        top = '#if ' + cond
        end = ('#endif  // !defined(PRODUCT) &&\n// defined(TARGET_ARCH_ARM64) &&\n'
               '// !defined(DART_COMPRESSED_POINTERS)')
    return ('// hdr\n#ifndef X\n#define X\n' + top + '\n' + '\n'.join(body_lines) +
            '\n' + end + '\n\n#endif  // X\n')


def compare_blocks(orig_blocks, new_blocks):
    """Returns failure strings for generated blocks that differ from checked-in."""
    failures = []
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
    return failures


def self_test():
    # Checked-in (clang-format layout, with the hand-kept conditional).
    checked_in = header_text([
        'static constexpr dart::compiler::target::word AOT_Code_elements_start_offset =',
        '#if defined(DART_SHOREBIRD_INTERPRETER)\n    0xa0;\n#else\n    0x98;\n#endif',
        'static constexpr dart::compiler::target::word AOT_Code_element_size = 0x4;',
        'static constexpr dart::compiler::target::word Foo_x[] = {0x1, 0x2};',
    ], wrapped=True)
    norm = normalize_interpreter(checked_in)
    assert '= 0xa0 ;' in norm and '#else' not in norm, norm
    assert non_interpreter_value(checked_in) == 0x98
    orig = split_blocks(checked_in)
    assert len(orig) == 1, orig

    def gen(code_start='0xa0', size='0x4', arr='{0x1, 0x2}'):
        # Unformatted extractor output (what we get without clang-format).
        return header_text([
            'static constexpr dart::compiler::target::word AOT_Code_elements_start_offset = %s;' % code_start,
            'static constexpr dart::compiler::target::word AOT_Code_element_size = %s;' % size,
            'static constexpr dart::compiler::target::word Foo_x[] = %s;' % arr,
        ], wrapped=False)

    # Formatting-only difference must pass.
    assert compare_blocks(orig, split_blocks(gen())) == []
    # Content differences must fail.
    for bad in (gen(code_start='0xa8'), gen(size='0x8'), gen(arr='{0x1, 0x3}'),
                gen(arr='{0x1}')):
        assert compare_blocks(orig, split_blocks(bad)), bad
    # A block with an unknown condition must fail too.
    other = gen().replace('TARGET_ARCH_ARM64', 'TARGET_ARCH_ARM')
    assert compare_blocks(orig, split_blocks(other)), 'unknown block accepted'
    # An empty block must not swallow the block after it.
    empty = header_text([], wrapped=True)
    two = empty.replace('\n\n#endif  // X', '\n' + header_text(['int a = 1;'], wrapped=False)
                        .split('#define X\n')[1].replace('ARM64', 'X64').rsplit('\n\n#endif', 1)[0] + '\n\n#endif  // X')
    assert len(split_blocks(two)) == 2, split_blocks(two)
    print('verify_vm_offsets self-test passed')


def run(cmd, cwd, **kw):
    print('+', ' '.join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True, **kw)


EXTRA_GN_ARGS = []


def gn_flags():
    return [f for a in GN_ARGS + EXTRA_GN_ARGS for f in ('--gn-args', a)]


def patched_extractor(sdk, archs):
    src = (sdk / 'tools/run_offsets_extractor.dart').read_text()
    arch_list = ',\n'.join(f"    '{a}'" for a in archs)
    out, n = re.subn(r'final archs = \[[^\]]*\];',
                     f'final archs = [\n{arch_list},\n  ];', src)
    assert n == 1, 'could not restrict the extractor arch list'
    # Skip formatting the header: the runner has no clang-format (the
    # buildtools/linux-x64 CIPD package is not synced) and the comparison is
    # token-based, so layout is irrelevant.  Any extractor failure is therefore
    # a real failure.
    out, n = re.subn(r"await run\(\['git', 'cl', 'format', extractedOffsetsFile\]\);",
                     "print('Skipping header formatting (layout-insensitive compare)');",
                     out)
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
    ap.add_argument('--gn-arg', action='append', default=[],
                    help='extra GN arg for local runs, e.g. mac_sdk_path=...')
    ap.add_argument('--emit-dir', default='',
                    help='copy the regenerated header and vm_offsets.g.dart here '
                         '(raw extractor output) so CI can upload them')
    ap.add_argument('--dart', default='tools/sdks/dart-sdk/bin/dart')
    a = ap.parse_args()
    EXTRA_GN_ARGS.extend(a.gn_arg)
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
    if a.emit_dir:
        out = pathlib.Path(a.emit_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / pathlib.Path(HEADER).name).write_text(new_header)
        (out / pathlib.Path(DART_FILE).name).write_text(new_dart)
    # Leave the checked-in files untouched in the working tree.
    header.write_text(orig_header)
    dart_file.write_text(orig_dart)

    failures = []
    new_blocks = split_blocks(new_header)
    expected = len(archs) * 2 * 2
    if len(new_blocks) != expected:
        failures.append(f'expected {expected} generated blocks, got {len(new_blocks)}')
    failures += compare_blocks(orig_blocks, new_blocks)
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
                     ] + [f for g in EXTRA_GN_ARGS for f in ('--gn-args', g)] + [
                     'offsets_extractor_aotruntime']
            run(build, sdk)
            exe = sdk / 'out' / f'{mode.capitalize()}{a.control_arch.upper()}' / ('offsets_extractor_aotruntime' + ext)
            cfg = json.loads(subprocess.run([str(exe)], cwd=sdk, check=True,
                                            capture_output=True, text=True).stdout)
            code = next(o for o in cfg['offsets']
                        if o['kind'] == 'array' and o['class'] == 'Code')
            got = int(code['startOffset'])
            arch = re.sub(r'^SIM', '', cfg['arch'].upper())  # simarm64 -> TARGET_ARCH_ARM64
            prod = 'defined ( PRODUCT )' if cfg['product'] else '! defined ( PRODUCT )'
            # Block keys are the token-normalized condition (no '#if').
            cands = [b for (c, n), b in orig_blocks.items()
                     if n == 1 and c.startswith(
                         f'{prod} & & defined ( TARGET_ARCH_{arch} ) & &')
                     and c.endswith('& & ! defined ( DART_COMPRESSED_POINTERS )')]
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
