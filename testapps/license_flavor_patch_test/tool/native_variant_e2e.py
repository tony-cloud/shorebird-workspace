#!/usr/bin/env python3
"""Shared real-server protocol and public app receipt oracles; owns no processes."""
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import threading
import time
import uuid

SCHEMA = 'native-variant-e2e-v2'
VARIANTS = ('free', 'pro', 'enterprise')
BUILD_ORDER = ('base', 'pro_seed', 'pro', 'enterprise')
LIFECYCLE = ('free', 'pro', 'enterprise', 'pro', 'free', 'enterprise', 'free')
EXPECTED_NUMBERS = {'free': None, 'pro': '2', 'enterprise': '3'}
CODE = {'base': "String variantSpecificCode() => 'free:base';",
        'pro_seed': "class ProSeedFeature { String value() => 'pro:seed'; }\nString variantSpecificCode() => ProSeedFeature().value();",
        'pro': "class ProVariantFeature { final int factor; ProVariantFeature(this.factor); @pragma('vm:never-inline') int calculate(int x) => x * factor; }\nString variantSpecificCode() => 'pro:${ProVariantFeature(int.parse(Platform.environment['SHOREBIRD_VARIANT_FACTOR'] ?? '7')).calculate(6)}';",
        'enterprise': "class EnterpriseVariantFeature { final List<int> seats; EnterpriseVariantFeature(this.seats); @pragma('vm:never-inline') int total() => seats.fold(0, (a, b) => a + b); }\nString enterpriseOnlyFunction() => 'enterprise:${EnterpriseVariantFeature((Platform.environment['SHOREBIRD_VARIANT_SEATS'] ?? '4,8,12').split(',').map(int.parse).toList()).total()}';\nString variantSpecificCode() => enterpriseOnlyFunction();"}
AAD_FIELDS = ('app_id', 'release_id', 'patch_number', 'patch_hash', 'transport_hash', 'platform', 'arch', 'device_id_hash')
MAX_BODY = 32768


def clear_private_flutter_metadata(flutter, owned_root):
    """Drop inherited informational caches only inside an owned SDK copy."""
    flutter, owned_root = Path(flutter).absolute(), Path(owned_root).resolve()
    if flutter != flutter.resolve() or owned_root not in flutter.parents:
        raise ValueError('Metadata cleanup requires a contained private Flutter copy')
    cache = flutter / 'bin/cache'
    stamp = (cache / 'engine.stamp').read_text().strip()
    revision = (flutter / 'bin/internal/engine.version').read_text().strip()
    if not re.fullmatch('[a-f0-9]{40}',stamp) or stamp != revision:
        raise ValueError('Private Flutter engine identity is not consistently pinned')
    removed = {}
    for name in ('engine_stamp.json','flutter.version.json'):
        path = cache / name
        if path.is_symlink():
            raise ValueError('Private informational metadata must not alias another SDK')
        if path.exists():
            if not path.is_file() or path.stat().st_size > 1024*1024:
                raise ValueError('Unexpected private informational metadata')
            removed[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            path.unlink()
    return {'engine_revision':stamp,'removed_inherited_metadata':removed}


def strict_json(value):
    def pairs(items):
        out = {}
        for key, item in items:
            if key in out:
                raise ValueError('Duplicate JSON key')
            out[key] = item
        return out
    return json.loads(value, object_pairs_hook=pairs)


def decimal(value, *, zero=False):
    if not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]{0,18}' if zero else r'[1-9][0-9]{0,18}', value) or int(value) > 9223372036854775807:
        raise ValueError('Invalid canonical decimal')
    return value


def selection_identity(value):
    if not isinstance(value, dict):
        raise ValueError('Missing selection identity')
    epoch = decimal(value.get('epoch'), zero=True)
    variant = value.get('variant_id')
    if variant not in VARIANTS or value.get('license_type') != variant or value.get('patch_number') != EXPECTED_NUMBERS[variant]:
        raise ValueError('Selection variant/content mismatch')
    digest = value.get('digest')
    if (epoch == '0' and (digest is not None or variant != 'free')) or (epoch != '0' and (not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest))):
        raise ValueError('Selection epoch/digest mismatch')
    return {name: value[name] for name in ('epoch', 'digest', 'variant_id', 'license_type', 'patch_number')}


