#!/usr/bin/env python3
"""Provision, distribute and check every Open Shorebird release key in one command.

One private directory holds all operator key material and the signed P4
release-trust documents. This tool:

* creates every missing key in it, and never overwrites an existing one:
  - the release root (k-of-n Ed25519, contract v2, docs/HARDENING_CONTRACTS.md §10);
  - the `build-manifest` and `artifact-catalog` delegates of that root;
  - the shorebird-server self-update signing key (shorebird-server/docs/SELF_UPDATE.md);
* signs root version 1, delegations version 1 and the release-trust bundle
  with scripts/release_trust.py's rules, and computes the compiled pins;
* with --write-repo-pins, writes the public pins into the workspace files that
  carry them (release-pins/, the P2 artifact-catalog trust store, the server
  installer's pinned update keys);
* syncs the GitHub repository variables and secrets in the required order,
  only with --apply-github (default: a dry run that never prints a secret).
  Secret values reach `gh` on stdin only, never in its arguments.

Subcommands:
  init       create missing keys and trust documents (idempotent), then pins/GitHub
  rotate     planned rotation or revocation of one role (release_trust.py rules)
  renew      re-sign the current delegations with a later expiry
  sync       pins/GitHub only, from the private directory as it is
  status     report private directory, repository pins and GitHub (exit 0)
  verify     the same checks, exit 1 on any disagreement
  env        print build-machine environment lines (paths and key IDs only)

The private directory must be outside every git work tree, owned by you and
not accessible to group or others (mode 0700). Production and
DEVELOPMENT-ONLY material never mix: a directory is created as one or the
other, and --production refuses a development directory and every committed
DEVELOPMENT-ONLY key.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_catalog as ac  # noqa: E402
import release_trust as rt  # noqa: E402
from validate_hardening_contracts import canonical_json  # noqa: E402

WORKSPACE = Path(__file__).resolve().parents[1]
STATE_FILE = 'provision.json'
STATE_SCHEMA = 'open-shorebird.release-key-provisioning'
PINS_SCHEMA = 'open-shorebird.release-pins'
DEFAULT_REPO = 'tony-cloud/shorebird-workspace'

DELEGATE_ROLES = rt.ROLES  # build-manifest, artifact-catalog
SERVER_ROLE = 'server-update'
EXTERNAL_ROLES = (*DELEGATE_ROLES, SERVER_ROLE)
ALL_ROLES = ('root', *EXTERNAL_ROLES)

# GitHub names (docs/HARDENING_CONTRACTS.md §10.10, shorebird-server/docs/SELF_UPDATE.md).
VAR_TRUST = 'OPEN_SHOREBIRD_RELEASE_TRUST'
VAR_ROOT = 'OPEN_SHOREBIRD_RELEASE_ROOT'
SECRET_CATALOG = 'OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY'
VAR_SERVER_KEYS = 'SHOREBIRD_SERVER_UPDATE_PUBLIC_KEYS'
SECRET_SERVER = 'SHOREBIRD_SERVER_UPDATE_SIGNING_KEY'
SECRET_RELEASE_TOKEN = 'SHOREBIRD_SERVER_RELEASE_TOKEN'

# Committed, deliberately public keys. Production refuses every one of them.
DEVELOPMENT_KEY_FILES = {
    'root': rt.DEVELOPMENT_ROOT_KEYS[0],
    'build-manifest': rt.TESTDATA / 'build-manifest-1.DEVELOPMENT-ONLY.pem',
    'artifact-catalog': ac.DEVELOPMENT_KEY,
    SERVER_ROLE: WORKSPACE / 'shorebird-server' / 'signing' / 'server-update.DEVELOPMENT-ONLY.pem',
}
# shorebird-server/internal/updater.DevelopmentPublicKey (kept here so the
# refusal works without the submodule checked out).
SERVER_DEVELOPMENT_PUBLIC_KEY = 'a0ZEK+btwcAvqSxqsi4jT7YFIVj9AecHhANwfESYE4w='

ROOT_YEARS = 10
DELEGATIONS_YEARS = 2
EXPIRY_WARNING_DAYS = 90


class ProvisionError(Exception):
    """An operator-facing refusal; the message says what to do."""


# ---------------------------------------------------------------------------
# Small helpers


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def time_text(moment: dt.datetime) -> str:
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def years_from_now(years: int) -> str:
    moment = now()
    try:
        return time_text(moment.replace(year=moment.year + years))
    except ValueError:  # 29 February
        return time_text(moment.replace(year=moment.year + years, day=28))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def private_pem(seed: bytes) -> str:
    return f'-----BEGIN PRIVATE KEY-----\n{b64(ac._PKCS8_ED25519_PREFIX + seed)}\n-----END PRIVATE KEY-----\n'


def public_pem(public: bytes) -> str:
    return f'-----BEGIN PUBLIC KEY-----\n{b64(ac._SPKI_ED25519_PREFIX + public)}\n-----END PUBLIC KEY-----\n'


def development_public_keys() -> dict[str, str]:
    """{base64 public key: role} of every committed DEVELOPMENT-ONLY key."""
    keys = {SERVER_DEVELOPMENT_PUBLIC_KEY: SERVER_ROLE}
    for role, path in DEVELOPMENT_KEY_FILES.items():
        if path.is_file():
            keys[b64(ac.load_public_key(path))] = role
    return keys


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    if path.is_symlink():
        raise ProvisionError(f'refusing to replace the symlink {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_exclusive(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Creates `path`; an existing file is never replaced."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def pretty_json(document: Any) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + '\n').encode('utf-8')


# ---------------------------------------------------------------------------
# The private directory


def git_work_tree_containing(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / '.git').exists():
            return candidate
    return None


def check_private_dir(path: Path, *, create: bool) -> Path:
    """The resolved private directory, or a refusal explaining the fix."""
    path = path.expanduser()
    if path.is_symlink():
        raise ProvisionError(f'{path} is a symlink; pass the real directory')
    path = path.absolute()
    resolved = path.resolve()
    existing = resolved
    while not existing.exists():
        existing = existing.parent
    tree = git_work_tree_containing(existing)
    if tree is not None:
        raise ProvisionError(f'{resolved} is inside the git work tree {tree}; keep private keys outside every '
                             'repository (for example on an encrypted volume)')
    if resolved == WORKSPACE or WORKSPACE in resolved.parents:
        raise ProvisionError(f'{resolved} is inside the workspace {WORKSPACE}')
    if not resolved.exists():
        if not create:
            raise ProvisionError(f'{resolved} does not exist; run `init` first')
        if not resolved.parent.is_dir():
            raise ProvisionError(f'the parent directory {resolved.parent} does not exist')
        resolved.mkdir(mode=0o700)
    if not resolved.is_dir():
        raise ProvisionError(f'{resolved} is not a directory')
    info = resolved.stat()
    if info.st_uid != os.getuid():
        raise ProvisionError(f'{resolved} is owned by uid {info.st_uid}, not by you ({os.getuid()})')
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ProvisionError(f'{resolved} is accessible to group or others '
                             f'(mode {stat.S_IMODE(info.st_mode):04o}); run: chmod 700 {resolved}')
    return resolved


def check_key_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ProvisionError(f'{path} is missing or not a regular file; restore it from your backup '
                             '(a key that is already pinned or signed into trust cannot be recreated)')
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ProvisionError(f'{path} is readable by group or others (mode {mode:04o}); run: chmod 600 {path}')


class Store:
    """The private directory and its state file (public information only)."""

    def __init__(self, directory: Path, state: dict):
        self.dir = directory
        self.state = state

    # -- loading -----------------------------------------------------------

    @classmethod
    def open(cls, directory: Path, *, create: bool, mode: str | None) -> 'Store':
        if create and mode is None and not (directory.expanduser() / STATE_FILE).exists():
            raise ProvisionError('a new private directory needs --production or --development')
        directory = check_private_dir(directory, create=create)
        state_path = directory / STATE_FILE
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding='utf-8'))
            if state.get('schema') != STATE_SCHEMA or state.get('version') != 1:
                raise ProvisionError(f'{state_path} is not a provisioning state file')
            if mode is not None and state['mode'] != mode:
                if mode == 'production':
                    raise ProvisionError(f'{directory} holds DEVELOPMENT-ONLY material; production keys need a '
                                         'separate, new private directory')
                raise ProvisionError(f'{directory} holds production keys; use a separate directory for '
                                     'DEVELOPMENT-ONLY material')
        else:
            if not create:
                raise ProvisionError(f'{directory} has no {STATE_FILE}; run `init` first')
            if mode is None:
                raise ProvisionError('a new private directory needs --production or --development')
            leftovers = [p.name for p in directory.iterdir() if p.name not in ('.DS_Store',)]
            if leftovers:
                raise ProvisionError(f'{directory} is not empty and has no {STATE_FILE}; use an empty directory')
            state = {'schema': STATE_SCHEMA, 'version': 1, 'mode': mode, 'created_at': time_text(now()),
                     'keys': [], 'root': None, 'delegations': None, 'server_update_signer': None,
                     'github': {}}
        store = cls(directory, state)
        if store.production:
            store.refuse_development_material()
        return store

    @property
    def production(self) -> bool:
        return self.state['mode'] == 'production'

    def save(self) -> None:
        atomic_write(self.dir / STATE_FILE, pretty_json(self.state), 0o600)

    # -- keys --------------------------------------------------------------

    def keys(self, role: str, *, status: str = 'active') -> list[dict]:
        return [k for k in self.state['keys'] if k['role'] == role and k['status'] == status]

    def key(self, role: str) -> dict | None:
        active = self.keys(role)
        return active[-1] if active else None

    def key_by_id(self, kid: str) -> dict:
        for record in self.state['keys']:
            if record['key_id'] == kid:
                return record
        raise ProvisionError(f'no key {kid} in {self.dir}')

    def public(self, record: dict) -> bytes:
        return base64.b64decode(record['public_key'])

    def seed(self, record: dict) -> bytes:
        if record['file'] is None:
            raise ProvisionError(f'{record["name"]} is held by an external signer; this step needs a local key')
        path = self.dir / record['file']
        check_key_file(path)
        seed = ac.load_private_seed(path)
        if b64(ac.ed25519_public_key(seed)) != record['public_key']:
            raise ProvisionError(f'{path} does not hold {record["key_id"]}; restore the original file')
        return seed

    def refuse_development_material(self) -> None:
        development = development_public_keys()
        for record in self.state['keys']:
            if record['public_key'] in development or 'DEVELOPMENT-ONLY' in (record.get('file') or ''):
                raise ProvisionError(f'{record["name"]} is a DEVELOPMENT-ONLY key; production refuses it')

    def _next_name(self, role: str) -> str:
        used = [k['name'] for k in self.state['keys'] if k['role'] == role]
        number = len(used) + 1
        while f'{role}-{number}' in used:
            number += 1
        return f'{role}-{number}'

    def create_key(self, role: str, *, external_public: Path | None = None) -> dict:
        """Creates (or records an external) key. An existing file is never replaced."""
        name = self._next_name(role)
        suffix = '' if self.production else '.DEVELOPMENT-ONLY'
        record = {'name': name, 'role': role, 'status': 'active', 'created_at': time_text(now()),
                  'file': None, 'public_file': None, 'revoked': False}
        if external_public is not None:
            public = ac.load_public_key(external_public)
            if '-----BEGIN PRIVATE KEY-----' in external_public.read_text(encoding='ascii'):
                raise ProvisionError(f'{external_public} holds a private key; an external signer role takes '
                                     'only its public PEM')
            relative = f'public/{name}{suffix}.pub.pem'
            write_exclusive(self.dir / relative, public_pem(public).encode('ascii'), 0o644)
            record['public_file'] = relative
        else:
            committed = DEVELOPMENT_KEY_FILES.get(role)
            if (not self.production and not self.keys(role) and not self.keys(role, status='retired')
                    and committed is not None and committed.is_file()):
                # Development directories start from the committed public test
                # keys, so every verifier recognizes them as DEVELOPMENT-ONLY.
                seed = ac.load_private_seed(committed)
            else:
                seed = secrets.token_bytes(32)
            public = ac.ed25519_public_key(seed)
            relative = f'keys/{name}{suffix}.pem'
            write_exclusive(self.dir / relative, private_pem(seed).encode('ascii'), 0o600)
            record['file'] = relative
        record['key_id'] = ac.key_id(public)
        record['public_key'] = b64(public)
        if self.production and record['public_key'] in development_public_keys():
            (self.dir / (record['file'] or record['public_file'])).unlink()
            raise ProvisionError(f'{external_public} is a DEVELOPMENT-ONLY key; production refuses it')
        if any(k['key_id'] == record['key_id'] for k in self.state['keys']):
            (self.dir / (record['file'] or record['public_file'])).unlink()
            raise ProvisionError(f'{record["key_id"]} is already recorded; a key belongs to one role, once')
        self.state['keys'].append(record)
        self.save()
        print(f'created {name}{" (external signer)" if external_public else ""}: {record["key_id"]}')
        return record

    def retire(self, record: dict, *, revoked: bool) -> None:
        record['status'] = 'retired'
        record['revoked'] = revoked
        record['retired_at'] = time_text(now())
        if record['file']:
            source = self.dir / record['file']
            target = self.dir / 'retired' / Path(record['file']).name
            target.parent.mkdir(mode=0o700, exist_ok=True)
            if target.exists():
                raise ProvisionError(f'{target} already exists')
            os.replace(source, target)
            record['file'] = str(target.relative_to(self.dir))

    # -- trust documents ---------------------------------------------------

    def trust_path(self, name: str) -> Path:
        return self.dir / 'trust' / name

    def load_document(self, name: str) -> dict:
        path = self.trust_path(name)
        if not path.is_file():
            raise ProvisionError(f'{path} is missing; restore it from your backup')
        return rt._load(path)

    def root_envelope(self, version: int) -> dict:
        return self.load_document(f'root.v{version}.json')

    def root_payload(self, version: int) -> dict:
        data, _ = rt.open_envelope(self.root_envelope(version), f'root.v{version}')
        return rt.parse_payload(data, 'release-root')

    def delegations_envelope(self) -> dict:
        return self.load_document(f'delegations.v{self.state["delegations"]["version"]}.json')

    def bundle(self) -> dict:
        chain = [self.root_envelope(v) for v in range(1, self.state['root']['version'] + 1)]
        return rt.bundle(chain, self.delegations_envelope())

    def pin_bytes(self) -> bytes:
        data, _ = rt.open_envelope(self.root_envelope(self.state['root']['pinned_version']), 'pinned root')
        return data


# ---------------------------------------------------------------------------
# Signing (release_trust.py rules)


def sign_root_version(store: Store, *, version: int, root_keys: list[dict], threshold: int, expires: str,
                      previous_keys: list[dict] = ()) -> None:
    payload = rt.root_payload([store.public(k) for k in root_keys], version=version, threshold=threshold,
                              expires_at=expires)
    previous = store.root_payload(version - 1) if version > 1 else None
    document = rt.sign_root(payload, [store.seed(k) for k in root_keys], previous,
                            [store.seed(k) for k in previous_keys])
    path = store.trust_path(f'root.v{version}.json')
    if path.exists():
        raise ProvisionError(f'{path} already exists; it is never re-signed')
    write_exclusive(path, pretty_json(document), 0o644)
    print(f'signed release root version {version} ({threshold}-of-{len(root_keys)})')


def sign_delegations_version(store: Store, *, version: int, expires: str, revoke: list[str] = ()) -> None:
    root_version = store.state['root']['version']
    root = store.root_payload(root_version)
    issued = time_text(now())
    roles = {role: [rt.delegated_key(store.public(k), issued, expires) for k in store.keys(role)]
             for role in DELEGATE_ROLES}
    previous = None
    revoked = list(revoke)
    if version > 1:
        data, _ = rt.open_envelope(store.load_document(f'delegations.v{version - 1}.json'), 'delegations')
        previous = rt.parse_payload(data, 'release-delegations')
        revoked += previous['revoked_key_ids']
    payload = rt.delegations_payload(version=version, root_version=root_version, issued_at=issued,
                                     expires_at=expires, roles=roles, revoked=revoked)
    root_keys = [store.key_by_id(k['key_id']) for k in root['keys']]
    seeds = [store.seed(k) for k in root_keys if k['file'] and (store.dir / k['file']).exists()]
    document = rt.sign_delegations(payload, root, seeds, previous)
    path = store.trust_path(f'delegations.v{version}.json')
    if path.exists():
        raise ProvisionError(f'{path} already exists; it is never re-signed')
    write_exclusive(path, pretty_json(document), 0o644)
    store.state['delegations'] = dict(store.state['delegations'] or {}, version=version, issued_at=issued,
                                      expires_at=expires)
    store.state['delegations'].setdefault('min_version', 1)
    print(f'signed delegations version {version} under root version {root_version}')


def write_bundle(store: Store) -> dict:
    document = store.bundle()
    rt.verify_bundle(document, store.pin_bytes(), store.state['delegations']['min_version'],
                     at=time_text(now()), production=store.production)
    path = store.trust_path('release-trust.json')
    data = pretty_json(document)
    if not path.exists() or path.read_bytes() != data:
        atomic_write(path, data, 0o644)
        print(f'wrote {path}')
    return document


def delegated_ids(store: Store) -> dict[str, list[str]]:
    data, _ = rt.open_envelope(store.delegations_envelope(), 'delegations')
    payload = rt.parse_payload(data, 'release-delegations')
    return {role: sorted(entry['key_id'] for entry in payload['roles'][role]) for role in DELEGATE_ROLES}


# ---------------------------------------------------------------------------
# init


def parse_external(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        role, separator, path = value.partition('=')
        if not separator or role not in EXTERNAL_ROLES or not path:
            raise ProvisionError(f'--external expects ROLE=PUBLIC_PEM with ROLE in {", ".join(EXTERNAL_ROLES)}')
        if role in result:
            raise ProvisionError(f'--external {role} given twice')
        result[role] = Path(path)
    return result


def command_init(args: argparse.Namespace) -> int:
    mode = 'production' if args.production else 'development' if args.development else None
    store = Store.open(args.private_dir, create=True, mode=mode)
    external = parse_external(args.external)
    if external and not store.production:
        raise ProvisionError('--external (KMS/HSM signers) is a production feature')
    state = store.state

    # 1. Keys. A key that signed or is pinned must exist; others are created.
    if state['root'] is None:
        roots = store.keys('root')
        count = args.root_keys if args.root_keys is not None else 1
        if count < 1 or count > rt.MAX_KEYS:
            raise ProvisionError(f'--root-keys must be 1..{rt.MAX_KEYS}')
        while len(roots) < count:
            roots.append(store.create_key('root'))
    elif args.root_keys is not None and args.root_keys != len(store.keys('root')):
        raise ProvisionError('the root already exists; change its keys with `rotate root`')
    for role in EXTERNAL_ROLES:
        current = store.key(role)
        if current is None:
            store.create_key(role, external_public=external.get(role))
        elif role in external:
            if current['public_file'] is None or ac.load_public_key(external[role]) != store.public(current):
                raise ProvisionError(f'{role} already has key {current["key_id"]}; '
                                     f'replace it with `rotate {role} --external PUB`')
    for record in [*store.keys('root'), *(k for r in EXTERNAL_ROLES for k in store.keys(r))]:
        if record['file']:
            store.seed(record)  # exists, private, matches its record
        elif not (store.dir / record['public_file']).is_file():
            raise ProvisionError(f'{store.dir / record["public_file"]} is missing; restore it')

    # 2. Root version 1, delegations version 1, bundle.
    if state['root'] is None:
        roots = store.keys('root')
        threshold = args.threshold if args.threshold is not None else len(roots) // 2 + 1
        if not 1 <= threshold <= len(roots):
            raise ProvisionError(f'--threshold must be 1..{len(roots)}')
        expires = args.root_expires or years_from_now(ROOT_YEARS)
        rt.parse_time(expires)
        sign_root_version(store, version=1, root_keys=roots, threshold=threshold, expires=expires)
        state['root'] = {'version': 1, 'pinned_version': 1, 'threshold': threshold, 'expires_at': expires}
        store.save()
    if state['delegations'] is None:
        expires = args.delegations_expires or years_from_now(DELEGATIONS_YEARS)
        rt.parse_time(expires)
        sign_delegations_version(store, version=1, expires=expires)
        store.save()
    else:
        expected = {role: sorted(k['key_id'] for k in store.keys(role)) for role in DELEGATE_ROLES}
        if delegated_ids(store) != expected:
            raise ProvisionError('the signed delegations name other keys than the private directory; '
                                 'run `rotate` for the role that changed')
    if state['server_update_signer'] is None:
        state['server_update_signer'] = store.key(SERVER_ROLE)['key_id']
        store.save()
    write_bundle(store)
    return publish(store, args)


# ---------------------------------------------------------------------------
# rotate / renew


def command_rotate(args: argparse.Namespace) -> int:
    store = Store.open(args.private_dir, create=False, mode=None)
    if store.state['root'] is None or store.state['delegations'] is None:
        raise ProvisionError('run `init` first')
    state = store.state
    external = Path(args.external) if args.external else None
    if external and not store.production:
        raise ProvisionError('--external (KMS/HSM signers) is a production feature')
    expires = args.delegations_expires or years_from_now(DELEGATIONS_YEARS)
    rt.parse_time(expires)
    next_delegations = state['delegations']['version'] + 1

    if args.role in DELEGATE_ROLES:
        if args.stage:
            raise ProvisionError('--stage applies to server-update only')
        old = store.key(args.role)
        new = store.create_key(args.role, external_public=external)
        try:
            store.retire(old, revoked=args.revoke)
            sign_delegations_version(store, version=next_delegations, expires=expires,
                                     revoke=[old['key_id']] if args.revoke else [])
        except Exception:
            print(f'error: rotation incomplete; {new["name"]} was created but not delegated', file=sys.stderr)
            raise
        if args.raise_floor:
            state['delegations']['min_version'] = next_delegations
        print(f'{args.role}: {old["key_id"]} -> {new["key_id"]}' + (' (revoked)' if args.revoke else ''))
    elif args.role == 'root':
        if args.revoke or args.raise_floor or external:
            raise ProvisionError('root rotation takes --root-keys/--threshold/--root-expires only')
        old_keys = store.keys('root')
        count = args.root_keys or len(old_keys)
        threshold = args.threshold or count // 2 + 1
        if not 1 <= threshold <= count:
            raise ProvisionError(f'--threshold must be 1..{count}')
        root_expires = args.root_expires or years_from_now(ROOT_YEARS)
        rt.parse_time(root_expires)
        new_keys = [store.create_key('root') for _ in range(count)]
        version = state['root']['version'] + 1
        sign_root_version(store, version=version, root_keys=new_keys, threshold=threshold, expires=root_expires,
                          previous_keys=old_keys)
        state['root'].update(version=version, threshold=threshold, expires_at=root_expires)
        if args.repin:
            state['root']['pinned_version'] = version
        store.save()
        sign_delegations_version(store, version=next_delegations, expires=expires)
        for record in old_keys:
            store.retire(record, revoked=False)
        print(f'root: version {version}; pinned version {state["root"]["pinned_version"]}')
    else:  # server-update: add, then activate, then retire (docs/SELF_UPDATE.md, "Rotation")
        if args.revoke or args.raise_floor:
            raise ProvisionError('server-update rotation uses --stage add|activate|retire')
        active = store.keys(SERVER_ROLE)
        signer = state['server_update_signer']
        if args.stage == 'add':
            if len(active) != 1:
                raise ProvisionError('a server-update rotation is already in progress; run --stage activate/retire')
            new = store.create_key(SERVER_ROLE, external_public=external)
            print(f'server-update: pinned keys are now {active[0]["key_id"]} and {new["key_id"]}; '
                  'publish a release (still signed by the old key) before --stage activate')
        elif args.stage == 'activate':
            if len(active) != 2 or active[-1]['key_id'] == signer:
                raise ProvisionError('run --stage add first')
            state['server_update_signer'] = active[-1]['key_id']
            print(f'server-update: releases are now signed by {active[-1]["key_id"]}')
        elif args.stage == 'retire':
            old = [k for k in active if k['key_id'] != signer]
            if len(active) != 2 or not old:
                raise ProvisionError('run --stage add and --stage activate first')
            store.retire(old[0], revoked=False)
            print(f'server-update: {old[0]["key_id"]} is no longer pinned')
        else:
            raise ProvisionError('server-update rotation needs --stage add|activate|retire')
    store.save()
    write_bundle(store)
    return publish(store, args)


def command_renew(args: argparse.Namespace) -> int:
    store = Store.open(args.private_dir, create=False, mode=None)
    if store.state['delegations'] is None:
        raise ProvisionError('run `init` first')
    expires = args.delegations_expires or years_from_now(DELEGATIONS_YEARS)
    rt.parse_time(expires)
    sign_delegations_version(store, version=store.state['delegations']['version'] + 1, expires=expires)
    store.save()
    write_bundle(store)
    return publish(store, args)


def command_sync(args: argparse.Namespace) -> int:
    store = Store.open(args.private_dir, create=False, mode=None)
    if store.state['delegations'] is None:
        raise ProvisionError('run `init` first')
    write_bundle(store)
    return publish(store, args)


# ---------------------------------------------------------------------------
# Public pins and repository files


def compute_pins(store: Store) -> dict:
    pin = store.pin_bytes()
    root = rt.parse_payload(pin, 'release-root')
    delegations = store.delegations_envelope()
    data, _ = rt.open_envelope(delegations, 'delegations')
    min_version = store.state['delegations']['min_version']
    development = (not store.production) or bool(rt.development_root_key_ids() & {k['key_id'] for k in root['keys']})
    return {
        'schema': PINS_SCHEMA,
        'version': 1,
        'development': development,
        'gn_args': {
            'shorebird_release_root': b64(pin),
            'shorebird_release_min_delegations_version': min_version,
        },
        'toolchain_security': {
            'contract_version': 2,
            'release_root': b64(pin),
            'min_delegations_version': min_version,
        },
        'release_root_sha256': 'sha256:' + hashlib.sha256(pin).hexdigest(),
        'release_root_version': root['version'],
        'delegations_version': store.state['delegations']['version'],
        'delegations_sha256': hashlib.sha256(data).hexdigest(),
        'delegations_expires_at': store.state['delegations']['expires_at'],
        'build_manifest_key_ids': sorted(k['key_id'] for k in store.keys('build-manifest')),
        'artifact_catalog_key_ids': sorted(k['key_id'] for k in store.keys('artifact-catalog')),
        'server_update_public_keys': server_public_keys(store),
        'server_update_signer': store.state['server_update_signer'],
    }


def server_public_keys(store: Store) -> list[str]:
    return [k['public_key'] for k in store.keys(SERVER_ROLE)]


def gn_text(pins: dict) -> str:
    gn = pins['gn_args']
    return ('# Generated by scripts/provision_release_keys.py; public values only.\n'
            f'shorebird_release_root = "{gn["shorebird_release_root"]}"\n'
            f'shorebird_release_min_delegations_version = {gn["shorebird_release_min_delegations_version"]}\n')


def server_keys_text(pins: dict) -> str:
    return ('# Production shorebird-server self-update public keys (base64 Ed25519), one per line.\n'
            '# Generated by shorebird-workspace scripts/provision_release_keys.py; the installer pins these.\n'
            + ''.join(f'{key}\n' for key in pins['server_update_public_keys']))


class RepoFiles:
    def __init__(self, workspace: Path, trust_store: Path | None = None):
        self.workspace = workspace
        self.pins_dir = workspace / 'release-pins'
        self.pins_json = self.pins_dir / 'release-pins.json'
        self.gn = self.pins_dir / 'release-root.gn'
        self.bundle = self.pins_dir / 'release-trust.json'
        self.trust_store = trust_store or workspace / 'flutter' / 'bin' / 'internal' / 'shorebird_artifact_trust.json'
        self.server = workspace / 'shorebird-server'
        self.server_keys = self.server / 'signing' / 'server-update.trusted-keys'
        self.installer_builder = self.server / 'deploy' / 'linux' / 'build_installer.py'


def desired_trust_store(store: Store, current: dict) -> dict:
    usage = 'production' if store.production else 'development'
    wanted = {k['key_id']: k for k in store.keys('artifact-catalog')}
    revoked = {k['key_id'] for k in store.keys('artifact-catalog', status='retired') if k['revoked']}
    keys = [entry for entry in current['keys'] if entry['key_id'] not in revoked]
    present = {entry['key_id'] for entry in keys}
    for kid, record in wanted.items():
        if kid not in present:
            keys.append(ac.trust_entry(store.public(record), usage,
                                       f'Open Shorebird {usage} artifact-catalog signer ({record["name"]}), '
                                       'the artifact-catalog delegate of the release root; added by '
                                       'scripts/provision_release_keys.py'))
    return dict(current, keys=keys)


def repo_changes(store: Store, files: RepoFiles, pins: dict, bundle: dict) -> list[tuple[Path, bytes]]:
    """(path, desired bytes) for every repository file whose content differs."""
    if files.pins_json.is_file():
        existing = json.loads(files.pins_json.read_text(encoding='utf-8'))
        if store.production is False and existing.get('development') is False:
            raise ProvisionError(f'{files.pins_json} holds production pins; DEVELOPMENT-ONLY pins never replace them')
    wanted: list[tuple[Path, bytes]] = [
        (files.pins_json, pretty_json(pins)),
        (files.gn, gn_text(pins).encode('ascii')),
        (files.bundle, pretty_json(bundle)),
    ]
    if files.trust_store.is_file():
        current = json.loads(files.trust_store.read_text(encoding='utf-8'))
        desired = desired_trust_store(store, current)
        errors = ac.check_trust_store(desired)
        if errors:
            raise ProvisionError(f'{files.trust_store}: ' + '; '.join(errors))
        if desired != current:
            wanted.append((files.trust_store, (json.dumps(desired, indent=2) + '\n').encode('utf-8')))
    else:
        print(f'note: {files.trust_store} is not checked out; the artifact-catalog trust entry is not written')
    if store.production:
        if files.server.is_dir():
            wanted.append((files.server_keys, server_keys_text(pins).encode('ascii')))
        else:
            print(f'note: {files.server} is not checked out; the server installer key pin is not written')
    else:
        print('note: DEVELOPMENT-ONLY server-update keys are never pinned into the server installer')
    return [(path, data) for path, data in wanted if not path.is_file() or path.read_bytes() != data]


def rebuild_installer(files: RepoFiles, *, check: bool) -> bool:
    if not files.installer_builder.is_file():
        return True
    command = [sys.executable, str(files.installer_builder)] + (['--check'] if check else [])
    result = subprocess.run(command, capture_output=True, text=True)
    if not check and result.returncode != 0:
        raise ProvisionError(f'{files.installer_builder} failed: {result.stderr.strip()}')
    return result.returncode == 0


def publish(store: Store, args: argparse.Namespace) -> int:
    """Repository pins and GitHub, both explicit; prints the plan otherwise."""
    pins = compute_pins(store)
    bundle = json.loads(store.trust_path('release-trust.json').read_bytes())
    files = RepoFiles(args.workspace, args.trust_store)
    changes = repo_changes(store, files, pins, bundle)
    if not changes:
        print('repository pins: up to date')
    for path, data in changes:
        if args.write_repo_pins:
            atomic_write(path, data, 0o644)
            print(f'repository pins: wrote {path}')
        else:
            print(f'repository pins: would write {path} (pass --write-repo-pins)')
    if args.write_repo_pins and any(path == files.server_keys for path, _ in changes):
        rebuild_installer(files, check=False)
        print(f'repository pins: rebuilt {files.installer_builder.parent / "install.sh"}')
    if changes and args.write_repo_pins:
        print('repository pins: review and commit these files (the trust store lives in the flutter '
              'submodule, the installer pin in shorebird-server)')
    status = github_sync(store, args, pins, bundle)
    print_next_steps(store, pins)
    return status


# ---------------------------------------------------------------------------
# GitHub


class GitHub:
    """`gh` with every value on stdin, never in argv."""

    def __init__(self, repo: str):
        self.repo = repo
        self.executable = shutil.which('gh')

    def _run(self, arguments: list[str], stdin: bytes | None = None) -> str:
        if self.executable is None:
            raise ProvisionError('the GitHub CLI `gh` is not on PATH')
        result = subprocess.run([self.executable, *arguments, '--repo', self.repo], input=stdin,
                                capture_output=True)
        if result.returncode != 0:
            message = result.stderr.decode('utf-8', 'replace').strip()
            raise ProvisionError(f'gh {" ".join(arguments[:2])} failed: {message}')
        return result.stdout.decode('utf-8')

    def variables(self) -> dict[str, str]:
        return {item['name']: item['value'] for item in json.loads(self._run(['variable', 'list', '--json', 'name,value']) or '[]')}

    def secrets(self) -> set[str]:
        return {item['name'] for item in json.loads(self._run(['secret', 'list', '--json', 'name']) or '[]')}

    def set_variable(self, name: str, value: str) -> None:
        self._run(['variable', 'set', name], value.encode('utf-8'))

    def set_secret(self, name: str, value: bytes) -> None:
        self._run(['secret', 'set', name], value)


def same_variable(name: str, left: str | None, right: str) -> bool:
    if left is None:
        return False
    if name == VAR_TRUST:
        try:
            return json.loads(left) == json.loads(right)
        except json.JSONDecodeError:
            return False
    if name == VAR_SERVER_KEYS:
        return [k.strip() for k in left.split(',') if k.strip()] == right.split(',')
    return left.strip() == right


def github_plan(store: Store, pins: dict, bundle: dict, token_file: Path | None) -> list[dict]:
    """Ordered: release trust variables before the catalog secret; server keys before its secret."""
    catalog = store.key('artifact-catalog')
    signer = store.key_by_id(store.state['server_update_signer'])
    plan = [
        {'kind': 'variable', 'name': VAR_TRUST, 'value': json.dumps(bundle, sort_keys=True, separators=(',', ':'))},
        {'kind': 'variable', 'name': VAR_ROOT, 'value': pins['gn_args']['shorebird_release_root']},
        {'kind': 'secret', 'name': SECRET_CATALOG, 'record': catalog, 'after': [VAR_TRUST, VAR_ROOT]},
        {'kind': 'variable', 'name': VAR_SERVER_KEYS, 'value': ','.join(pins['server_update_public_keys'])},
        {'kind': 'secret', 'name': SECRET_SERVER, 'record': signer, 'after': [VAR_SERVER_KEYS]},
    ]
    if token_file is not None:
        plan.append({'kind': 'token', 'name': SECRET_RELEASE_TOKEN, 'file': token_file, 'after': []})
    return plan


def github_sync(store: Store, args: argparse.Namespace, pins: dict, bundle: dict) -> int:
    repo = args.repo
    if args.skip_github:
        return 0
    if args.apply_github and not store.production and repo == DEFAULT_REPO:
        raise ProvisionError(f'DEVELOPMENT-ONLY material is never synced to {DEFAULT_REPO}; pass --repo for a fork')
    token_file = Path(args.server_release_token_file) if args.server_release_token_file else None
    if token_file is not None:
        check_key_file(token_file)
    github = GitHub(repo)
    try:
        variables, existing_secrets = github.variables(), github.secrets()
        known = True
    except ProvisionError as error:
        if args.apply_github:
            raise
        print(f'github {repo}: state unknown ({error}); showing the plan only')
        variables, existing_secrets, known = {}, set(), False
    record = store.state['github'].setdefault(repo, {'variables': {}, 'secrets': {}})
    verb = 'apply' if args.apply_github else 'dry run'
    print(f'github {repo} ({verb}):')
    failed = False
    done: dict[str, bool] = {}
    for step, action in enumerate(github_plan(store, pins, bundle, token_file), 1):
        name = action['name']
        label = f'  {step}. {action["kind"]:8} {name}'
        if any(not done.get(dependency, False) for dependency in action.get('after', [])):
            print(f'{label}: skipped (an earlier step it depends on did not complete)')
            failed = True
            done[name] = False
            continue
        if action['kind'] == 'variable':
            value = action['value']
            current = variables.get(name)
            digest = f'sha256 {sha256_text(value)[:16]}, {len(value)} bytes'
            if same_variable(name, current, value):
                print(f'{label}: unchanged ({digest})')
                record['variables'][name] = sha256_text(value)
                done[name] = True
                continue
            owned = current is not None and record['variables'].get(name) == sha256_text(current)
            if current is not None and not owned and not args.force_github:
                print(f'{label}: REFUSED: GitHub holds a different value this private directory did not set '
                      '(review it; --force-github replaces it)')
                failed = True
                done[name] = False
                continue
            change = 'create' if current is None else 'update'
            if not args.apply_github:
                print(f'{label}: would {change} ({digest})')
                done[name] = True
                continue
            github.set_variable(name, value)
            if not same_variable(name, github.variables().get(name), value):
                raise ProvisionError(f'{name} did not read back as set; stopping before any secret')
            record['variables'][name] = sha256_text(value)
            store.save()
            print(f'{label}: {change}d ({digest})')
            done[name] = True
        else:
            if action['kind'] == 'secret':
                key = action['record']
                if key['file'] is None:
                    print(f'{label}: not managed ({key["name"]} is held by an external signer; '
                          'configure CI to use it)')
                    done[name] = True
                    continue
                fingerprint = key['key_id']
            else:
                fingerprint = 'token-file'
            present = name in existing_secrets
            if present and record['secrets'].get(name) == fingerprint and action['kind'] == 'secret':
                print(f'{label}: unchanged (holds {fingerprint})')
                done[name] = True
                continue
            if present and name not in record['secrets'] and not args.force_github:
                print(f'{label}: REFUSED: the secret exists and this private directory did not set it '
                      '(--force-github replaces it)')
                failed = True
                done[name] = False
                continue
            change = 'create' if not present else 'update'
            what = f'key {fingerprint}' if action['kind'] == 'secret' else f'contents of {action["file"]}'
            if not args.apply_github:
                print(f'{label}: would {change} ({what}, value via stdin, never printed)')
                done[name] = True
                continue
            if action['kind'] == 'secret':
                value = (store.dir / key['file']).read_bytes()
                store.seed(key)
            else:
                value = action['file'].read_bytes().strip()
                if not value:
                    raise ProvisionError(f'{action["file"]} is empty')
            github.set_secret(name, value)
            if name not in github.secrets():
                raise ProvisionError(f'{name} is not listed after setting it')
            record['secrets'][name] = fingerprint
            store.save()
            print(f'{label}: {change}d ({what})')
            done[name] = True
    if not args.apply_github:
        print(f'github {repo}: nothing was changed; pass --apply-github to apply' +
              ('' if known else ' (and make `gh` work first)'))
    store.save()
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# status / verify


class Report:
    def __init__(self):
        self.problems = 0
        self.warnings = 0

    def ok(self, text: str) -> None:
        print(f'  ok    {text}')

    def warn(self, text: str) -> None:
        self.warnings += 1
        print(f'  warn  {text}')

    def bad(self, text: str) -> None:
        self.problems += 1
        print(f'  FAIL  {text}')


def check_private(store: Store, report: Report) -> dict | None:
    print(f'private directory {store.dir} ({store.state["mode"]})')
    report.ok('outside every git work tree, owned by you, mode 0700')
    for record in store.state['keys']:
        if record['status'] != 'active':
            continue
        try:
            if record['file']:
                store.seed(record)
                report.ok(f'{record["name"]}: {record["key_id"]}')
            else:
                if ac.load_public_key(store.dir / record['public_file']) != store.public(record):
                    raise ProvisionError(f'{record["public_file"]} does not hold {record["key_id"]}')
                report.ok(f'{record["name"]}: {record["key_id"]} (external signer)')
        except (ProvisionError, ac.CatalogError, OSError) as error:
            report.bad(f'{record["name"]}: {error}')
    if store.state['root'] is None or store.state['delegations'] is None:
        report.bad('no signed root/delegations; run `init`')
        return None
    try:
        bundle = store.bundle()
        trust = rt.verify_bundle(bundle, store.pin_bytes(), store.state['delegations']['min_version'],
                                 production=store.production)
        report.ok(f'release trust verifies: root version {trust.root["version"]} (pinned version '
                  f'{store.state["root"]["pinned_version"]}), delegations version {trust.delegations["version"]}')
        if store.production and trust.root_key_ids & rt.development_root_key_ids():
            report.bad('the root contains a DEVELOPMENT-ONLY key')
        expected = {role: sorted(k['key_id'] for k in store.keys(role)) for role in DELEGATE_ROLES}
        if delegated_ids(store) != expected:
            report.bad('the delegations name other keys than the private directory')
        moment = now()
        for what, when in (('root', trust.root['expires_at']), ('delegations', trust.delegations['expires_at'])):
            remaining = rt.parse_time(when) - moment
            if remaining.total_seconds() <= 0:
                report.bad(f'{what} expired at {when}; run `renew` (delegations) or `rotate root`')
            elif remaining < dt.timedelta(days=EXPIRY_WARNING_DAYS):
                report.warn(f'{what} expires at {when}')
        saved = store.trust_path('release-trust.json')
        if not saved.is_file() or json.loads(saved.read_bytes()) != bundle:
            report.bad(f'{saved} is stale; run `sync`')
    except (rt.TrustError, ProvisionError, ac.CatalogError, OSError) as error:
        report.bad(f'release trust: {error}')
        return None
    return compute_pins(store)


def check_repo(store: Store, files: RepoFiles, pins: dict, report: Report) -> None:
    print(f'repository pins in {files.workspace}')
    if not files.pins_json.is_file():
        report.bad(f'{files.pins_json} is missing; run with --write-repo-pins')
        return
    try:
        bundle = json.loads(store.trust_path('release-trust.json').read_bytes())
        changes = {path for path, _ in repo_changes(store, files, pins, bundle)}
    except ProvisionError as error:
        report.bad(str(error))
        return
    for path in (files.pins_json, files.gn, files.bundle, files.trust_store,
                 *([files.server_keys] if store.production and files.server.is_dir() else [])):
        if path in changes:
            report.bad(f'{path} differs from the private directory; run with --write-repo-pins')
        elif path.is_file():
            report.ok(f'{path.relative_to(files.workspace) if files.workspace in path.parents else path}')
    if store.production and files.installer_builder.is_file():
        if rebuild_installer(files, check=True):
            report.ok('shorebird-server installer embeds the pinned update keys')
        else:
            report.bad('shorebird-server/deploy/linux/install.sh is stale; run with --write-repo-pins')


def check_github(store: Store, repo: str, pins: dict, report: Report) -> None:
    print(f'github {repo}')
    github = GitHub(repo)
    try:
        variables, existing = github.variables(), github.secrets()
    except ProvisionError as error:
        report.bad(str(error))
        return
    bundle = json.loads(store.trust_path('release-trust.json').read_bytes())
    for action in github_plan(store, pins, bundle, None):
        name = action['name']
        if action['kind'] == 'variable':
            if same_variable(name, variables.get(name), action['value']):
                report.ok(f'variable {name} matches')
            elif name not in variables:
                report.bad(f'variable {name} is not set')
            else:
                report.bad(f'variable {name} differs from the private directory')
        else:
            if action['record']['file'] is None:
                report.ok(f'secret {name}: not managed (external signer)')
            elif name in existing:
                recorded = store.state['github'].get(repo, {}).get('secrets', {}).get(name)
                if recorded is not None and recorded != action['record']['key_id']:
                    report.bad(f'secret {name} was last set to {recorded}, not {action["record"]["key_id"]}')
                else:
                    report.ok(f'secret {name} exists' + (f' (set to {recorded})' if recorded else ''))
            else:
                report.bad(f'secret {name} is not set')
    if SECRET_RELEASE_TOKEN in existing:
        report.ok(f'secret {SECRET_RELEASE_TOKEN} exists')
    else:
        report.warn(f'secret {SECRET_RELEASE_TOKEN} is not set (needed only to publish server releases)')


def command_status(args: argparse.Namespace, *, strict: bool) -> int:
    store = Store.open(args.private_dir, create=False, mode=None)
    report = Report()
    pins = check_private(store, report)
    if pins is not None:
        if not args.skip_repo:
            check_repo(store, RepoFiles(args.workspace, args.trust_store), pins, report)
        if not args.skip_github:
            check_github(store, args.repo, pins, report)
    print(f'{report.problems} problem(s), {report.warnings} warning(s)')
    return 1 if strict and report.problems else 0


# ---------------------------------------------------------------------------
# Build-machine environment and next steps


def command_env(args: argparse.Namespace) -> int:
    store = Store.open(args.private_dir, create=False, mode=None)
    if store.state['delegations'] is None:
        raise ProvisionError('run `init` first')
    manifest = store.key('build-manifest')
    print('# Build machine (docs/HARDENING_CONTRACTS.md §10.5); paths only, no key material.')
    print(f'export SHOREBIRD_RELEASE_TRUST={store.trust_path("release-trust.json")}')
    if manifest['file']:
        print(f'export SHOREBIRD_BUILD_MANIFEST_SIGNING_KEY={store.dir / manifest["file"]}')
    else:
        print('export SHOREBIRD_BUILD_MANIFEST_SIGNER=/path/to/your-kms-signer  # `<signer> sign --key-id ID --message M`')
        print(f'export SHOREBIRD_BUILD_MANIFEST_SIGNER_KEY_ID={manifest["key_id"]}')
    return 0


def print_next_steps(store: Store, pins: dict) -> None:
    if pins['development']:
        print('mode: DEVELOPMENT-ONLY (never ship these pins)')
    print(f'pin: root {pins["release_root_sha256"]} version {pins["release_root_version"]}, '
          f'delegations version {pins["delegations_version"]} (floor {pins["gn_args"]["shorebird_release_min_delegations_version"]})')
    print(f'build machines: eval "$(scripts/provision_release_keys.sh env --private-dir {store.dir})"')


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.split('\n\n', 1)[1])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--private-dir', type=Path, required=True,
                        help='operator key directory: outside every git work tree, mode 0700')
    common.add_argument('--workspace', type=Path, default=WORKSPACE, help=argparse.SUPPRESS)
    common.add_argument('--trust-store', type=Path, default=None, help=argparse.SUPPRESS)
    common.add_argument('--repo', default=os.environ.get('OPEN_SHOREBIRD_PROVISION_REPO', DEFAULT_REPO),
                        help=f'GitHub repository for variables and secrets (default {DEFAULT_REPO})')
    common.add_argument('--skip-github', action='store_true', help='do not contact GitHub at all')

    publishing = argparse.ArgumentParser(add_help=False)
    publishing.add_argument('--write-repo-pins', action='store_true',
                            help='write public pins into the workspace (release-pins/, trust store, installer)')
    publishing.add_argument('--apply-github', action='store_true',
                            help='set the GitHub variables and secrets (default: dry run)')
    publishing.add_argument('--force-github', action='store_true',
                            help='replace GitHub values this private directory did not set')
    publishing.add_argument('--server-release-token-file', default=None,
                            help=f'file holding the {SECRET_RELEASE_TOKEN} token (mode 0600); sent via stdin')
    publishing.add_argument('--delegations-expires', default=None, help='default: two years from now')

    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', parents=[common, publishing], help='create missing keys and trust (idempotent)')
    mode = init.add_mutually_exclusive_group()
    mode.add_argument('--production', action='store_true', help='operator keys; refuses DEVELOPMENT-ONLY material')
    mode.add_argument('--development', action='store_true',
                      help='DEVELOPMENT-ONLY material (the committed public test keys)')
    init.add_argument('--root-keys', type=int, default=None, help='number of root keys (first init; default 1)')
    init.add_argument('--threshold', type=int, default=None, help='root threshold k (default: majority)')
    init.add_argument('--root-expires', default=None, help='default: ten years from now')
    init.add_argument('--external', action='append', default=[], metavar='ROLE=PUBLIC_PEM',
                      help='KMS/HSM signer: record only the public key for build-manifest, artifact-catalog '
                           'or server-update (repeat)')

    rotate = sub.add_parser('rotate', parents=[common, publishing], help='rotate or revoke one role')
    rotate.add_argument('role', choices=ALL_ROLES)
    rotate.add_argument('--revoke', action='store_true', help='revoke the old delegate (compromise)')
    rotate.add_argument('--raise-floor', action='store_true',
                        help='raise shorebird_release_min_delegations_version to the new delegations version')
    rotate.add_argument('--external', default=None, metavar='PUBLIC_PEM', help='new key held by a KMS/HSM')
    rotate.add_argument('--stage', choices=('add', 'activate', 'retire'), help='server-update rotation stage')
    rotate.add_argument('--root-keys', type=int, default=None)
    rotate.add_argument('--threshold', type=int, default=None)
    rotate.add_argument('--root-expires', default=None)
    rotate.add_argument('--repin', action='store_true', help='root: pin the new root version in new engines')

    sub.add_parser('renew', parents=[common, publishing], help='re-sign the delegations with a later expiry')
    sub.add_parser('sync', parents=[common, publishing], help='repository pins and GitHub only')
    for name in ('status', 'verify'):
        command = sub.add_parser(name, parents=[common], help='check private dir, repository pins and GitHub')
        command.add_argument('--skip-repo', action='store_true', help='do not check the repository pins')
    sub.add_parser('env', parents=[common], help='print build-machine environment lines')

    args = parser.parse_args(argv)
    args.workspace = args.workspace.resolve()
    try:
        if args.command == 'init':
            return command_init(args)
        if args.command == 'rotate':
            return command_rotate(args)
        if args.command == 'renew':
            return command_renew(args)
        if args.command == 'sync':
            return command_sync(args)
        if args.command == 'env':
            return command_env(args)
        return command_status(args, strict=args.command == 'verify')
    except (ProvisionError, rt.TrustError, ac.CatalogError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
