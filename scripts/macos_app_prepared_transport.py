#!/usr/bin/env python3
"""Runtime-only prepared Mac app transport; never sign, compile or launch an app.

Schema macos-prepared-variant-lifecycle-v1 binds an externally supplied execution
commit, actual clean root/gitlinks/helper bytes, independently pinned components,
original H/T and server producer provenance, pristine state/DB/objects, private
fixture seed and exact signed bundle inventory. Producer roots may differ from
the execution root; none is relabelled. The SDK93 transport is not imported.

prepare creates a fresh staging directory with payload/ and private verification
diagnostics. package includes only payload. validate checks archive bytes/source;
extract additionally admits only a native Intel host and verifies signatures
before creating fresh runtime credentials. No lifecycle consumer is invoked:
protected-container/TCC feasibility and actual native acceptance remain pending.
All public CLI output is an authored status, never raw exceptions/seed contents.

The caller authenticates and independently pins --components, --build-manifest
and --server-pin. Server-pin schema macos-app-server-input-v1 contains exactly
server_commit, binary_sha256, archive_sha256 and producer (the authenticated
target-producer record with artifact_name=shorebird-server-darwin-amd64).
This records the original archive identity; this helper accepts an already
selected binary and does not download or attest a producer's source-to-binary
build. Signing verification requires the installed platform tools.
An authenticated downloader, sanitized lifecycle launcher, TCC capability smoke
and public allowlisted exporter remain separate integration work.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing, contextmanager
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile

import macos_app_build_inputs as inputs

SCHEMA = 'macos-prepared-variant-lifecycle-v1'
MANIFEST = 'prepared-app-manifest.json'
ROOT = re.compile(r'/private/tmp/shorebird-macos-x64-app-[A-Za-z0-9_-]{8,80}')
SHA, COMMIT = inputs.SHA, inputs.COMMIT
CERT = re.compile(r'[A-F0-9]{40}')
MAX_FILE, MAX_TOTAL, MAX_FILES, MAX_ARCHIVE = inputs.MAX_FILE, 6 * 1024**3, 30000, 2 * 1024**3
TOOL = 'testapps/license_flavor_patch_test/tool/'
SOURCES = ('scripts/macos_app_prepared_transport.py', 'scripts/macos_app_build_inputs.py', *(
    TOOL + name for name in ('macos_interpreter_e2e.py', 'macos_variant_e2e.py', 'macos_variant_lifecycle.py',
                            'ios_device_e2e.py', 'owned_host_processes.py',
                            'macos_interpreter/PatchTestKeysV2.swift.template', 'macos_variant/variant_status.dart.template')))
MODULES = ('ios_device_e2e', 'owned_host_processes', 'macos_interpreter_e2e',
           'macos_variant_e2e', 'macos_variant_lifecycle')
LINKS = {'dart-sdk':'dart_sdk', 'flutter':'flutter', 'updater':'updater',
         'shorebird':'shorebird', 'shorebird-server':'server'}
NATIVE = tuple('app/' + name for name in (
    'macos/Runner/MainFlutterWindow.swift', 'macos/Runner/PatchTestKeys.swift',
    'macos/Runner/FullFfiFixture.c', 'macos/Runner/FullFfiFixture.h',
    'macos/Runner/Runner-Bridging-Header.h', 'macos/Runner/Release.entitlements',
    'macos/Runner/Configs/Release.xcconfig', 'macos/Runner.xcodeproj/project.pbxproj'))
STATE_KEYS = {'schema','variant_schema','work_dir','session','created','platform','arch','bundle_id',
              'key_service','key_ids','app_id','release_version','sentinel','full_replacement',
              'full_replacement_ffi','base_built','built_phases','native_source_hashes','release_scope',
              'released_app','server','api_url','report_url','assignments'}
SEED_KEYS = {'bootstrap_password','admin_password','admin_password_active','signing_master_key','variant_keys'}
FIXED = {'state.json','state.sqlite','private-bootstrap.json','runtime/shorebird-server',
         'provenance/build-inputs.json','provenance/server-input.json',*NATIVE}
MANIFEST_KEYS = {'schema','status','canonical_root','source','certificate_sha1','signature',
                 'app','objects','files','input_pins'}

require = inputs.require
encoded = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
sha = lambda data: hashlib.sha256(data).hexdigest()


def canonical(value):
    require(isinstance(value,str) and ROOT.fullmatch(value), 'Invalid canonical app root')
    return Path(value)


def absent(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise ValueError('Destination/container already exists')


def available_ports(state):
    from urllib.parse import urlparse
    sockets=[]
    try:
        for name in ('api_url','report_url'):
            probe=socket.socket(socket.AF_INET,socket.SOCK_STREAM); sockets.append(probe)
            probe.bind(('127.0.0.1',urlparse(state[name]).port))
    finally:
        for probe in sockets: probe.close()


def read(root, name, limit=MAX_FILE):
    with inputs.host_file(Path(root), inputs.relative(name), limit) as (stream, _):
        data = stream.read(limit+1)
    require(len(data) <= limit, 'Input exceeds bounded size')
    return data


def json_file(root, name):
    return inputs.strict_json(read(root,name,inputs.MAX_JSON))


def write(path, data, mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with path.open('xb') as stream:
        stream.write(data)
    path.chmod(mode)


def git(workspace, *args):
    return subprocess.check_output(['git','-C',str(workspace),*args],stderr=subprocess.PIPE,timeout=30).decode().strip()


def source_identity(workspace, expected_commit, components):
    require(isinstance(expected_commit,str) and COMMIT.fullmatch(expected_commit), 'Invalid external execution commit')
    require(set(components) == inputs.COMPONENTS | {'server'}, 'Invalid external component pins')
    inputs.components({key:value for key,value in components.items() if key!='server'})
    require(isinstance(components['server'],str) and COMMIT.fullmatch(components['server']), 'Invalid server pin')
    workspace = Path(workspace).resolve()
    require(git(workspace,'rev-parse','--show-toplevel') == str(workspace) and
            git(workspace,'rev-parse','HEAD') == expected_commit, 'Actual checkout differs from external execution commit')
    require(not git(workspace,'diff','--name-only','--ignore-submodules=all','HEAD'), 'Tracked execution source is dirty')
    for path,key in LINKS.items():
        entry = git(workspace,'ls-tree','HEAD','--',path).split()
        require(entry == ['160000','commit',components[key],path], 'Root gitlink differs from external components')
    files = {}
    for name in SOURCES:
        git(workspace,'ls-files','--error-unmatch',name)
        files[name] = sha(read(workspace,name,inputs.MAX_JSON))
    require(files[SOURCES[0]] == sha(Path(__file__).read_bytes()), 'Executing transport differs from pinned tracked implementation')
    require(files[SOURCES[1]] == sha(Path(inputs.__file__).read_bytes()), 'Imported materializer differs from pinned implementation')
    return {'workspace_commit':expected_commit,'components':components,'files':files}


@contextmanager
def fixture_modules(workspace):
    """Use only the verified checkout's modules; restore embedding callers."""
    old = {name:sys.modules.pop(name,None) for name in MODULES}
    directory = str(Path(workspace)/TOOL)
    sys.path.insert(0,directory)
    try:
        # Compile only pinned source bytes, ignoring a caller's writable pycache.
        for name in MODULES:
            path=Path(directory)/(name+'.py')
            spec=importlib.util.spec_from_file_location(name,path)
            module=importlib.util.module_from_spec(spec); sys.modules[name]=module
            exec(compile(read(workspace,TOOL+name+'.py',inputs.MAX_JSON),str(path),'exec'),module.__dict__)
        variant=sys.modules['macos_variant_e2e']; lifecycle=sys.modules['macos_variant_lifecycle']
        for name in MODULES:
            module = sys.modules.get(name)
            require(module is not None and Path(module.__file__).resolve().parent == Path(directory).resolve(),
                    'Fixture verifier imported outside pinned checkout')
        yield variant,lifecycle
    finally:
        sys.path.remove(directory)
        for name,module in old.items():
            sys.modules.pop(name,None)
            if module is not None: sys.modules[name] = module