def context_for(result):
    context = {'version': 2, 'selection': result['selection'], 'selection_digest': result['selection_digest']}
    selection_identity({'epoch': context['selection']['epoch'], 'digest': context['selection_digest'],
        'variant_id': context['selection']['variant_id'], 'license_type': context['selection']['license_type'],
        'patch_number': context['selection']['target']['patch_number']})
    return context


def parse_aad(aad):
    if not isinstance(aad, str) or len(aad.encode()) > 8192:
        raise ValueError('Invalid AAD size')
    lines = aad.split('\n')
    if len(lines) != 9 or lines[0] != 'open-aot-patch-delivery-v2':
        raise ValueError('Invalid AAD header')
    result = {}
    for field, line in zip(AAD_FIELDS, lines[1:]):
        key, separator, value = line.partition('=')
        if not separator or key != field or not value or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid AAD field')
        result[key] = value
    if str(uuid.UUID(result['app_id'])) != result['app_id']:
        raise ValueError('Invalid app identity')
    decimal(result['release_id']); decimal(result['patch_number'])
    for key in ('patch_hash', 'transport_hash', 'device_id_hash'):
        if not re.fullmatch('[a-f0-9]{64}', result[key]):
            raise ValueError('Invalid AAD hash')
    if result['platform'] not in ('android', 'linux', 'windows') or result['arch'] not in ('arm', 'aarch64', 'x86_64') or (result['arch'] == 'arm' and result['platform'] != 'android'):
        raise ValueError('Invalid native AAD target')
    return result


def canonical_aad(context):
    if set(context) != set(AAD_FIELDS):
        raise ValueError('Unknown delivery context')
    value = 'open-aot-patch-delivery-v2' + ''.join('\n' + key + '=' + str(context[key]) for key in AAD_FIELDS)
    parse_aad(value)
    return value


