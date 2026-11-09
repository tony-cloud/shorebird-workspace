#!/usr/bin/env python3
"""Lifecycle negatives of an installed hardened release (P6 runbook).

docs/HARDENING_CONTRACTS.md section 12.6. One command per platform runs the
device-side half of the hardened acceptance on a real installation:

  baseline         the release as built: the integrity gate passes and logs
                   "Integrity check passed (N us)" (startup cost, N samples)
  tampered-config  shorebird.yaml edited after signing (base_url redirected):
                   the gate refuses with config_digest_mismatch
  replaced-engine  one byte of a measured (non-writable) section of the engine
                   library flipped, then the package re-signed the way an
                   attacker can (ad-hoc / own key): the gate refuses with
                   module_mismatch
  reinstalled      the original release installed again: the gate passes

A refused gate keeps the bundled release running without patches; the runbook
checks the log line only, not the UI. The evidence (JSON) records every
launch's log lines, the outcome, and the startup-cost samples.

Subcommands:
  desktop  --platform macos|linux|windows --app <bundle>   (runs locally)
  android  --apk <apk> --package <id> --keystore <ks>      (adb, apksigner)
  ios      --app <Runner.app> --device <udid> --identity <codesign id>
           (xcrun devicectl; the identity re-signs mutated copies)

Nothing here needs a production key: the release under test is whatever the
operator built (a development-root build is fine for the gate's behaviour, and
the evidence names the release's toolchain ID and development flag).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import plistlib
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

PASSED = re.compile(r'Integrity check passed \((\d+) us\)')
REFUSED_UPDATER = re.compile(r'integrity check failed: ([a-z_]+)(?: \([^)]*\))*(?: \((\d+) us\))?')
REFUSED_ENGINE = re.compile(r'Shorebird integrity check failed \(([a-z_]+)\)')
MANIFEST_PREFIX = 'shorebird_build_manifest:'

EXPECTED = {
    'baseline': ('passed', None),
    'tampered-config': ('refused', 'config_digest_mismatch'),
    'replaced-engine': ('refused', 'module_mismatch'),
    'reinstalled': ('passed', None),
}


class LifecycleError(Exception):
    pass


# --- log parsing -----------------------------------------------------------


def parse_integrity(text: str) -> dict:
    """The gate's outcome in a launch log (updater or engine line)."""
    passed = PASSED.findall(text)
    refused = [m.group(1) for m in REFUSED_UPDATER.finditer(text)]
    refused += REFUSED_ENGINE.findall(text)
    if refused:
        codes = sorted(set(refused))
        return {'outcome': 'refused' if len(codes) == 1 else 'ambiguous', 'code': ','.join(codes),
                'micros': None}
    if passed:
        return {'outcome': 'passed', 'code': None, 'micros': int(passed[-1])}
    return {'outcome': 'unknown', 'code': None, 'micros': None}


def integrity_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()
            if 'ntegrity check' in line or 'Integrity check refused' in line]


# --- mutations ---------------------------------------------------------------


def tamper_config(data: bytes) -> bytes:
    """Redirect base_url (or add a line) above the signed manifest line."""
    text = data.decode('utf-8')
    lines = text.splitlines(keepends=True)
    if not lines or not lines[-1].startswith(MANIFEST_PREFIX):
        raise LifecycleError('shorebird.yaml has no trailing shorebird_build_manifest line; '
                             'is this a hardened release?')
    for index, line in enumerate(lines[:-1]):
        if line.startswith('base_url:'):
            lines[index] = 'base_url: https://attacker.invalid\n'
            break
    else:
        lines.insert(len(lines) - 1, 'auto_update: true\n')
    tampered = ''.join(lines).encode('utf-8')
    if tampered == data:
        raise LifecycleError('the config mutation changed nothing')
    return tampered