def validate_state(state, root):
    require(isinstance(state,dict) and set(state)==STATE_KEYS, 'Invalid exact runtime state projection')
    require(state['schema']=='macos-interpreter-e2e-v1' and state['variant_schema']=='macos-variant-e2e-v2' and
            state['work_dir']==str(root) and state['platform']=='macos' and state['arch']=='x64' and
            all(state[k] is True for k in ('base_built','full_replacement','full_replacement_ffi')) and
            state['built_phases']==['base','pro_seed','pro','enterprise'] and state['assignments']==[],
            'Runtime state is not a pristine full-profile x64 fixture')
    require(isinstance(state['session'],str) and re.fullmatch('[a-f0-9]{12}',state['session']) and
            state['bundle_id']=='org.tonycloud.shorebird-macos-ffi.'+state['session'] and
            state['key_service']=='org.tonycloud.shorebird-macos-key.'+state['session'] and
            isinstance(state['sentinel'],str) and re.fullmatch('[a-f0-9]{48}',state['sentinel']) and
            re.fullmatch('[a-f0-9-]{36}',state['app_id']) and
            re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+\+[0-9]+',state['release_version']), 'Invalid fixture public identity')
    require(type(state['created']) in (int,float) and math.isfinite(state['created']) and state['created']>0,
            'Invalid preparation timestamp')
    require(state['key_ids']=={v:state['session']+'-'+v+'-artifact' for v in ('pro','enterprise')}, 'Invalid scoped key IDs')
    app = Path(state['released_app'])
    require(app.parent==root/'released' and re.fullmatch(r'[A-Za-z0-9_-]+\.app',app.name) and
            state['server']==str(root/'runtime/shorebird-server'), 'Unallowlisted runtime executable path')
    require(isinstance(state['native_source_hashes'],dict) and state['native_source_hashes'].keys()=={name[4:] for name in NATIVE} and
            all(isinstance(v,str) and SHA.fullmatch(v) for v in state['native_source_hashes'].values()), 'Wrong native source inventory')
    scope = state['release_scope']
    require(isinstance(scope,dict) and set(scope)=={'app_id','release_version','platform','arch','runtime_profile','base_artifact_hash','host_abi_hash'} and
            all(scope[k]==v for k,v in {'app_id':state['app_id'],'release_version':state['release_version'],
                'platform':'macos','arch':'x86_64','runtime_profile':'interpreter-full'}.items()) and
            all(isinstance(scope[k],str) and SHA.fullmatch(scope[k]) for k in ('base_artifact_hash','host_abi_hash')), 'Invalid pinned full release scope')
    from urllib.parse import urlparse
    ports = []
    for key in ('api_url','report_url'):
        url = urlparse(state[key])
        require(url.scheme=='http' and url.hostname=='127.0.0.1' and url.port and
                not url.username and not url.query and not url.fragment and
                url.path==('' if key=='api_url' else '/report/'+state['session']), 'Invalid fixed loopback endpoint')
        ports.append(url.port)
    require(len(set(ports))==2, 'Fixture endpoints share one port')
    return app.relative_to(root).as_posix()


def project_state(original, root):
    forbidden = ('installation_id','observed_selection','pending_assignment','cleanup','cleanup_fence','cleanup_in_progress',
                 'launches','runtime_acceptance','server_pid','controller_pid')
    require(isinstance(original,dict) and not any(k in original for k in forbidden),
            'Preparation contains prior runtime authority/cleanup')
    result = {key:original[key] for key in STATE_KEYS if key!='server'}
    result['server'] = str(root/'runtime/shorebird-server')
    validate_state(result,root)
    return result


def validate_seed(seed):
    require(isinstance(seed,dict) and set(seed)==SEED_KEYS and type(seed['admin_password_active']) is bool,
            'Invalid private bootstrap field allowlist')
    require(all(isinstance(seed[k],str) and re.fullmatch('[A-Za-z0-9_-]{16,256}',seed[k]) for k in ('bootstrap_password','admin_password')),
            'Invalid disposable password shape')
    key = base64.b64decode(seed['signing_master_key'],validate=True)
    require(len(key)==32 and base64.b64encode(key).decode()==seed['signing_master_key'], 'Invalid managed master key')
    keys = seed['variant_keys']
    require(isinstance(keys,dict) and set(keys)=={'pro','enterprise'} and len(set(keys.values()))==2 and
            all(isinstance(v,str) and SHA.fullmatch(v) and v!='00'*32 for v in keys.values()), 'Invalid distinct fixture AES keys')


def provenance(build, server, components):
    require(isinstance(build,dict) and set(build)=={'schema','status','runtime_acceptance','components','metadata',
                'target','host','required_private_flutter_cache_bindings','pending','files'} and
            build.get('schema')=='macos-app-materialized-build-inputs-v1' and
            build.get('status')=='materialized_unexecuted' and build.get('runtime_acceptance') is False and
            build.get('components')=={k:v for k,v in components.items() if k!='server'}, 'Wrong materializer provenance')
    inputs.host_provenance({'schema':'macos-app-host-provenance-v1','components':build['components'],'producer':build['host']['producer']})
    inputs.target_pin({'schema':'macos-app-target-archive-v1','components':build['components'],
                       'producer':build['target']['producer'],'archive_sha256':build['target']['archive_sha256']})
    require(set(build['host'])=={'platform','arch','machine','out','producer','original_args_sha256','closure_files'} and
            set(build['target'])=={'platform','arch','machine','out','producer','archive_sha256','archive_manifest','original_args_sha256'} and
            all(build[role].get(k)==v for role,values in {
                'host':{'platform':'macos','arch':'arm64','machine':'arm64','out':'out/'+inputs.HOST_NAME},
                'target':{'platform':'macos','arch':'x64','machine':'x86_64','out':'out/'+inputs.TARGET_NAME}}
                .items() for k,v in values.items()), 'Wrong host/target role')
    for role in ('host','target'):
        require(isinstance(build[role]['original_args_sha256'],str) and SHA.fullmatch(build[role]['original_args_sha256']),
                'Missing original producer args hash')
    metadata=build['metadata']
    require(isinstance(metadata,dict) and set(metadata)=={'external_sha256','host_closure_canonical_sha256','target_pin_canonical_sha256'} and
            set(metadata['external_sha256'])=={'host_manifest','target_pin'} and
            all(isinstance(value,str) and SHA.fullmatch(value) for value in metadata['external_sha256'].values()),
            'Missing independently pinned preparation metadata')
    host={'schema':'macos-app-host-closure-v1','components':build['components'],
          'producer':build['host']['producer'],'files':build['host']['closure_files']}
    target={'schema':'macos-app-target-archive-v1','components':build['components'],
            'producer':build['target']['producer'],'archive_sha256':build['target']['archive_sha256']}
    require(metadata['host_closure_canonical_sha256']==sha(encoded(host)) and
            metadata['target_pin_canonical_sha256']==sha(encoded(target)), 'Original H/T metadata identity changed')
    require(isinstance(host['files'],dict) and inputs.HOST_REQUIRED<=host['files'].keys(), 'Incomplete original host closure')
    for inventory in (host['files'],build['files']):
        require(isinstance(inventory,dict) and 0<len(inventory)<=MAX_FILES, 'Missing complete preparation inventory')
        inputs.check_names(inventory)
        for name,item in inventory.items():
            require(isinstance(item,dict), 'Invalid preparation file identity')
            if set(item)=={'symlink'}: inputs.relative(item['symlink'])
            else:
                require(set(item)=={'sha256','size','executable'} and isinstance(item['sha256'],str) and
                        SHA.fullmatch(item['sha256']) and type(item['size']) is int and 0<=item['size']<=MAX_FILE and
                        type(item['executable']) is bool, 'Invalid preparation file identity')
    require(isinstance(server,dict) and set(server)=={'schema','server_commit','binary_sha256','archive_sha256','producer'} and
            server['schema']=='macos-app-server-input-v1' and server['server_commit']==components['server'] and
            all(isinstance(server[k],str) and SHA.fullmatch(server[k]) for k in ('binary_sha256','archive_sha256')),
            'Invalid server provenance')
    p = dict(server['producer'])
    require(p.pop('artifact_name',None)=='shorebird-server-darwin-amd64', 'Wrong native server artifact')
    p['artifact_name']='macos-engine-x64'
    inputs.target_pin({'schema':'macos-app-target-archive-v1','components':build['components'],
                       'producer':p,'archive_sha256':server['archive_sha256']})


def validate_server(data, pin):
    """Read native Go build settings without invoking the executable or Go."""
    require(sha(data)==pin['binary_sha256'], 'Native server hash mismatch')
    inputs.macho(data,{inputs.X64},2)
    # Current producer uses Go 1.25+ inline buildinfo in __go_buildinfo.
    ncmds=struct.unpack_from('<I',data,16)[0]; position=32; sections=[]
    for _ in range(ncmds):
        command,size=struct.unpack_from('<II',data,position)
        if command==0x19:  # LC_SEGMENT_64
            require(size>=72, 'Truncated native server segment')
            count=struct.unpack_from('<I',data,position+64)[0]
            require(72+count*80<=size, 'Truncated native server sections')
            for index in range(count):
                section=position+72+index*80
                if data[section:section+16].rstrip(b'\0')==b'__go_buildinfo':
                    length=struct.unpack_from('<Q',data,section+40)[0]
                    offset=struct.unpack_from('<I',data,section+48)[0]
                    require(32<=length<=1024**2 and offset+length<=len(data), 'Invalid Go buildinfo section')
                    sections.append(data[offset:offset+length])
        position+=size
    require(len(sections)==1 and sections[0][:16]==b'\xff Go buildinf:\x08\x02', 'Missing current native Go buildinfo')
    info=sections[0]; cursor=32
    def string():
        nonlocal cursor
        length=0
        for shift in range(0,64,7):
            require(cursor<len(info), 'Truncated Go buildinfo string')
            byte=info[cursor]; cursor+=1; length|=(byte&127)<<shift
            if byte<128: break
        else: raise ValueError('Oversized Go buildinfo varint')
        require(length<=len(info)-cursor, 'Truncated Go buildinfo payload')
        value=info[cursor:cursor+length]; cursor+=length
        return value
    version=string(); module=string()
    require(re.fullmatch(rb'go1\.[0-9]+(?:\.[0-9]+)?',version) and len(module)>=33 and module[-17:-16]==b'\n',
            'Invalid Go version/module framing')
    lines=module[16:-16].decode('utf-8').splitlines()
    require(lines.count('path\tgithub.com/shorebird-server/cmd/server')==1, 'Wrong Go server entrypoint')
    settings={}
    for line in lines:
        if not line.startswith('build\t'): continue
        key,separator,value=line[6:].partition('=')
        require(separator and key not in settings, 'Invalid Go build settings')
        settings[key]=value
    require(all(settings.get(k)==v for k,v in {'GOOS':'darwin','GOARCH':'amd64','CGO_ENABLED':'0',
                'vcs':'git','vcs.revision':pin['server_commit'],'vcs.modified':'false'}.items()),
            'Go binary differs from pinned clean native server source')


def scan(root):
    info = Path(root).lstat()
    require(stat.S_ISDIR(info.st_mode), 'Inventory root is not a real directory')
    entries = {}; total = 0
    for directory,dirs,files in os.walk(root,followlinks=False):
        for name in sorted([*dirs,*files]):
            path = Path(directory)/name; relative = path.relative_to(root).as_posix(); inputs.relative(relative)
            info = path.lstat(); mode = stat.S_IMODE(info.st_mode)
            require(not mode & 0o7000 and (stat.S_ISLNK(info.st_mode) or not mode & 0o022),
                    'Special or group/world-writable mode')
            if stat.S_ISLNK(info.st_mode): entries[relative]={'type':'link','target':os.readlink(path)}
            elif stat.S_ISDIR(info.st_mode): entries[relative]={'type':'directory','mode':mode}
            else:
                require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=MAX_FILE, 'Unsafe runtime file')
                data = read(root,relative); total += len(data)
                entries[relative]={'type':'file','mode':mode,'size':len(data),'sha256':sha(data)}
            require(len(entries)<=MAX_FILES and total<=MAX_TOTAL, 'Runtime inventory bound exceeded')
    return entries