def delivery_material(secret, aad):
    parse_aad(aad)
    import base64
    def material(label):
        return hmac.new(secret, label + b'\0' + aad.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(material(b'key-id')[:12]).rstrip(b'=').decode(), material(b'key')


def validate_context(key_id, aad, context, allowed_contexts):
    if isinstance(context, str):
        context = strict_json(context)
    fields = parse_aad(aad)
    if not isinstance(key_id, str) or not re.fullmatch('[A-Za-z0-9_-]{16}', key_id) or not isinstance(context, dict) or set(context) != {'version', 'selection', 'selection_digest'} or type(context['version']) is not int or context['version'] != 2:
        raise ValueError('Invalid verified selection context')
    if context not in allowed_contexts:
        raise ValueError('Context was not returned by an authenticated assignment')
    descriptor, scope = context['selection'], context['selection']['scope']
    target = descriptor['target']
    start = int(decimal(descriptor.get('not_before'),zero=True))
    end = descriptor.get('offline_valid_until')
    end = int(decimal(end,zero=True)) if end is not None else None
    now = int(time.time())
    if start > 253402300799 or (end is not None and (end > 253402300799 or end <= start)) or now < start or (end is not None and now >= end):
        raise ValueError('Verified grant lease is not currently valid')
    if (scope.get('runtime_profile') != 'native-aot' or scope.get('host_abi_hash') is not None or
        any(scope.get(key) != fields[field] for key, field in [('app_id','app_id'), ('platform','platform'), ('arch','arch'), ('client_id_hash','device_id_hash')]) or
        descriptor.get('channel') != 'stable' or target.get('kind') != 'patch' or
        target.get('encryption') != 'transport-aes-256-gcm' or target.get('artifact_key_id') is not None or
        target.get('patch_number') != fields['patch_number'] or target.get('artifact_hash') != fields['patch_hash']):
        raise ValueError('Verified grant differs from native transport AAD')
    identity = selection_identity({'epoch': descriptor['epoch'], 'digest': context['selection_digest'],
        'variant_id': descriptor['variant_id'], 'license_type': descriptor['license_type'], 'patch_number': target['patch_number']})
    return dict(identity, key_id=key_id)


class KeyAuthority:
    def __init__(self, secret, token, platform, arch):
        self.secret, self.token, self.platform, self.arch = secret, token, platform, arch
        self.allowed = set()
        self.lock = threading.Lock()
        self.accepted = self.rejected = 0

    def allow(self, context):
        aad = canonical_aad(context)
        fields = parse_aad(aad)
        if fields['platform'] != self.platform or fields['arch'] != self.arch:
            raise ValueError('Provisioning target mismatch')
        self.allowed.add(aad)
        return aad

    def provision(self, authorization, body):
        with self.lock:
            try:
                if not isinstance(authorization,str) or not hmac.compare_digest(authorization,'Bearer ' + self.token) or len(body) > MAX_BODY:
                    raise ValueError('Unauthorized provisioning')
                value = strict_json(body)
                if set(value) != {'key_id', 'aad'} or value['aad'] not in self.allowed:
                    raise ValueError('Unregistered delivery')
                key_id, key = delivery_material(self.secret, value['aad'])
                if not hmac.compare_digest(value['key_id'], key_id):
                    raise ValueError('Wrong delivery key ID')
                self.accepted += 1
                return key.hex().encode()
            except (ValueError, TypeError, KeyError, AttributeError):
                self.rejected += 1
                raise ValueError('Provisioning rejected') from None


def apply_profile(app, session, sentinel, phase):
    if phase not in BUILD_ORDER:
        raise ValueError('Unknown native variant build phase')
    if any(not isinstance(value,str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}',value) for value in (session,sentinel)):
        raise ValueError('Noncanonical native fixture token')
    variant = 'free' if phase == 'base' else 'pro' if phase == 'pro_seed' else phase
    source = Path(__file__).with_name('native_variant_status.dart.template').read_text()
    for key, value in {'__SESSION__': session, '__SENTINEL__': sentinel, '__VARIANT__': variant,
                       '__DATA_SCHEMA__': str({'free':1,'pro':2,'enterprise':3}[variant]), '__VARIANT_CODE__': CODE[phase]}.items():
        source = source.replace(key, value)
    (Path(app) / 'lib/main.dart').write_text(source)


def validate_report(report, nonce, pid, variant, previous_data=None, accepted=None):
    expected_code = {'free':'free:base','pro':'pro:42','enterprise':'enterprise:24'}[variant]
    if (report.get('schema') != SCHEMA or report.get('nonce') != nonce or report.get('pid') != pid or
        type(report.get('pid')) is not int or report['pid'] <= 0 or report.get('post_frame') is not True or
        report.get('variant') != variant or report.get('new_code_result') != expected_code or
        report.get('rendered') != ['variant:' + variant + ' ' + expected_code] or
        report.get('stage') not in ('boot','checked') or not isinstance(report.get('installation_id'), str) or
        not 0 < len(report['installation_id'].encode()) <= 1024):
        raise ValueError('Missing fresh public API/rendered variant receipt')
    status = report['selection']
    if type(status.get('version')) is not int or status.get('version') != 2 or type(status.get('restart_required')) is not bool:
        raise ValueError('Invalid atomic selection status')
    current = selection_identity(status['current'])
    observed = selection_identity(status['accepted'])
    if current['variant_id'] != variant:
        raise ValueError('Running Dart code differs from process-pinned status')
    if accepted is not None and observed != selection_identity(accepted):
        raise ValueError('Accepted status differs from authenticated assignment')
    next_boot = status['next_boot']
    if (next_boot.get('variant_id') not in VARIANTS or next_boot.get('license_type') != next_boot['variant_id'] or
        next_boot.get('patch_number') != EXPECTED_NUMBERS[next_boot['variant_id']] or
        next_boot.get('reason') not in ('ready','selected_base','expired','not_yet_valid','content_unavailable','quarantined','no_authorization')):
        raise ValueError('Invalid effective next boot')
    expected_restart = current['epoch'] != observed['epoch'] or current['digest'] != observed['digest'] or current['patch_number'] != next_boot['patch_number']
    if status['restart_required'] != expected_restart:
        raise ValueError('Authorization/content restart signal mismatch')
    data = report['data']
    if (not isinstance(data, dict) or type(data.get('launches')) is not int or data['launches'] < 1 or
        type(data.get('schema')) is not int or data['schema'] < {'free':1,'pro':2,'enterprise':3}[variant] or
        not isinstance(data.get('history'), list) or len(data['history']) != data['launches'] or data['history'][-1] != variant):
        raise ValueError('Application-owned persistent data missing')
    if previous_data is not None:
        if (data.get('session') != previous_data['session'] or data.get('sentinel') != previous_data['sentinel'] or
            data['launches'] != previous_data['launches'] + 1 or data['history'] != previous_data['history'] + [variant] or
            data['schema'] < previous_data['schema'] or any(data.get(k) != v for k,v in previous_data.items() if k not in ('launches','history','schema'))):
            raise ValueError('Variant transition lost application data')
    return report


def validate_cold_activation(report, accepted, *, fallback=False):
    expected = selection_identity(accepted)
    status = report['selection']
    if selection_identity(status['accepted']) != expected:
        raise ValueError('Cold process lost latest accepted authorization')
    current = selection_identity(status['current'])
    if fallback:
        if report['variant'] != 'free' or current != dict(expected,variant_id='free',license_type='free',patch_number=None):
            raise ValueError('Cold BASE fallback lost accepted epoch/digest')
    elif current != expected or report['variant'] != expected['variant_id'] or status['restart_required'] is not False:
        raise ValueError('Cold process did not adopt exact latest accepted authorization')
    return report


class NativeVariantSession:
    def __init__(self, app_id, release_id, release_version, platform, arch, session, request):
        self.app_id, self.release_id, self.release_version = app_id, release_id, release_version
        self.platform, self.arch, self.session, self.request = platform, arch, session, request
        self.installation_id = None
        self.contexts, self.assignments = [], []
        self.observed = {'epoch':'0','digest':None,'variant_id':'free','license_type':'free','patch_number':None}
        self.pending = None

    @property
    def latest(self):
        return self.observed

    def path(self, suffix):
        return f'/api/v1/apps/{self.app_id}/releases/{self.release_id}' + suffix

    def map_free(self):
        return self.request('PUT', self.path('/variants/free'), {'platform':self.platform,'arch':self.arch,
            'channel':'stable','license_type':'free','target':{'kind':'base','patch_number':None,
            'artifact_hash':None,'encryption':'none','artifact_key_id':None}})

    def remember(self, report):
        value = report['installation_id']
        if not isinstance(value,str) or not value or (self.installation_id is not None and value != self.installation_id):
            raise ValueError('Public installation ID changed')
        self.installation_id = value
        observed = selection_identity(report['selection']['accepted'])
        if int(observed['epoch']) < int(self.latest['epoch']):
            raise ValueError('Accepted epoch regressed')
        if observed['epoch'] == self.latest['epoch'] and observed != self.latest:
            raise ValueError('Conflicting accepted digest at same epoch')
        if int(observed['epoch']) > int(self.latest['epoch']) and observed['variant_id'] != 'free':
            raise ValueError('Unknown paid authorization')
        self.observed = observed

    def assign(self, variant, offline_valid_until=None, not_before=None):
        if variant not in VARIANTS or not self.installation_id:
            raise ValueError('Assignment requires a real public installation ID')
        body = {'client_id':self.installation_id,'platform':self.platform,'arch':self.arch,'channel':'stable',
            'variant_id':variant,'entitlement_revision':self.session + ':' + str(uuid.uuid4()),
            'not_before':str(int(time.time())) if not_before is None else not_before,
            'offline_valid_until':offline_valid_until,'expected_epoch':self.latest['epoch'],'idempotency_key':str(uuid.uuid4())}
        if self.pending is not None:
            if self.pending['variant_id'] != variant or self.pending['offline_valid_until'] != offline_valid_until or (not_before is not None and self.pending['not_before'] != not_before):
                raise ValueError('Resolve prior idempotent assignment first')
            body = self.pending
        else:
            self.pending = body
        result = self.request('POST', self.path('/selections'), body)
        context = context_for(result)
        descriptor, scope = context['selection'], context['selection']['scope']
        if (descriptor['epoch'] != str(int(body['expected_epoch']) + 1) or descriptor['variant_id'] != variant or
            descriptor['license_type'] != variant or descriptor['target']['patch_number'] != EXPECTED_NUMBERS[variant] or
            scope['app_id'] != self.app_id or scope['release_version'] != self.release_version or
            scope['platform'] != self.platform or scope['arch'] != self.arch or scope['runtime_profile'] != 'native-aot' or
            scope['host_abi_hash'] is not None or scope['client_id_hash'] != hashlib.sha256(self.installation_id.encode()).hexdigest() or
            any(descriptor.get(key) != body[key] for key in ('channel','entitlement_revision','not_before','offline_valid_until'))):
            raise ValueError('Authenticated assignment did not bind installation/content/scope')
        self.observed = selection_identity({'epoch':descriptor['epoch'],'digest':context['selection_digest'],
            'variant_id':variant,'license_type':variant,'patch_number':descriptor['target']['patch_number']})
        self.contexts.append(context)
        self.assignments.append(result)
        self.pending = None
        return result