def _macho_cstring_offsets(data: bytes) -> list[tuple[int, int]]:
    """(file offset, size) of __TEXT,__cstring in every 64-bit slice."""
    magic = struct.unpack_from('>I', data, 0)[0]
    if magic in (0xCAFEBABE, 0xCAFEBABF):  # fat
        count = struct.unpack_from('>I', data, 4)[0]
        wide = magic == 0xCAFEBABF
        slices = []
        for index in range(count):
            if wide:
                _, _, offset, _, _, _ = struct.unpack_from('>iiQQII', data, 8 + index * 32)
            else:
                _, _, offset, _, _ = struct.unpack_from('>iiIII', data, 8 + index * 20)
            slices.append(offset)
    else:
        slices = [0]
    found = []
    for base in slices:
        if struct.unpack_from('<I', data, base)[0] != 0xFEEDFACF:
            raise LifecycleError('not a 64-bit Mach-O slice')
        ncmds = struct.unpack_from('<I', data, base + 16)[0]
        cursor = base + 32
        for _ in range(ncmds):
            cmd, size = struct.unpack_from('<II', data, cursor)
            if cmd == 0x19:  # LC_SEGMENT_64
                segname = data[cursor + 8:cursor + 24].rstrip(b'\0')
                nsects = struct.unpack_from('<I', data, cursor + 64)[0]
                for section in range(nsects):
                    at = cursor + 72 + section * 80
                    sectname = data[at:at + 16].rstrip(b'\0')
                    sec_size = struct.unpack_from('<Q', data, at + 40)[0]
                    sec_offset = struct.unpack_from('<I', data, at + 48)[0]
                    if segname == b'__TEXT' and sectname == b'__cstring':
                        found.append((base + sec_offset, sec_size))
            cursor += size
    return found


def _elf_rodata(data: bytes) -> list[tuple[int, int]]:
    if data[:4] != b'\x7fELF':
        raise LifecycleError('not an ELF file')
    is64 = data[4] == 2
    endian = '<' if data[5] == 1 else '>'
    if is64:
        shoff = struct.unpack_from(endian + 'Q', data, 0x28)[0]
        shentsize, shnum, shstrndx = struct.unpack_from(endian + 'HHH', data, 0x3A)
    else:
        shoff = struct.unpack_from(endian + 'I', data, 0x20)[0]
        shentsize, shnum, shstrndx = struct.unpack_from(endian + 'HHH', data, 0x2E)

    def header(index):
        at = shoff + index * shentsize
        if is64:
            name, _, flags, _, offset, size = struct.unpack_from(endian + 'IIQQQQ', data, at)
        else:
            name, _, flags, _, offset, size = struct.unpack_from(endian + 'IIIIII', data, at)
        return name, flags, offset, size

    _, _, strtab, _ = header(shstrndx)
    found = []
    for index in range(shnum):
        name, flags, offset, size = header(index)
        label = data[strtab + name:data.index(b'\0', strtab + name)]
        if label == b'.rodata' and not flags & 0x1:  # SHF_WRITE
            found.append((offset, size))
    return found


def _pe_rdata(data: bytes) -> list[tuple[int, int]]:
    if data[:2] != b'MZ':
        raise LifecycleError('not a PE file')
    pe = struct.unpack_from('<I', data, 0x3C)[0]
    if data[pe:pe + 4] != b'PE\0\0':
        raise LifecycleError('not a PE file')
    nsections = struct.unpack_from('<H', data, pe + 6)[0]
    optional = struct.unpack_from('<H', data, pe + 20)[0]
    table = pe + 24 + optional
    found = []
    for index in range(nsections):
        at = table + index * 40
        name = data[at:at + 8].rstrip(b'\0')
        raw_size, raw_offset = struct.unpack_from('<II', data, at + 16)
        characteristics = struct.unpack_from('<I', data, at + 36)[0]
        if name == b'.rdata' and not characteristics & 0x80000000:  # IMAGE_SCN_MEM_WRITE
            found.append((raw_offset, raw_size))
    return found