def rsa_public_identity(data, *, spki):
    """Strict public DER parser: DB PKIX/SPKI and compiled PKCS1 share RSA n/e."""
    require(isinstance(data,bytes) and 0<len(data)<=4096, 'Invalid RSA public DER size')
    def element(value, position, tag):
        require(position+2<=len(value) and value[position]==tag, 'Invalid RSA public DER tag')
        length=value[position+1]; position+=2
        if length&128:
            count=length&127
            require(1<=count<=4 and position+count<=len(value) and value[position]!=0,
                    'Invalid RSA public DER length')
            length=int.from_bytes(value[position:position+count],'big'); position+=count
            require(length>=128, 'Noncanonical RSA public DER length')
        end=position+length
        require(end<=len(value), 'Truncated RSA public DER')
        return value[position:end],end
    sequence,end=element(data,0,0x30)
    require(end==len(data), 'Trailing RSA public DER')
    if spki:
        algorithm,position=element(sequence,0,0x30)
        require(algorithm==bytes.fromhex('06092a864886f70d0101010500'), 'Non-RSA public key algorithm')
        bits,end=element(sequence,position,0x03)
        require(end==len(sequence) and len(bits)>1 and bits[0]==0, 'Invalid RSA public BIT STRING')
        return rsa_public_identity(bits[1:],spki=False)
    modulus,position=element(sequence,0,0x02)
    exponent,end=element(sequence,position,0x02)
    require(end==len(sequence), 'Extra RSA public parameters')
    values=[]
    for integer in (modulus,exponent):
        require(integer and not integer[0]&128 and
                not (len(integer)>1 and integer[0]==0 and not integer[1]&128), 'Noncanonical RSA public integer')
        values.append(int.from_bytes(integer,'big'))
    n,e=values
    require(2048<=n.bit_length()<=8192 and n&1 and 3<=e<2**31 and e&1, 'Invalid RSA public parameters')
    return n,e


