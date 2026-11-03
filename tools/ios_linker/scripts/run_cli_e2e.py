#!/usr/bin/env python3
"""Host end-to-end of the iOS ios-aot-link route through the real tooling code.

release  : flutter_tools AOTSnapshotter (real gen_snapshot, xcrun) -> sidecar
           CLI IosReleaser.assembleAotLinkSupplement -> zip -> safe extract
patch    : flutter_tools AOTSnapshotter with SHOREBIRD_AOT_LINK_BASE (two ELF
           passes + ios_linker pool-hints + assembly build)
           CLI IosPatcher.createAotLinkPatchArtifacts (link --min-link-percent,
           check, diff-base, `patch` bidiff+zstd, hash)
device   : the updater's own inflate reconstructs the download against the
           release blob; byte-equal to the vmcode; header verified.

usage: run_cli_e2e.py [--case NAME] [--dd N] [--work DIR]
GEN_SNAPSHOT selects gen_snapshot (default: the linker branch's ProductARM64SB).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_flutter_app as f  # noqa: E402
import run_matrix as m  # noqa: E402

WS = m.WS
SB = WS / '.agents/wt/linker/dart-sdk/xcodebuild/ProductARM64SB/gen_snapshot'
FLUTTER_DART = WS / 'flutter/bin/cache/dart-sdk/bin/dart'
PATCH_TOOL = WS / 'updater/target/release/patch'
TOOL = WS / 'tools/ios_linker/bin/ios_linker.dart'


def run(cmd, cwd, env_extra, log):
    env = dict(os.environ, **env_extra)
    with open(log, 'ab') as out:
        out.write(f'$ {" ".join(map(str, cmd))}\n'.encode())
        r = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, stdout=out,
                           stderr=subprocess.STDOUT)
        out.write(f'# exit={r.returncode}\n'.encode())
    print(f'  [{r.returncode}] {" ".join(map(str, cmd[:4]))} ...', flush=True)
    if r.returncode:
        print(Path(log).read_text(errors='replace')[-6000:])
        raise SystemExit(f'failed: {cmd[:3]}')
    return r


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--case', default='f2_ui_string', choices=list(f.CASES))
    ap.add_argument('--dd', type=int, help='enable DD with this caller-tree threshold')
    ap.add_argument('--work', default=str(WS / '.agents/ios-aot-link-cli-e2e-20261104'))
    a = ap.parse_args()
    work = Path(a.work) / (a.case + ('_dd' if a.dd else ''))
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    log = work / 'log.txt'
    gen = Path(os.environ.get('GEN_SNAPSHOT', SB))
    assert gen.exists(), gen
    assert PATCH_TOOL.exists(), PATCH_TOOL
    sdk = subprocess.check_output(['xcrun', '--sdk', 'iphoneos', '--show-sdk-path'], text=True).strip()

    # The project: patch_perf_bench with the aot-link profile in shorebird.yaml.
    project = work / 'app'
    shutil.copytree(f.APP_SRC, project, symlinks=True)
    (project / 'shorebird.yaml').write_text(
        'app_id: e2e-aot-link\naot_patch_runtime_mode: ios-aot-link\n'
        'patch_verification: strict\n')
    m.GEN = gen
    # Kernels (release, and the patch with a source edit) as a release build makes them.
    (work / 'release').mkdir()
    (work / 'patch').mkdir()
    print('frontend (release + patch kernels)', flush=True)
    f.frontend(project, work / 'release/app.dill', log)
    pristine = {p: (project / p).read_text() for p in ('lib/kernel.dart', 'lib/main.dart')}
    try:
        for path, old, new in f.CASES[a.case]:
            text = (project / path).read_text()
            assert old in text, old
            (project / path).write_text(text.replace(old, new, 1))
        f.frontend(project, work / 'patch/app.dill', log)
    finally:
        for p, t in pristine.items():
            (project / p).write_text(t)

    def cfg(stage, **extra):
        c = dict(stage=stage, work=str(work), project=str(project), gen_snapshot=str(gen),
                 dart=str(m.DART), tool=str(TOOL), sdk_root=sdk, patch_tool=str(PATCH_TOOL),
                 dill=str(work / stage / 'app.dill'), **extra)
        path = work / f'{stage}.cfg.json'
        path.write_text(json.dumps(c))
        return path

    ft = WS / 'flutter/packages/flutter_tools'
    cli = WS / 'shorebird/packages/shorebird_cli'
    ft_test = 'test/general.shard/shorebird/shorebird_aot_link_host_e2e_test.dart'
    cli_test = 'test/e2e/ios_aot_link_host_e2e_test.dart'
    # flutter_tools' test guard only allows writes under the temp dir; this run
    # writes under .agents on purpose (never the system disk).
    env = {'TMPDIR': str(WS / '.agents/tmp-aotlink'), 'FLUTTER_TEST_DISABLE_FS_GUARD': 'true'}
    dd = {'dd_max_bytes': a.dd, 'dd_packages': ['patch_perf_bench']} if a.dd else {}

    print('1/4 flutter_tools release build', flush=True)
    c = cfg('release', **dd)
    run([FLUTTER_DART, 'test', ft_test], ft, {**env, 'SHOREBIRD_AOT_LINK_E2E': c}, log)
    print('2/4 CLI release supplement (assemble, zip, extract, verify)', flush=True)
    run([FLUTTER_DART, 'test', cli_test], cli, {**env, 'SHOREBIRD_AOT_LINK_E2E': c}, log)
    rel = json.loads((work / 'release.cli.json').read_text())
    supplement = Path(rel['supplement'])
    print('3/4 flutter_tools patch build against the downloaded supplement', flush=True)
    c = cfg('patch', base_link=str(supplement / 'App.link'),
            dd_targets=str(supplement / 'App.dd') if (supplement / 'App.dd').exists() else None)
    run([FLUTTER_DART, 'test', ft_test], ft, {**env, 'SHOREBIRD_AOT_LINK_E2E': c}, log)
    print('4/4 CLI patch: link, check, diff-base, bidiff+zstd', flush=True)
    c = cfg('patch', supplement=str(supplement), base_link=str(supplement / 'App.link'))
    run([FLUTTER_DART, 'test', cli_test], cli, {**env, 'SHOREBIRD_AOT_LINK_E2E': c}, log)
    pat = json.loads((work / 'patch.cli.json').read_text())

    vmcode, diff, base = Path(pat['vmcode']), Path(pat['diff']), Path(pat['diff_base'])
    assert sha256(vmcode) == pat['hash']
    assert pat['release_text_sha256'] == rel['release_text_sha256']

    print('device: updater inflate against the release blob', flush=True)
    cargo_env = {**env, 'SHOREBIRD_E2E_BASE': str(base), 'SHOREBIRD_E2E_DIFF': str(diff),
                 'SHOREBIRD_E2E_EXPECTED': str(vmcode), 'SHOREBIRD_E2E_HASH': pat['hash']}
    cargo = ['cargo', '+1.97.0', 'test', '--offline', '-p', 'updater', '--lib',
             'linked_aot_host_e2e', '--', '--ignored', '--nocapture']
    run(cargo, WS / 'updater', cargo_env, log)

    # Negative control: a different release blob must not reconstruct the patch.
    tampered = work / 'App.diffbase.tampered'
    blob = bytearray(base.read_bytes())
    blob[-4096] ^= 0xFF
    tampered.write_bytes(bytes(blob))
    r = subprocess.run(cargo, cwd=WS / 'updater', env={**os.environ, **cargo_env,
                       'SHOREBIRD_E2E_BASE': str(tampered)}, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    with open(log, 'a') as out:
        out.write(f'# negative control exit={r.returncode}\n{r.stdout[-2000:]}\n')
    assert r.returncode != 0, 'a tampered release blob reconstructed the patch'
    print(f'  negative control: tampered base rejected (exit {r.returncode})')

    summary = {
        'case': a.case, 'dd': a.dd, 'gen_snapshot': str(gen),
        'release_supplement_zip_bytes': rel['zip_bytes'],
        'release_supplement_files': rel['files'],
        'link_percent': pat['link_percent'],
        'linked_code_bytes': pat['linked_code_bytes'], 'patch_code_bytes': pat['patch_code_bytes'],
        'vmcode_bytes': pat['vmcode_bytes'], 'diff_base_bytes': base.stat().st_size,
        'download_bytes': pat['diff_bytes'], 'vmcode_sha256': pat['hash'],
        'byte_equal_after_updater_inflate': True, 'tampered_base_rejected': True,
    }
    (work / 'summary.json').write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == '__main__':
    main()