def flip_measured_byte(data: bytes) -> bytes:
    """Flip one byte inside a non-writable (measured) section of every slice."""
    if data[:4] == b'\x7fELF':
        regions = _elf_rodata(data)
    elif data[:2] == b'MZ':
        regions = _pe_rdata(data)
    else:
        regions = _macho_cstring_offsets(data)
    regions = [(offset, size) for offset, size in regions if size > 32]
    if not regions:
        raise LifecycleError('no measured read-only section to modify')
    mutated = bytearray(data)
    for offset, size in regions:
        mutated[offset + size // 2] ^= 0x20
    return bytes(mutated)


# --- package layouts -----------------------------------------------------------


@dataclass
class Layout:
    config: Path
    engine: Path
    executable: Path | None = None


def desktop_layout(platform: str, app: Path, executable: str | None = None) -> Layout:
    if platform == 'macos':
        info = plistlib.loads((app / 'Contents' / 'Info.plist').read_bytes())
        frameworks = app / 'Contents' / 'Frameworks'
        config = frameworks / 'App.framework' / 'Resources' / 'flutter_assets' / 'shorebird.yaml'
        engine = frameworks / 'FlutterMacOS.framework' / 'Versions' / 'A' / 'FlutterMacOS'
        exe = app / 'Contents' / 'MacOS' / (executable or info['CFBundleExecutable'])
    elif platform in ('linux', 'windows'):
        config = app / 'data' / 'flutter_assets' / 'shorebird.yaml'
        engine = app / ('lib/libflutter_linux_gtk.so' if platform == 'linux' else 'flutter_windows.dll')
        if executable:
            exe = app / executable
        else:
            candidates = sorted(p for p in app.iterdir() if p.is_file() and (
                p.suffix == '.exe' if platform == 'windows' else os.access(p, os.X_OK)))
            if len(candidates) != 1:
                raise LifecycleError(f'pass --executable; candidates: {[p.name for p in candidates]}')
            exe = candidates[0]
    elif platform == 'ios':
        config = app / 'Frameworks' / 'App.framework' / 'flutter_assets' / 'shorebird.yaml'
        engine = app / 'Frameworks' / 'Flutter.framework' / 'Flutter'
        exe = None
    else:
        raise LifecycleError(f'unsupported platform {platform}')
    for path in (config, engine):
        if not path.is_file():
            raise LifecycleError(f'missing {path}')
    return Layout(config, engine, exe)


def prepare_variant(platform: str, original: Path, work: Path, variant: str,
                    resign: Callable[[Path, Layout], None] | None = None,
                    executable: str | None = None) -> Path:
    """Copy the installation and apply the variant's mutation."""
    target = work / variant / original.name
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(original, target, symlinks=True)
    layout = desktop_layout(platform, target, executable)
    if variant == 'tampered-config':
        layout.config.write_bytes(tamper_config(layout.config.read_bytes()))
    elif variant == 'replaced-engine':
        layout.engine.write_bytes(flip_measured_byte(layout.engine.read_bytes()))
        if resign is not None:
            resign(target, layout)
    return target


def mutate_apk(source: Path, target: Path, variant: str, abi: str) -> None:
    """Rewrite an APK with the variant's mutation (unsigned; sign afterwards)."""
    config_name = 'assets/flutter_assets/shorebird.yaml'
    engine_name = f'lib/{abi}/libflutter.so'
    seen = set()
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, 'w') as zout:
        for info in zin.infolist():
            if info.filename.startswith('META-INF/') and info.filename.split('/')[-1].split('.')[-1] in (
                    'SF', 'RSA', 'EC', 'DSA', 'MF'):
                continue  # the old v1 signature; apksigner writes a new one
            data = zin.read(info)
            if variant == 'tampered-config' and info.filename == config_name:
                data = tamper_config(data)
                seen.add(config_name)
            elif variant == 'replaced-engine' and info.filename == engine_name:
                data = flip_measured_byte(data)
                seen.add(engine_name)
            clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            clone.compress_type = info.compress_type
            clone.external_attr = info.external_attr
            zout.writestr(clone, data)
    wanted = {'tampered-config': config_name, 'replaced-engine': engine_name}.get(variant)
    if wanted and wanted not in seen:
        raise LifecycleError(f'{source} has no {wanted}')