def validate_inventory(entries, app, objects):
    require(isinstance(app,str) and re.fullmatch(r'released/[A-Za-z0-9_-]+\.app',app), 'Invalid inventoried app path')
    require(isinstance(objects,dict) and bool(objects), 'Missing DB object inventory')
    for name,item in objects.items():
        inputs.relative(name)
        require(name.startswith('releases/') and isinstance(item,dict) and set(item)=={'size','sha256','artifact_hash'} and type(item['size']) is int and
                0<item['size']<=MAX_FILE and all(isinstance(item[k],str) and SHA.fullmatch(item[k]) for k in ('sha256','artifact_hash')),
                'Invalid DB object inventory')
    require(isinstance(entries,dict) and len(entries)<=MAX_FILES, 'Invalid runtime inventory')
    inputs.check_names(entries)
    regular = set(); total = 0
    for name,item in entries.items():
        require(isinstance(item,dict), 'Invalid inventory entry')
        parents = list(PurePosixPath(name).parents)[:-1]
        require(all(entries.get(str(p),{}).get('type')=='directory' for p in parents), 'Missing/aliased inventory parent')
        if item.get('type')=='file':
            require(set(item)=={'type','mode','size','sha256'} and type(item['size']) is int and 0<=item['size']<=MAX_FILE and
                    isinstance(item['sha256'],str) and SHA.fullmatch(item['sha256']), 'Invalid file inventory')
            regular.add(name); total += item['size']
        elif item.get('type')=='directory': require(set(item)=={'type','mode'}, 'Invalid directory inventory')
        else: require(item.get('type')=='link' and set(item)=={'type','target'} and name.startswith(app+'/'), 'Unallowlisted symlink')
        if item['type']!='link': require(type(item['mode']) is int and 0<=item['mode']<=0o777 and not item['mode'] & 0o022 and
                                      (item['type']!='directory' or item['mode'] & 0o500 == 0o500),
                                      'Unsafe file mode')
    require(total<=MAX_TOTAL and regular == FIXED | {'objects/'+name for name in objects} |
            {name for name in regular if name.startswith(app+'/')}, 'Missing/unallowlisted runtime file')
    require(entries['runtime/shorebird-server']['mode'] & 0o100, 'Server is not executable')
    for name in FIXED-{'runtime/shorebird-server'} | {'objects/'+name for name in objects}:
        require(entries[name]['mode']==0o600, 'Private projection file has unexpected mode')
    for name,item in entries.items():
        if item['type']=='directory':
            require(name==app or name.startswith(app+'/') or any(p.startswith(name+'/') for p in regular), 'Unallowlisted empty directory')
        if item['type']!='link': continue
        match=re.fullmatch(re.escape(app)+r'/Contents/Frameworks/([A-Za-z0-9_.-]+)\.framework/(.+)',name)
        require(match is not None, 'Only declared versioned-framework links are allowed')
        child=match[2]
        require((child=='Versions/Current' and re.fullmatch('[A-Za-z0-9_-]{1,32}',item['target'])) or
                (child in (match[1],'Headers','Modules','Resources') and item['target']=='Versions/Current/'+child),
                'Unexpected versioned-framework link layout')
        target = inputs.relative(item['target']); resolved = str(PurePosixPath(name).parent/target); seen = set()
        for _ in range(32):
            require(resolved.startswith(app+'/') and resolved not in seen, 'Escaping/cyclic app symlink')
            seen.add(resolved); parts = resolved.split('/'); replacement = None
            for count in range(1,len(parts)+1):
                prefix = '/'.join(parts[:count])
                if entries.get(prefix,{}).get('type')=='link':
                    target = inputs.relative(entries[prefix]['target'])
                    replacement = str(PurePosixPath(prefix).parent/target)
                    if count<len(parts): replacement += '/'+'/'.join(parts[count:])
                    break
            if replacement is None: break
            resolved = replacement
        else: raise ValueError('Excessive app link resolution')
        require(resolved in entries and entries[resolved]['type']!='link', 'Dangling app symlink')