# --- running -------------------------------------------------------------------


@dataclass
class Launch:
    variant: str
    run: int
    lines: list[str]
    outcome: str
    code: str | None
    micros: int | None


@dataclass
class Evidence:
    platform: str
    subject: str
    launches: list[Launch] = field(default_factory=list)
    release: dict = field(default_factory=dict)

    def add(self, variant: str, run: int, text: str) -> Launch:
        parsed = parse_integrity(text)
        launch = Launch(variant, run, integrity_lines(text), parsed['outcome'], parsed['code'], parsed['micros'])
        self.launches.append(launch)
        return launch

    def verdict(self) -> dict:
        failures = []
        for variant, (outcome, code) in EXPECTED.items():
            runs = [launch for launch in self.launches if launch.variant == variant]
            if not runs:
                failures.append(f'{variant}: not run')
            for launch in runs:
                if launch.outcome != outcome or (code and launch.code != code):
                    failures.append(f'{variant} run {launch.run}: {launch.outcome} {launch.code or ""}'
                                    f' (expected {outcome} {code or ""})'.rstrip())
        samples = [launch.micros for launch in self.launches
                   if launch.outcome == 'passed' and launch.micros is not None]
        cost = None
        if samples:
            ordered = sorted(samples)
            cost = {'samples_us': samples, 'p50_us': int(statistics.median(ordered)),
                    'max_us': ordered[-1], 'min_us': ordered[0]}
        return {'accepted': not failures, 'failures': failures, 'startup_cost': cost}

    def to_json(self) -> dict:
        return {
            'schema': 'open-shorebird.hardened-lifecycle-evidence',
            'version': 1,
            'platform': self.platform,
            'subject': self.subject,
            'release': self.release,
            'launches': [launch.__dict__ for launch in self.launches],
            **self.verdict(),
        }


def release_facts(config: bytes) -> dict:
    """Toolchain identity of the release under test (from its manifest line)."""
    text = config.decode('utf-8', 'replace')
    facts = {}
    for key in ('security_profile', 'toolchain_id', 'security_development_exceptions'):
        match = re.search(rf'^{key}: *(.+)$', text, re.M)
        if match:
            facts[key] = match.group(1).strip()
    line = text.rstrip('\n').splitlines()[-1] if text.strip() else ''
    if line.startswith(MANIFEST_PREFIX):
        payload = line.split(':', 1)[1].strip().split('.')[1]
        try:
            manifest = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            facts['manifest_contract_version'] = manifest.get('contract_version')
            facts['modules'] = manifest.get('modules')
        except ValueError:
            facts['manifest'] = 'unparsable'
    return facts


def run_lifecycle(evidence: Evidence, runs: int,
                  launch: Callable[[str, int], str]) -> dict:
    """baseline xN, tampered-config, replaced-engine, reinstalled xN."""
    for variant, count in (('baseline', runs), ('tampered-config', 1), ('replaced-engine', 1),
                           ('reinstalled', runs)):
        for run in range(1, count + 1):
            launched = evidence.add(variant, run, launch(variant, run))
            detail = launched.code or (f'({launched.micros} us)' if launched.micros is not None else '')
            print(f'[lifecycle] {variant} #{run}: {launched.outcome} {detail}'.rstrip(), flush=True)
    return evidence.to_json()


def _run_capture(argv: list[str], seconds: float, cwd: Path | None = None) -> tuple[str, int]:
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd)
    try:
        output, _ = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
    return output.decode('utf-8', 'replace'), process.pid