def database_objects(payload, state, app):
    path = payload/'state.sqlite'
    for suffix in ('-wal','-shm','-journal'): absent(Path(str(path)+suffix))
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        require(db.execute('PRAGMA quick_check').fetchone()==('ok',), 'Invalid closed database seed')
        seed=json_file(payload,'private-bootstrap.json')
        require(db.execute('SELECT email,is_admin,must_change_password,auth_provider FROM users').fetchall()==[
                    ('macos-fixture@example.invalid',1,0 if seed['admin_password_active'] else 1,'password')],
                'DB is not the one disposable fixture account/password state')
        require(db.execute('SELECT id FROM apps').fetchall()==[(state['app_id'],)], 'DB contains another app')
        releases = db.execute('SELECT id,app_id,version,selection_protocol FROM releases').fetchall()
        require(len(releases)==1 and releases[0][1:]==(state['app_id'],state['release_version'],2), 'Wrong pristine release DB')
        release = releases[0][0]
        rows = db.execute('SELECT state FROM release_selection_states WHERE release_id=?',(release,)).fetchall()
        require(len(rows)==1, 'Missing published scope seed')
        selection = inputs.strict_json(rows[0][0].encode())
        require(set(selection)=={'scopes','published','mappings','selections','idempotency'} and
                selection.get('published')=={'macos':True} and
                selection.get('selections')=={} and selection.get('idempotency')=={} and
                set(selection.get('scopes',{}))=={'macos/x86_64'} and
                selection['scopes']['macos/x86_64']['record']['scope']==state['release_scope'] and
                selection['scopes']['macos/x86_64']['record']['published'] is True, 'DB has prior authority or wrong scope')
        require(db.execute('SELECT COUNT(*) FROM account_tokens').fetchone()==(0,), 'DB retains account tokens')
        require(db.execute('SELECT COUNT(*) FROM patch_events').fetchone()==(0,), 'DB retains runtime events')
        for table in ('rolled_back_patches','patch_target_devices'):
            require(db.execute('SELECT COUNT(*) FROM '+table).fetchone()==(0,), 'DB retains runtime patch policy')
        require(db.execute('SELECT platform,status FROM release_platform_statuses WHERE release_id=?',(release,)).fetchall()==[('macos','active')],
                'Release is not uniquely published')
        key = db.execute('SELECT id,app_id,public_key_pem,wrapped_private_key FROM managed_signing_keys').fetchall()
        require(len(key)==1 and key[0][1]==state['app_id'] and isinstance(key[0][3],str) and
                len(base64.b64decode(key[0][3],validate=True))>28, 'Missing managed key seed')
        public = re.fullmatch(r'-----BEGIN PUBLIC KEY-----\s+([A-Za-z0-9+/=\s]+)-----END PUBLIC KEY-----\s*',key[0][2])
        require(public is not None, 'Invalid managed public key')
        public_der = base64.b64decode(re.sub(r'\s','',public[1]),validate=True)
        yaml = list((payload/app).rglob('shorebird.yaml'))
        require(len(yaml)==1, 'Missing unique app policy')
        match = re.search(r'^patch_public_key:\s*([^\n]+)$',yaml[0].read_text(),re.M)
        require(match is not None and rsa_public_identity(base64.b64decode(match[1].strip().strip('\"\''),validate=True),spki=False)==
                rsa_public_identity(public_der,spki=True),
                'DB signing key differs from compiled public key')
        release_rows = db.execute('SELECT id,platform,arch,storage_key,hash,size,signing_mode,signing_key_id FROM release_artifacts WHERE release_id=?',(release,)).fetchall()
        # Apple release rows label the uploaded containers, not the wire CPU.
        # The immutable scope independently binds x86_64 and both container IDs.
        require(len(release_rows)==2 and {row[2] for row in release_rows}=={'app','macos_supplement'} and
                all(row[1]=='macos' and row[6:]==('managed',key[0][0]) for row in release_rows),
                'Wrong original managed Mac app/supplement artifacts')
        primary=next(row for row in release_rows if row[2]=='app')
        supplement=next(row for row in release_rows if row[2]=='macos_supplement')
        record=selection['scopes']['macos/x86_64']
        require(record=={'record':{'scope':state['release_scope'],'release_artifact_id':primary[0],
                    'supplement_artifact_id':supplement[0],'published':True},
                    'release_hash':primary[4],'supplement_hash':supplement[4]},
                'Published scope lost original artifact provenance')
        artifacts = [(row[3],row[4],row[5],True) for row in release_rows]
        patches = db.execute('SELECT p.number,a.platform,a.arch,a.storage_key,a.hash,a.size FROM patches p JOIN patch_artifacts a ON a.patch_id=p.id WHERE p.release_id=?',(release,)).fetchall()
        require({p[0] for p in patches}=={1,2,3} and len(patches)==3 and all(p[1:3]==('macos','x86_64') for p in patches), 'Wrong CLI patch inventory')
        expected_mappings={'macos/x86_64/stable/free':{'platform':'macos','arch':'x86_64','channel':'stable',
            'license_type':'free','target':{'kind':'base','patch_number':None,'artifact_hash':None,
                                         'encryption':'none','artifact_key_id':None}}}
        patch_hashes={str(p[0]):p[4] for p in patches}
        for variant,number in (('pro','2'),('enterprise','3')):
            expected_mappings['macos/x86_64/stable/'+variant]={'platform':'macos','arch':'x86_64',
                'channel':'stable','license_type':variant,'target':{'kind':'patch','patch_number':number,
                'artifact_hash':patch_hashes[number],'encryption':'artifact-aes-256-gcm','artifact_key_id':state['key_ids'][variant]}}
        require(selection['mappings']==expected_mappings, 'DB variant mappings differ from prepared CLI artifacts')
        artifacts += [(*p[3:],False) for p in patches]
        require(bool(artifacts), 'Missing actual release objects')
        objects = {}
        for name,digest,size,is_release in artifacts:
            inputs.relative(name)
            storage_path='releases/'+name
            require(storage_path not in objects and isinstance(digest,str) and SHA.fullmatch(digest) and type(size) is int and 0<size<=MAX_FILE,
                    'Invalid DB object reference')
            data = read(payload,'objects/'+storage_path)
            require(len(data)==size, 'Stored object differs from actual CLI upload size')
            # Release uploads are full archives; patch uploads are compressed
            # deltas. Their DB hash authenticates the reconstructed target,
            # whereas sha256 below seals the actual transported download bytes.
            stored_hash=sha(data)
            require(not is_release or stored_hash==digest, 'Original release archive hash mismatch')
            objects[storage_path]={'size':size,'sha256':stored_hash,'artifact_hash':digest}
    return objects