def desktop_launcher(platform: str, app: Path, work: Path, seconds: float,
                     executable: str | None) -> Callable[[str, int], str]:
    def resign(target: Path, layout: Layout) -> None:
        if platform == 'macos':
            for path in (layout.engine.parents[2], target):  # FlutterMacOS.framework, the app
                subprocess.run(['codesign', '--force', '--sign', '-', str(path)], check=True,
                               capture_output=True)

    copies: dict[str, Path] = {}

    def launch(variant: str, run: int) -> str:
        if variant not in copies:
            copies[variant] = prepare_variant(
                platform, app, work, variant if variant != 'reinstalled' else 'reinstalled', resign, executable)
        layout = desktop_layout(platform, copies[variant], executable)
        output, pid = _run_capture([str(layout.executable)], seconds, cwd=layout.executable.parent)
        if platform == 'macos':
            # The updater logs through os_log; the engine's refusal also reaches stderr.
            unified = subprocess.run(['/usr/bin/log', 'show', '--last', f'{int(seconds) + 30}s', '--style', 'compact',
                                      '--predicate', f'processID == {pid}'], capture_output=True, text=True)
            output += unified.stdout
        return output

    return launch


def android_launcher(args, work: Path) -> Callable[[str, int], str]:
    sdk = Path(args.android_sdk or os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT') or '')
    tools = sorted((sdk / 'build-tools').glob('*'))
    if not tools:
        raise LifecycleError('set ANDROID_HOME (needs build-tools with zipalign and apksigner)')
    build_tools = tools[-1]
    adb = [args.adb] + (['-s', args.serial] if args.serial else [])
    apks: dict[str, Path] = {'baseline': Path(args.apk), 'reinstalled': Path(args.apk)}

    def build(variant: str) -> Path:
        unsigned = work / f'{variant}-unsigned.apk'
        aligned = work / f'{variant}-aligned.apk'
        signed = work / f'{variant}.apk'
        mutate_apk(Path(args.apk), unsigned, variant, args.abi)
        subprocess.run([str(build_tools / 'zipalign'), '-f', '-p', '4', str(unsigned), str(aligned)], check=True)
        sign = [str(build_tools / 'apksigner'), 'sign', '--ks', args.keystore, '--out', str(signed)]
        if args.ks_pass:
            sign += ['--ks-pass', args.ks_pass]
        if args.ks_key_alias:
            sign += ['--ks-key-alias', args.ks_key_alias]
        subprocess.run(sign + [str(aligned)], check=True)
        return signed

    installed = {'variant': None}

    def launch(variant: str, run: int) -> str:
        if variant not in apks:
            apks[variant] = build(variant)
        if installed['variant'] != variant:
            subprocess.run(adb + ['uninstall', args.package], capture_output=True)
            subprocess.run(adb + ['install', str(apks[variant])], check=True, capture_output=True)
            installed['variant'] = variant
        subprocess.run(adb + ['shell', 'am', 'force-stop', args.package], check=True)
        subprocess.run(adb + ['logcat', '-c'], check=True)
        subprocess.run(adb + ['shell', 'monkey', '-p', args.package, '-c', 'android.intent.category.LAUNCHER', '1'],
                       check=True, capture_output=True)
        time.sleep(args.seconds)
        log = subprocess.run(adb + ['logcat', '-d'], check=True, capture_output=True, text=True).stdout
        subprocess.run(adb + ['shell', 'am', 'force-stop', args.package], check=True)
        return log

    return launch


def ios_launcher(args, work: Path) -> Callable[[str, int], str]:
    app = Path(args.app)

    def resign(target: Path, layout: Layout) -> None:
        sign = ['codesign', '--force', '--sign', args.identity]
        subprocess.run(sign + [str(layout.engine.parent)], check=True, capture_output=True)
        entitlements = []
        if args.entitlements:
            entitlements = ['--entitlements', args.entitlements]
        subprocess.run(sign + entitlements + [str(target)], check=True, capture_output=True)

    copies: dict[str, Path] = {'baseline': app, 'reinstalled': app}
    device = ['xcrun', 'devicectl', 'device']
    installed = {'variant': None}

    def launch(variant: str, run: int) -> str:
        if variant not in copies:
            copies[variant] = prepare_variant('ios', app, work, variant, resign)
            if variant == 'tampered-config':
                resign(copies[variant], desktop_layout('ios', copies[variant]))
        if installed['variant'] != variant:
            subprocess.run(device + ['uninstall', 'app', '--device', args.device, args.bundle_id], capture_output=True)
            subprocess.run(device + ['install', 'app', '--device', args.device, str(copies[variant])], check=True)
            installed['variant'] = variant
        output, _ = _run_capture(device + ['process', 'launch', '--device', args.device, '--console',
                                           '--terminate-existing', args.bundle_id], args.seconds)
        return output

    return launch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', required=True, type=Path, help='evidence JSON')
    parser.add_argument('--runs', type=int, default=5, help='baseline and reinstalled launches (startup cost)')
    parser.add_argument('--seconds', type=float, default=6.0, help='how long each launch runs')
    parser.add_argument('--work', type=Path, help='work directory for mutated copies (default: a temp dir)')
    sub = parser.add_subparsers(dest='command', required=True)
    desktop = sub.add_parser('desktop')
    desktop.add_argument('--platform', required=True, choices=('macos', 'linux', 'windows'))
    desktop.add_argument('--app', required=True, type=Path, help='.app, Linux bundle or Windows Release dir')
    desktop.add_argument('--executable', help='executable name inside the bundle')
    android = sub.add_parser('android')
    android.add_argument('--apk', required=True)
    android.add_argument('--package', required=True)
    android.add_argument('--abi', default='arm64-v8a')
    android.add_argument('--keystore', required=True, help='the release keystore (same key as the release)')
    android.add_argument('--ks-pass', help='apksigner --ks-pass value, e.g. env:KS_PASS')
    android.add_argument('--ks-key-alias')
    android.add_argument('--serial')
    android.add_argument('--adb', default='adb')
    android.add_argument('--android-sdk')
    ios = sub.add_parser('ios')
    ios.add_argument('--app', required=True, help='the signed Runner.app of the release')
    ios.add_argument('--bundle-id', required=True)
    ios.add_argument('--device', required=True, help='devicectl device identifier')
    ios.add_argument('--identity', required=True, help='codesign identity for the mutated copies')
    ios.add_argument('--entitlements', help='entitlements plist for re-signing the app')
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error('--runs must be at least 1')

    work = args.work or Path(tempfile.mkdtemp(prefix='hardened-lifecycle-'))
    work.mkdir(parents=True, exist_ok=True)
    try:
        if args.command == 'desktop':
            layout = desktop_layout(args.platform, args.app, args.executable)
            evidence = Evidence(args.platform, str(args.app))
            evidence.release = release_facts(layout.config.read_bytes())
            launcher = desktop_launcher(args.platform, args.app, work, args.seconds, args.executable)
        elif args.command == 'android':
            with zipfile.ZipFile(args.apk) as apk:
                config = apk.read('assets/flutter_assets/shorebird.yaml')
            evidence = Evidence('android', args.apk)
            evidence.release = release_facts(config)
            launcher = android_launcher(args, work)
        else:
            layout = desktop_layout('ios', Path(args.app))
            evidence = Evidence('ios', args.app)
            evidence.release = release_facts(layout.config.read_bytes())
            launcher = ios_launcher(args, work)
        result = run_lifecycle(evidence, args.runs, launcher)
    except (LifecycleError, subprocess.CalledProcessError, OSError, KeyError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    finally:
        if args.work is None:
            shutil.rmtree(work, ignore_errors=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'accepted': result['accepted'], 'failures': result['failures'],
                      'startup_cost': result['startup_cost']}, sort_keys=True))
    return 0 if result['accepted'] else 2


if __name__ == '__main__':
    sys.exit(main())