def inspect_bundle(workspace, work, payload, state, certificate):
    root = canonical(state['work_dir']); app = payload/Path(state['released_app']).relative_to(root)
    before = scan(app)
    require(bool(before), 'Missing signed app')
    for name,item in before.items():
        if item['type']!='file': continue
        data = read(app,name)
        if data[:4] in (b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca',b'\xca\xfe\xba\xbf',b'\xbf\xba\xfe\xca'):
            kind = int.from_bytes(data[12:16],'little')
            require(kind in (2,6), 'Unsupported app Mach-O role')
            inputs.macho(data,{inputs.X64},kind)
    inputs.macho(read(app,'Contents/MacOS/'+__import__('plistlib').loads(read(app,'Contents/Info.plist'))['CFBundleExecutable']),{inputs.X64},2)
    inputs.macho(read(app,'Contents/Frameworks/FlutterMacOS.framework/Versions/A/FlutterMacOS'),{inputs.X64},6)
    with fixture_modules(workspace) as (variant,_):
        variant.v1.app_identity(work,state,app)
        require(variant.validate_compiled_scope(dict(state),app)==state['release_scope'], 'Signed app differs from original scope')
        inspection = variant.v1.inspect_app(work,state,app,certificate)
    require(scan(app)==before, 'App changed during verification')
    return {'certificate_sha1':certificate,'team_identifier':inspection['signatures'][0]['team_identifier'],
            'hardened_runtime':True,'app_entitlements':inspection['signatures'][0]['entitlements']}


def validate_payload(payload, manifest, workspace, source, certificate, *, inspect=True, work=None):
    root = canonical(manifest['canonical_root'])
    require(set(manifest)==MANIFEST_KEYS and
            manifest['schema']==SCHEMA and manifest['status']=='prepared_runtime_pending' and manifest['source']==source and
            manifest['certificate_sha1']==certificate and CERT.fullmatch(certificate), 'Prepared source/certificate/schema mismatch')
    state = json_file(payload,'state.json'); app = validate_state(state,root)
    require(manifest['app']==app, 'Wrong app path')
    validate_seed(json_file(payload,'private-bootstrap.json'))
    provenance(json_file(payload,'provenance/build-inputs.json'),json_file(payload,'provenance/server-input.json'),source['components'])
    require(manifest['input_pins']=={name:sha(read(payload,'provenance/'+name+'.json')) for name in ('build-inputs','server-input')},
            'Original externally pinned preparation metadata changed')
    validate_server(read(payload,'runtime/shorebird-server'),json_file(payload,'provenance/server-input.json'))
    for name in NATIVE:
        require(sha(read(payload,name))==state['native_source_hashes'][name[4:]], 'Native source changed')
    require(database_objects(payload,state,app)==manifest['objects'], 'Changed DB/object seed')
    validate_inventory(manifest['files'],app,manifest['objects'])
    found = scan(payload); found.pop(MANIFEST,None)
    require(found==manifest['files'], 'Payload inventory/hash/mode changed')
    with fixture_modules(workspace) as (variant,_):
        signature=manifest['signature']
        require(isinstance(signature,dict) and set(signature)=={'certificate_sha1','team_identifier','hardened_runtime','app_entitlements'} and
                signature['certificate_sha1']==certificate and signature['hardened_runtime'] is True and
                isinstance(signature['team_identifier'],str) and re.fullmatch('[A-Z0-9]{10}',signature['team_identifier']) and
                signature['app_entitlements']==variant.v1.APP_ENTITLEMENTS, 'Invalid strict public signing contract')
        require(variant.validate_compiled_scope(dict(state),payload/app)==state['release_scope'],
                'Compiled app differs from original scope')
    if inspect:
        require(inspect_bundle(workspace,work or payload,payload,state,certificate)==manifest['signature'], 'Strict signing identity changed')
    return state


def prepare(fixture, destination, workspace, commit, components, build_path, build_sha, server_path, server_pin_path, server_pin_sha, certificate):
    source = source_identity(workspace,commit,components)
    require(CERT.fullmatch(certificate), 'Invalid public certificate pin')
    fixture = canonical(str(Path(fixture).absolute()))
    require(fixture==fixture.resolve(), 'Preparation root traverses a symlink')
    absent(fixture/'lifecycle')
    require(not list(fixture.glob('runtime-*.json')), 'Preparation contains prior runtime reports')
    original = json_file(fixture,'state.json'); state = project_state(original,fixture)
    absent(Path.home()/'Library/Containers'/state['bundle_id'])
    build = inputs.pinned_json(build_path,build_sha); server = inputs.pinned_json(server_pin_path,server_pin_sha)
    provenance(build,server,components)
    secrets_input = json_file(fixture,'secrets.json')
    seed = {key:secrets_input[key] for key in SEED_KEYS}; validate_seed(seed)
    destination = Path(destination).absolute()
    require(destination==destination.resolve() and destination.parent.is_dir() and not destination.is_relative_to(fixture) and
            not fixture.is_relative_to(destination), 'Invalid staging destination')
    absent(destination); destination.mkdir(mode=0o700); identity=destination.stat()
    try:
        payload=destination/'payload'; payload.mkdir(mode=0o700)
        app = Path(state['released_app']).relative_to(fixture).as_posix()
        app_inventory = scan(fixture/app)
        for name,item in sorted(app_inventory.items(),key=lambda pair:(len(PurePosixPath(pair[0]).parts),pair[0])):
            path=payload/app/name
            if item['type']=='directory': path.mkdir(parents=True,exist_ok=True,mode=0o700)
            elif item['type']=='file': write(path,read(fixture,app+'/'+name),item['mode'])
        (payload/app).chmod(stat.S_IMODE((fixture/app).lstat().st_mode))
        for name,item in app_inventory.items():
            if item['type']=='link':
                path=payload/app/name; path.parent.mkdir(parents=True,exist_ok=True,mode=0o700); path.symlink_to(item['target'])
        for name,item in app_inventory.items():
            if item['type']=='directory': (payload/app/name).chmod(item['mode'])
        require(scan(payload/app)==app_inventory, 'App changed during private copy')
        for name in ('state.sqlite',*NATIVE): write(payload/name,read(fixture,name))
        for suffix in ('-wal','-shm','-journal'): absent(fixture/('state.sqlite'+suffix))
        # Real CLI/API preparation logins issue refresh-token rows. Remove only
        # that copied login authority; never scrub selection/runtime history to
        # make a previously exercised fixture look pristine.
        with closing(sqlite3.connect(payload/'state.sqlite')) as db, db:
            db.execute('DELETE FROM account_tokens')
        write(payload/'state.json',encoded(state)); write(payload/'private-bootstrap.json',encoded(seed))
        for name,path in (('build-inputs',build_path),('server-input',server_pin_path)):
            path=inputs.regular_path(path)
            write(payload/('provenance/'+name+'.json'),read(path.parent,path.name,inputs.MAX_JSON))
        server_path=inputs.regular_path(server_path)
        data=read(server_path.parent,server_path.name); require(sha(data)==server['binary_sha256'], 'Unpinned server bytes')
        write(payload/'runtime/shorebird-server',data,0o700)
        # Copy only immutable object paths actually referenced by this release.
        with closing(sqlite3.connect((payload/'state.sqlite').as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
            names=db.execute('SELECT storage_key FROM release_artifacts UNION SELECT storage_key FROM patch_artifacts').fetchall()
        for (name,) in names:
            path='objects/releases/'+inputs.relative(name)
            write(payload/path,read(fixture,path))
        objects=database_objects(payload,state,app)
        manifest={'schema':SCHEMA,'status':'prepared_runtime_pending','canonical_root':str(fixture),'source':source,
                  'certificate_sha1':certificate,'signature':inspect_bundle(workspace,destination,payload,state,certificate),
                  'app':app,'objects':objects,'files':scan(payload),
                  'input_pins':{'build-inputs':build_sha,'server-input':server_pin_sha}}
        validate_inventory(manifest['files'],app,objects)
        write(payload/MANIFEST,encoded(manifest))
        validate_payload(payload,manifest,workspace,source,certificate,inspect=False)
        return manifest
    except BaseException:
        remove_owned(destination,identity.st_dev,identity.st_ino,canonical_only=False)
        raise


def package(stage, output, workspace, commit, components, certificate):
    source=source_identity(workspace,commit,components); stage=Path(stage).resolve(); payload=stage/'payload'
    manifest=json_file(payload,MANIFEST); validate_payload(payload,manifest,workspace,source,certificate,work=stage)
    prefix=str(canonical(manifest['canonical_root'])).lstrip('/')+'/'
    output=Path(output).absolute()
    require(output==output.resolve() and not output.is_relative_to(stage), 'Unsafe archive destination')
    absent(output); absent(Path(str(output)+'.sha256'))
    with output.open('xb') as stream,zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as archive:
        os.fchmod(stream.fileno(),0o600)
        for name,item in {**manifest['files'],MANIFEST:{'type':'file','mode':0o600}}.items():
            info=zipfile.ZipInfo(prefix+name+('/' if item['type']=='directory' else '')); info.create_system=3
            info.compress_type=zipfile.ZIP_DEFLATED
            kind={'file':stat.S_IFREG,'directory':stat.S_IFDIR,'link':stat.S_IFLNK}[item['type']]
            info.external_attr=(kind|item.get('mode',0o777))<<16
            archive.writestr(info,item['target'].encode() if item['type']=='link' else
                             b'' if item['type']=='directory' else read(payload,name))
    require(output.stat().st_size<=MAX_ARCHIVE, 'Archive exceeds bound')
    digest=inputs.digest(output)
    write(Path(str(output)+'.sha256'),(digest+'  '+output.name+'\n').encode())
    return digest


def validate_archive(archive, expected_sha, workspace, commit, components, certificate):
    source=source_identity(workspace,commit,components); archive=inputs.regular_path(archive)
    require(SHA.fullmatch(expected_sha) and archive.stat().st_size<=MAX_ARCHIVE and inputs.digest(archive)==expected_sha,
            'External archive hash mismatch')
    with zipfile.ZipFile(archive) as zipped:
        infos=zipped.infolist(); require(0<len(infos)<=MAX_FILES+1, 'Archive count bound')
        require(sum(item.file_size for item in infos)<=MAX_TOTAL+inputs.MAX_JSON, 'Archive expansion bound')
        roots=set()
        for entry in infos:
            name=entry.filename.rstrip('/'); inputs.relative(name)
            require(entry.orig_filename==entry.filename, 'NUL-aliased ZIP member')
            parts=name.split('/'); require(len(parts)>=4 and parts[:2]==['private','tmp'], 'Wrong archive prefix')
            roots.add('/'+'/'.join(parts[:3]))
            require(not entry.flag_bits&1 and entry.file_size<=MAX_FILE and entry.compress_type in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED), 'Unsafe ZIP member')
        require(len(roots)==1, 'Mixed canonical roots'); root=canonical(roots.pop()); prefix=str(root).lstrip('/')+'/'
        names=[entry.filename for entry in infos]; require(len(set(names))==len(names), 'Duplicate ZIP member')
        require(zipped.getinfo(prefix+MANIFEST).file_size<=inputs.MAX_JSON, 'Oversized transport manifest')
        manifest=inputs.strict_json(zipped.read(prefix+MANIFEST))
        require(manifest.get('source')==source and manifest.get('certificate_sha1')==certificate and
                manifest.get('schema')==SCHEMA and manifest.get('status')=='prepared_runtime_pending' and
                manifest.get('canonical_root')==str(root), 'Archive source/certificate/schema mismatch')
        require(set(manifest)==MANIFEST_KEYS, 'Unknown manifest field')
        validate_inventory(manifest['files'],manifest['app'],manifest['objects'])
        expected={prefix+name+('/' if item['type']=='directory' else '') for name,item in manifest['files'].items()}|{prefix+MANIFEST}
        require(set(names)==expected, 'Undeclared/missing archive member')
        for entry in infos:
            name=entry.filename[len(prefix):].rstrip('/')
            item=manifest['files'].get(name,{'type':'file','mode':0o600})
            mode=entry.external_attr>>16
            require(stat.S_IFMT(mode)=={'file':stat.S_IFREG,'directory':stat.S_IFDIR,'link':stat.S_IFLNK}[item['type']] and
                    (stat.S_IMODE(mode)==item.get('mode',0o777)), 'ZIP type/mode mismatch')
            data=zipped.read(entry)
            if name==MANIFEST: continue
            if item['type']=='file': require(len(data)==item['size'] and sha(data)==item['sha256'], 'ZIP content hash mismatch')
            else: require(data==(item['target'].encode() if item['type']=='link' else b''), 'ZIP link/directory bytes changed')
    # Semantic admission, including SQLite, must finish before the canonical
    # runtime root is created. This temporary projection is never executable.
    with tempfile.TemporaryDirectory(prefix='shorebird-macos-app-validation-') as temporary:
        payload=Path(temporary)/'payload'; payload.mkdir(mode=0o700)
        materialize_archive(archive,root,manifest,payload)
        validate_payload(payload,manifest,workspace,source,certificate,inspect=False)
    return root,manifest


def materialize_archive(archive, canonical_root, manifest, destination):
    """Copy only the already verified finite inventory; never ZipFile.extract."""
    prefix=str(canonical_root).lstrip('/')+'/'
    with zipfile.ZipFile(archive) as zipped:
        for name,item in sorted(manifest['files'].items(),key=lambda pair:(len(PurePosixPath(pair[0]).parts),pair[0])):
            path=destination/name
            if item['type']=='directory': path.mkdir(mode=0o700)
            elif item['type']=='file': write(path,zipped.read(prefix+name),item['mode'])
        for name,item in manifest['files'].items():
            if item['type']=='link': (destination/name).symlink_to(item['target'])
        write(destination/MANIFEST,zipped.read(prefix+MANIFEST))
    for name,item in manifest['files'].items():
        if item['type']=='directory': (destination/name).chmod(item['mode'])


def remove_owned(root, device, inode, *, canonical_only=True):
    if canonical_only: canonical(str(root))
    info=Path(root).lstat()
    require(stat.S_ISDIR(info.st_mode) and (info.st_dev,info.st_ino)==(device,inode), 'Owned root identity changed')
    shutil.rmtree(root)


def cleanup(receipt):
    receipt=inputs.regular_path(receipt); data=inputs.strict_json(read(receipt.parent,receipt.name,inputs.MAX_JSON))
    require(set(data)=={'schema','root','device','inode'} and data['schema']=='macos-app-owned-root-v1' and
            type(data['device']) is int and type(data['inode']) is int, 'Invalid ownership receipt')
    root=canonical(data['root'])
    try: root.lstat()
    except FileNotFoundError: return
    remove_owned(root,data['device'],data['inode'])


def extract(archive, expected_sha, workspace, commit, components, certificate, evidence):
    root,manifest=validate_archive(archive,expected_sha,workspace,commit,components,certificate)
    with fixture_modules(workspace) as (_,lifecycle): lifecycle.require_native_host({'arch':'x64'})
    # Unknown permission errors must not be mistaken for an absent container.
    with zipfile.ZipFile(archive) as zipped:
        prefix=str(root).lstrip('/')+'/'
        state=inputs.strict_json(zipped.read(prefix+'state.json')); validate_state(state,root)
    absent(Path.home()/'Library/Containers'/state['bundle_id']); absent(root)
    available_ports(state)
    evidence=Path(evidence).absolute()
    require(evidence==evidence.resolve() and not evidence.is_relative_to(root) and evidence.parent.is_dir(), 'Unsafe ownership evidence path')
    absent(evidence); evidence.mkdir(mode=0o700); receipt=evidence/'owned-root.json'
    root.mkdir(mode=0o700); info=root.stat()
    try:
        write(receipt,encoded({'schema':'macos-app-owned-root-v1','root':str(root),'device':info.st_dev,'inode':info.st_ino}))
        materialize_archive(archive,root,manifest,root)
        state=validate_payload(root,manifest,workspace,manifest['source'],certificate,work=evidence)
        seed=json_file(root,'private-bootstrap.json'); seed['jwt_secret']=secrets.token_urlsafe(48)
        write(root/'secrets.json',encoded(seed))
        return {'status':'verified_extracted_runtime_pending','runtime_acceptance':False,'root':str(root),
                'source_commit':commit,'ownership_receipt':str(receipt)}
    except BaseException:
        remove_owned(root,info.st_dev,info.st_ino)
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ('prepare','package','validate','extract'):
        p=sub.add_parser(command)
        for name in ('workspace','components'): p.add_argument('--'+name,type=Path,required=True)
        for name in ('source-commit','components-sha256','certificate-sha1'): p.add_argument('--'+name,required=True)
        if command=='prepare':
            for name in ('fixture','destination','build-manifest','server','server-pin'): p.add_argument('--'+name,type=Path,required=True)
            for name in ('build-manifest-sha256','server-pin-sha256'): p.add_argument('--'+name,required=True)
        elif command=='package':
            p.add_argument('--stage',type=Path,required=True); p.add_argument('--output',type=Path,required=True)
        else:
            p.add_argument('--archive',type=Path,required=True); p.add_argument('--sha256',required=True)
            if command=='extract': p.add_argument('--evidence',type=Path,required=True)
    p=sub.add_parser('cleanup'); p.add_argument('--ownership-receipt',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='cleanup': cleanup(args.ownership_receipt); result={'status':'owned_root_removed'}
        else:
            components=inputs.pinned_json(args.components,args.components_sha256)
            common=(args.workspace,args.source_commit,components)
            if args.command=='prepare':
                prepare(args.fixture,args.destination,*common,args.build_manifest,args.build_manifest_sha256,
                        args.server,args.server_pin,args.server_pin_sha256,args.certificate_sha1)
                result={'status':'prepared_runtime_pending','runtime_acceptance':False}
            elif args.command=='package': result={'status':'packaged_runtime_pending','sha256':package(args.stage,args.output,*common,args.certificate_sha1),'runtime_acceptance':False}
            elif args.command=='validate':
                validate_archive(args.archive,args.sha256,*common,args.certificate_sha1)
                result={'status':'validated_archive_runtime_pending','runtime_acceptance':False}
            else: result=extract(args.archive,args.sha256,*common,args.certificate_sha1,args.evidence)
        print(json.dumps(result,sort_keys=True))
    except Exception as error:
        parser.exit(1,'Prepared app transport failed: '+type(error).__name__+'\n')


if __name__=='__main__': main()
