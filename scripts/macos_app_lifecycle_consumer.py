#!/usr/bin/env python3
"""Consume a private, externally pinned prepared Mac app; never build or sign.

Commands: download (authenticated unpublished draft only), run (fixed native
Python worker), and _worker (same verified image/process chain). The run path
writes the allowlisted public export and its exact two-file upload inventory.
The reviewed transport owns archive/source/signature admission and root cleanup.
The existing Lifecycle.run_cases/cleanup remain the runtime authority. A separate
control-only absent-key receipt/read/write smoke must pass before all 62 cases.
Public output is a finite authored projection; raw logs/reports/credentials stay
private and are never uploaded by this helper. No service mutation is performed
by download. Runtime execution requires explicit --execute on run/_worker.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import pwd
import re
import secrets
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import macos_app_prepared_transport as transport

require = transport.require
ASSET = 'macos-x64-prepared-app-lifecycle.zip'
SCHEMA = 'macos-app-public-lifecycle-v1'
CONTRACT = 'macos-variant-lifecycle-62-v1'
SELF = 'scripts/macos_app_lifecycle_consumer.py'
STORAGE_HOSTS = {'release-assets.githubusercontent.com', 'objects.githubusercontent.com',
                 'github-releases.githubusercontent.com'}
VARIANTS = ('free', 'pro', 'enterprise')
KEY_ROLES = {'initial-pro-key':('pro','provision'),'initial-enterprise-key':('enterprise','provision'),
    'missing-key-control':('pro','delete'),'missing-key-restore':('pro','provision'),
    'wrong-key-control':('pro','overwrite'),'wrong-key-restore':('pro','overwrite'),
    'final-delete-pro':('pro','delete'),'final-delete-enterprise':('enterprise','delete')}
ERRORS = {'ValueError','OSError','PermissionError','FileNotFoundError','TimeoutExpired',
          'RuntimeError','KeyboardInterrupt','OwnedProcessError','MembershipSnapshotError'}
TRANSFER_CASES = {'transition-1-pro-check':'2','transition-2-enterprise-check':'3',
                 'ciphertext-reauthorized-pro-check':'2','signature-reauthorized-pro-check':'2'}
CACHE_REQUIRED = {'same-content-higher-epoch-check','leased-pro-check'}
MAX_SERVER_LOG = 64*1024**2
MAX_HTTP_WINDOW = 4*1024**2
MAX_PRIVATE_FILES, MAX_PRIVATE_TOTAL, MAX_PRIVATE_FILE = 8000,512*1024**2,64*1024**2


def public_error(error, phase):
    result = {'phase': phase, 'error_type': type(error).__name__ if type(error).__name__ in ERRORS else 'other'}
    if isinstance(error, OSError) and type(error.errno) is int and 0 < error.errno < 4096:
        result['errno'] = error.errno
    return result


def clean_environment(image=None):
    account = pwd.getpwuid(os.getuid())
    result = {'PATH':'/usr/bin:/bin:/usr/sbin:/sbin', 'LANG':'C', 'LC_ALL':'C', 'TZ':'UTC',
              'HOME':account.pw_dir, 'USER':account.pw_name, 'LOGNAME':account.pw_name}
    if image:
        result.update(ImageOS=image['image_os'], ImageVersion=image['image_version'])
    return result


@contextmanager
def clean_process_environment(image):
    previous = dict(os.environ)
    os.environ.clear(); os.environ.update(clean_environment(image))
    try: yield
    finally:
        os.environ.clear(); os.environ.update(previous)


def source_identity(workspace, commit, components):
    identity = transport.source_identity(workspace, commit, components)
    transport.git(workspace, 'ls-files', '--error-unmatch', SELF)
    content = transport.read(workspace, SELF, transport.inputs.MAX_JSON)
    require(content == Path(__file__).read_bytes(), 'Executing consumer differs from the pinned tracked source')
    return dict(identity, consumer_sha256=transport.sha(content))


def native_context(workspace, commit, components, python, python_sha, image_path, image_sha):
    image = transport.inputs.pinned_json(image_path, image_sha)
    require(isinstance(image,dict) and set(image)=={'schema','image_os','image_version','runner_image_commit'} and
            image['schema']=='macos-app-native-image-v1' and
            all(isinstance(image[k],str) and re.fullmatch('[A-Za-z0-9_.-]{1,80}',image[k]) for k in ('image_os','image_version')) and
            isinstance(image['runner_image_commit'],str) and transport.COMMIT.fullmatch(image['runner_image_commit']) and
            os.environ.get('ImageOS')==image['image_os'] and os.environ.get('ImageVersion')==image['image_version'],
            'Actual runner image environment differs from the external image pin')
    # Even read-only git/platform verifier children receive no download/cloud
    # credentials. Preserve actual image metadata only after the external match.
    with clean_process_environment(image):
        source = source_identity(workspace, commit, components)
        with transport.fixture_modules(workspace) as (_, lifecycle):
            lifecycle.require_native_host({'arch':'x64'})
    python = transport.inputs.regular_path(python)
    require(python == Path(sys.executable).resolve() and stat.S_IMODE(python.stat().st_mode) & 0o100,
            'Run under the exact independently pinned native Python executable')
    require(transport.inputs.SHA.fullmatch(python_sha) and transport.inputs.digest(python) == python_sha,
            'Native Python hash changed')
    data = transport.read(python.parent, python.name)
    if data[:4] == b'\xcf\xfa\xed\xfe': transport.inputs.macho(data, {transport.inputs.X64}, 2)
    else: transport.inputs.macho(data, {transport.inputs.X64,transport.inputs.ARM64}, 2, universal=True)
    return source, python, image


def process_chain():
    chain=[]; pid=os.getpid(); seen=set()
    for _ in range(12):
        require(pid not in seen, 'Cyclic host process ancestry'); seen.add(pid)
        result=subprocess.run(['/bin/ps','-p',str(pid),'-o','pid=,ppid=,comm='],
            capture_output=True,check=True,text=True,timeout=2,env=clean_environment())
        fields=result.stdout.strip().split(None,2)
        require(len(fields)==3 and fields[0].isdecimal() and fields[1].isdecimal() and int(fields[0])==pid and
                0<len(fields[2])<=1024 and fields[2].isascii(), 'Unknown actual host process chain')
        parent=int(fields[1]); name=Path(fields[2]).name
        role='python' if name.startswith('python') or name=='Python' else name if name in ('bash','zsh','launchd') else 'other'
        chain.append({'pid':pid,'parent_pid':parent,'role':role,'executable_path_sha256':transport.sha(fields[2].encode())})
        if pid==1 or parent==0: break
        pid=parent
    else: raise ValueError('Unbounded actual host process ancestry')
    require(chain[0]['parent_pid']==os.getppid(), 'Host parent identity changed')
    return chain


def host_metadata():
    version=platform.mac_ver()[0]; kernel=os.uname().release
    require(re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,2}',version) and re.fullmatch('[A-Za-z0-9_.-]{1,80}',kernel),
            'Actual native macOS image version could not be recorded')
    return {'macos_version':version,'kernel_release':kernel,'python_version':list(sys.version_info[:3]),
            'python_path_sha256':transport.sha(str(Path(sys.executable).resolve()).encode())}


def release_asset(data, release_id, asset_id, digest):
    require(isinstance(data,dict) and type(data.get('id')) is int and data['id']==release_id and
            data.get('draft') is True and 'published_at' in data and data['published_at'] is None,
            'Only the exact authenticated unpublished draft is allowed')
    assets=data.get('assets'); require(isinstance(assets,list) and len(assets)<=1000, 'Invalid release membership')
    matches=[a for a in assets if isinstance(a,dict) and a.get('id')==asset_id]
    require(len(matches)==1 and sum(a.get('name')==ASSET for a in assets if isinstance(a,dict))==1,
            'Asset ID/name is not uniquely attached to the draft')
    asset=matches[0]
    require(type(asset.get('id')) is int and asset.get('name')==ASSET and asset.get('state')=='uploaded' and
            asset.get('content_type')=='application/zip' and type(asset.get('size')) is int and
            0<asset['size']<=transport.MAX_ARCHIVE and asset.get('digest') in (None,'sha256:'+digest),
            'Draft asset differs from the external pin')
    return {key:asset.get(key) for key in ('id','name','state','content_type','size','digest','created_at','updated_at')}


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed=urllib.parse.urlsplit(newurl)
        require(parsed.scheme=='https' and not parsed.username and not parsed.password and
                parsed.port in (None,443) and parsed.hostname in STORAGE_HOSTS|{'api.github.com'}, 'Unsafe asset redirect')
        result=super().redirect_request(req,fp,code,msg,headers,newurl)
        require(result is not None, 'Unexpected asset redirect method')
        if parsed.hostname!='api.github.com':
            result.remove_header('Authorization'); result.remove_header('Proxy-Authorization')
        return result


def download(repo, release_id, asset_id, digest, output, evidence):
    require(isinstance(repo,str) and re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo) and
            type(release_id) is int and release_id>0 and type(asset_id) is int and asset_id>0 and
            isinstance(digest,str) and transport.SHA.fullmatch(digest), 'Invalid draft download pins')
    token=os.environ.get('GITHUB_TOKEN'); require(isinstance(token,str) and token, 'Authenticated download requires GITHUB_TOKEN')
    output=Path(output).absolute(); evidence=Path(evidence).absolute()
    require(output==output.resolve() and evidence==evidence.resolve() and output.parent.is_dir() and evidence.parent.is_dir() and
            not output.is_relative_to(evidence), 'Unsafe private download paths')
    transport.absent(output); transport.absent(evidence); evidence.mkdir(mode=0o700)
    report={'schema':'macos-app-draft-download-v1','status':'download_failed','release_id':release_id,'asset_id':asset_id}
    identity=None; phase='metadata_before'
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),SafeRedirect())
    base='https://api.github.com/repos/'+repo
    headers={'Authorization':'Bearer '+token,'X-GitHub-Api-Version':'2022-11-28',
             'Accept':'application/vnd.github+json','User-Agent':'shorebird-prepared-macos-app'}
    def metadata():
        request=urllib.request.Request(base+'/releases/'+str(release_id),headers=headers)
        with opener.open(request,timeout=30) as response:
            raw=response.read(transport.inputs.MAX_JSON+1)
            require(len(raw)<=transport.inputs.MAX_JSON,'Oversized authenticated release metadata')
            return release_asset(transport.inputs.strict_json(raw),release_id,asset_id,digest)
    try:
        before=metadata(); phase='asset_download'; size=0; hashed=hashlib.sha256(); deadline=time.monotonic()+300
        request=urllib.request.Request(base+'/releases/assets/'+str(asset_id),headers={**headers,'Accept':'application/octet-stream'})
        with opener.open(request,timeout=30) as response, output.open('xb') as stream:
            os.fchmod(stream.fileno(),0o600); identity=os.fstat(stream.fileno())
            while block:=response.read(1024**2):
                size+=len(block)
                require(size<=before['size'] and time.monotonic()<=deadline,'Asset transfer bound exceeded')
                stream.write(block); hashed.update(block)
        require(size==before['size'] and hashed.hexdigest()==digest,'Incomplete or changed asset bytes')
        phase='metadata_after'; require(metadata()==before,'Draft publication/membership/asset metadata changed during download')
        report.update(status='download_verified_runtime_pending',archive_sha256=digest,size=size,draft=True,published_at=None)
        return report
    except BaseException as error:
        report['error']=public_error(error,phase)
        if identity is not None:
            info=output.lstat()
            require((info.st_dev,info.st_ino)==(identity.st_dev,identity.st_ino),'Download output ownership changed')
            output.unlink()
        raise
    finally:
        transport.write(evidence/'download.json',transport.encoded(report))


def case_spec():
    specs=[]
    def add(role,variant=None,label=None): specs.append((role,label or re.escape(role),variant))
    for label in ('initial-free','initial-signed-free','initial-cold-free'): add(label,'free')
    add('initial-pro-key',label='key-provision-pro-3'); add('initial-enterprise-key',label='key-provision-enterprise-4')
    current='free'
    for index,target in enumerate(('pro','enterprise','pro','free','enterprise','free'),1):
        for suffix,variant in (('check',current),('cold',target)):
            add(f'transition-{index}-{target}-{suffix}',variant,f'switch-{target}-e([1-9][0-9]{{0,18}})-{suffix}')
        current=target
    for prefix,current in (('same-content-first','free'),('same-content-higher-epoch','pro')):
        add(prefix+'-check',current); add(prefix+'-cold','pro')
    add('offline-paid','pro')
    for prefix in ('missing','wrong'):
        add(prefix+'-key-control'); add(prefix+'-key-free','free'); add(prefix+'-key-restore'); add(prefix+'-key-recovered-pro','pro')
    for prefix in ('ciphertext','signature'):
        add(prefix+'-tamper-fallback','free')
        add(prefix+'-explicit-free-check','free'); add(prefix+'-explicit-free-cold','free')
        add(prefix+'-reauthorized-pro-check','free'); add(prefix+'-reauthorized-pro-cold','pro')
        add(prefix+'-repaired-offline-pro','pro')
    for role,variant in (('leased-pro-check','pro'),('leased-pro-cold','pro'),('lease-valid-offline-pro','pro'),
        ('lease-expired-offline-free','free'),('old-response-rejected','free'),('online-expired-server-free','free'),
        ('cold-server-free','free'),('before-rollback-enterprise-check','free'),('before-rollback-enterprise-cold','enterprise'),
        ('rollback-accept-free','enterprise'),('rollback-cold-free','free'),('rollforward-no-implicit-paid','free'),
        ('rollforward-still-free','free'),('explicit-reauthorization-check','free'),('explicit-reauthorization-cold','enterprise'),
        ('final-free-check','enterprise'),('final-free-cold','free'),('final-offline-free','free')): add(role,variant)
    add('final-delete-pro'); add('final-delete-enterprise')
    require(len(specs)==62,'Trusted case contract changed')
    return specs


def number(value):
    require(type(value) in (int,float) and math.isfinite(value) and value>=0,'Invalid public numeric evidence')
    return value


def transfer_catalog(work, state, manifest):
    """Bind logged storage paths to the sealed DB's actual signed target rows."""
    with closing(sqlite3.connect((work/'state.sqlite').as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        rows=db.execute('SELECT p.number,p.release_id,a.platform,a.arch,a.storage_key,a.hash,a.size,a.hash_signature FROM patches p JOIN patch_artifacts a ON a.patch_id=p.id').fetchall()
    require(len(rows)==3 and {row[0] for row in rows}=={1,2,3},'Missing immutable transfer catalog')
    catalog={}
    for patch,release,platform,arch,key,target_hash,size,signature in rows:
        expected=f'apps/{state["app_id"]}/releases/{release}/patches/{patch}/macos/x86_64/dlc.vmcode'
        require(type(release) is int and release>0 and platform=='macos' and arch=='x86_64' and key==expected,
                'Patch download key differs from actual app/release/wire scope')
        item=manifest['objects']['releases/'+key]
        require(item['size']==size and item['artifact_hash']==target_hash,'Sealed transfer row identity changed')
        require(isinstance(signature,str) and len(base64.b64decode(signature,validate=True))==256,'Missing actual managed RSA patch hash signature')
        catalog[str(patch)]={'path':'/storage/dl/releases/'+key,'size':size,'artifact_hash':target_hash,
            'stored_sha256':item['sha256'],'hash_signature':signature}
    return catalog


def paid_check_target(role):
    if role in TRANSFER_CASES: return TRANSFER_CASES[role]
    match=re.fullmatch('transition-[1-6]-(pro|enterprise|free)-check',role)
    variant=match[1] if match else {'same-content-first-check':'pro','same-content-higher-epoch-check':'pro',
        'leased-pro-check':'pro','before-rollback-enterprise-check':'enterprise','explicit-reauthorization-check':'enterprise'}.get(role)
    return {'pro':'2','enterprise':'3'}.get(variant)


def cache_probe(lifecycle, state, catalog, role, baselines=None):
    patch=paid_check_target(role)
    probe={'role':role,'target_patch_number':patch,'verified_cached':False if patch else None,
        'metadata_base64':None,'metadata_sha256':None,'metadata_size':None,'artifact_sha256':None,'artifact_size':None,
        'baseline_available':False,'pair_present':False,'pair_pristine':False,'survivors_pristine':False}
    if patch is None: return probe
    root=lifecycle.cache_root(state)
    try: raw=transport.read(root,'patches/'+patch+'/state.json',65536)
    except FileNotFoundError: raw=None
    metadata=None
    if raw is not None:
        probe.update(metadata_base64=base64.b64encode(raw).decode(),metadata_sha256=transport.sha(raw),metadata_size=len(raw))
        try: metadata=transport.inputs.strict_json(raw)
        except ValueError: pass
    try: data=transport.read(root,'patches/'+patch+'/dlc.vmcode')
    except FileNotFoundError: data=None
    if data is not None: probe.update(artifact_sha256=transport.sha(data),artifact_size=len(data))
    probe['pair_present']=raw is not None and data is not None
    probe['verified_cached']=probe['pair_present'] and isinstance(metadata,dict) and set(metadata)=={'kind','size','signature'} and metadata['kind']=='Installed' and \
        type(metadata['size']) is int and metadata['size']==len(data) and metadata['signature']==catalog[patch]['hash_signature'] and \
        probe['artifact_sha256']==catalog[patch]['artifact_hash']
    baseline=(baselines or {}).get(patch)
    if baseline:
        probe['baseline_available']=True; probe['baseline']=baseline
        ciphertext=data is None or {'sha256':probe['artifact_sha256'],'size':probe['artifact_size']}==baseline['ciphertext']
        installed=raw is None or {'sha256':probe['metadata_sha256'],'size':probe['metadata_size']}==baseline['metadata']
        probe['survivors_pristine']=ciphertext and installed
        probe['pair_pristine']=probe['pair_present'] and probe['verified_cached'] and probe['survivors_pristine']
    return probe


def transfer_counts(role, probe):
    target=paid_check_target(role)
    require(probe.get('role')==role and probe.get('target_patch_number')==target and
            (type(probe.get('verified_cached')) is bool if target else probe.get('verified_cached') is None),
            'Missing exact prelaunch cache observation')
    if target is None: return (0,)
    if role in ('transition-1-pro-check','transition-2-enterprise-check'):
        require(probe['verified_cached'] is False and not probe.get('pair_present'),'Initial paid transfer was pre-cached')
    if role in TRANSFER_CASES: return (1,)
    require(probe.get('baseline_available') is True and probe.get('survivors_pristine') is True,
            'Surviving future-target ciphertext/Installed metadata changed from validated paid cold baseline')
    if role in CACHE_REQUIRED:
        require(probe.get('pair_pristine') is True,'Required same-current cached pair is absent or changed')
        return (0,)
    return (0,1) if probe.get('pair_pristine') is True else (1,)


def expected_transfer(role, probe):
    return transfer_counts(role,probe)==(1,)


def running_cache(entry, role, catalog, baselines):
    result={'running_patch_number':None,'unchanged':None,'baseline_created':False}
    if entry.get('validated') is not True or entry.get('expected_variant') not in ('pro','enterprise'): return result
    patch={'pro':'2','enterprise':'3'}[entry['expected_variant']]; files=entry['cache']['files']
    pair={'ciphertext':files['patches/'+patch+'/dlc.vmcode'],'metadata':files['patches/'+patch+'/state.json']}
    require(all(isinstance(item,dict) and set(item)=={'size','sha256'} and type(item['size']) is int and item['size']>0 and
        isinstance(item['sha256'],str) and transport.SHA.fullmatch(item['sha256']) for item in pair.values()) and
        pair['ciphertext']['sha256']==catalog[patch]['artifact_hash'],'Running paid ciphertext/cache metadata is unbound')
    if patch not in baselines:
        require(role==('transition-1-pro-cold' if patch=='2' else 'transition-2-enterprise-cold'),
                'Paid cache baseline was not established by its first validated cold launch')
        baselines[patch]={**pair,'anchor_role':role,'pid':entry['pid']}; result['baseline_created']=True
    require(all(pair[key]==baselines[patch][key] for key in ('ciphertext','metadata')),'Running paid artifact or Installed metadata changed')
    return dict(result,running_patch_number=patch,unchanged=True,baseline=baselines[patch],observed=pair)


def log_checkpoint(work, *, allow_absent=False):
    try:
        with transport.inputs.host_file(work,'server.log',MAX_SERVER_LOG) as (stream,info): data=stream.read(MAX_SERVER_LOG+1)
    except FileNotFoundError:
        require(allow_absent,'Missing real server transfer log')
        return None
    require(len(data)==info.st_size and len(data)<=MAX_SERVER_LOG and (not data or data.endswith(b'\n')),
            'Unbounded, growing, or partial server transfer log')
    return {'device':info.st_dev,'inode':info.st_ino,'size':len(data),'sha256':transport.sha(data),'time':time.time()}


def log_window(work, before):
    after=log_checkpoint(work,allow_absent=before is None)
    if before is None:
        require(after is None,'Server log appeared inside an unadmitted transfer window')
        return b'',None
    require(after is not None and (after['device'],after['inode'])==(before['device'],before['inode']) and
            before['size']<=after['size']<=before['size']+MAX_HTTP_WINDOW,'Server transfer log identity/size changed')
    data=transport.read(work,'server.log',MAX_SERVER_LOG)
    require(len(data)==after['size'] and transport.sha(data[:before['size']])==before['sha256'] and
            transport.sha(data)==after['sha256'],'Server transfer log was rewritten or changed during observation')
    return data[before['size']:],after


def transfer_proof(raw, state, catalog, role, pid, before, after, probe=None):
    records=[]
    pattern=re.compile(r'\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} "([A-Z]+) (http://[^ "\r\n]+) HTTP/1\.1" from 127\.0\.0\.1:[1-9][0-9]{0,4} - ([0-9]{3}) ([0-9]+)B in ([0-9.hmsnµμu]+)')
    for line in raw.decode('utf-8').splitlines():
        if '/storage/dl/' not in line: continue
        match=pattern.fullmatch(line)
        require(match is not None,'Unrecognized real storage request completion record')
        method,url,status,size,_=match.groups(); parsed=urllib.parse.urlsplit(url)
        require(method=='GET' and parsed.scheme=='http' and parsed.netloc==urllib.parse.urlsplit(state['api_url']).netloc and
                not parsed.query and not parsed.fragment and not parsed.username and not parsed.password,
                'Storage transfer is not the exact owned path-only loopback request')
        records.append({'path':parsed.path,'status':int(status),'bytes':int(size)})
    target=paid_check_target(role)
    probe=probe if probe is not None else {'role':role,'target_patch_number':target,'verified_cached':False if target else None,'pair_present':False}
    require(len(records) in transfer_counts(role,probe),'Transfer count contradicts first-cold cached-pair/pruning oracle')
    patch=target if records else None
    if patch:
        expected=catalog[patch]
        require(before is not None and after is not None and len(records)==1 and records[0]=={
            'path':expected['path'],'status':200,'bytes':expected['size']} and expected['size']>0,
            'Mandatory real signed patch transfer was cached, failed, partial, duplicated, or wrong-scope')
    else:
        expected=None
        require(not records,'Cached/offline/control launch performed an unexpected patch transfer')
    return {'role':role,'pid':pid,'status':'passed','request_count':len(records),'successful_transfers':len(records),
        'transferred_bytes':sum(r['bytes'] for r in records),'patch_number':patch,
        'expected_size':expected['size'] if expected else 0,
        'target_patch_number':target,'cache_verified':probe['verified_cached'],'cache_probe_canonical_sha256':transport.sha(transport.encoded(probe)),
        'baseline_available':probe.get('baseline_available',False),'pair_present':probe.get('pair_present',False),
        'pair_pristine':probe.get('pair_pristine',False),'survivors_pristine':probe.get('survivors_pristine',False),
        'path_sha256':transport.sha(expected['path'].encode()) if expected else None,
        'artifact_hash':expected['artifact_hash'] if expected else None,'stored_sha256':expected['stored_sha256'] if expected else None,
        'window_sha256':transport.sha(raw),'log_present':before is not None,
        'from_offset':before['size'] if before else 0,'to_offset':after['size'] if after else 0,
        'prefix_before_sha256':before['sha256'] if before else None,'prefix_after_sha256':after['sha256'] if after else None,
        'started':before['time'] if before else None,'completed':after['time'] if after else None,
        'signed_download_grant_validated':bool(patch)}


def measured_lifecycle(base, fixture, lifecycle, catalog):
    """Add observations around original launches; original cases/cleanup unchanged."""
    class Measured(base):
        def __init__(self,*args,**kwargs):
            self.http_transfers=[]; self.paid_cache_baselines={}
            super().__init__(*args,**kwargs)
        def launch(self,label,*args,**kwargs):
            final=label in ('final-delete-pro','final-delete-enterprise')
            index=60 if label=='final-delete-pro' else 61 if label=='final-delete-enterprise' else len(self.launches)
            require(index<len(case_spec()),'Extra measured lifecycle launch')
            role,pattern,_=case_spec()[index]
            require(re.fullmatch(pattern,label),'Unrecognized measured case role')
            probe=cache_probe(lifecycle,self.state,catalog,role,self.paid_cache_baselines)
            checkpoint_error=None
            try: before=log_checkpoint(self.work,allow_absent=final)
            except BaseException as error:
                if not final: raise
                before=None; checkpoint_error=error
            try: return super().launch(label,*args,**kwargs)
            finally:
                if checkpoint_error is not None: raise checkpoint_error
                if self.launches and self.launches[-1].get('label')==label:
                    index=60 if label=='final-delete-pro' else 61 if label=='final-delete-enterprise' else len(self.launches)-1
                    require(index<len(case_spec()),'Extra measured lifecycle launch')
                    role,pattern,_=case_spec()[index]
                    require(re.fullmatch(pattern,label),'Unrecognized measured case role')
                    raw,after=log_window(self.work,before)
                    fixture.v1.shared.private_write(self.work/'lifecycle'/label/'http-window.log',raw)
                    self.write(label+'/http-cache-before.json',probe)
                    try:
                        running=running_cache(self.launches[-1],role,catalog,self.paid_cache_baselines)
                        self.write(label+'/http-running-cache.json',running)
                        proof=transfer_proof(raw,self.state,catalog,role,self.launches[-1]['pid'],before,after,probe)
                        proof.update(running_cache_unchanged=running['unchanged'],running_cache_canonical_sha256=transport.sha(transport.encoded(running)))
                    except BaseException as error:
                        failure={'role':role,'pid':self.launches[-1]['pid'],'status':'failed',
                            'request_count':sum(b'/storage/dl/' in line for line in raw.splitlines()),
                            'window_sha256':transport.sha(raw),'error':public_error(error,'http_transfer')}
                        self.http_transfers.append(failure); self.write(label+'/http-transfer.json',failure)
                        raise
                    self.http_transfers.append(proof); self.write(label+'/http-transfer.json',proof)
    return Measured


def private_name(name):
    """Finite canonical evidence grammar; never payload/seeds/DB/object trees."""
    transport.inputs.relative(name)
    if name in {'state.json','server.log','lifecycle/result.json','lifecycle/signed-bundle.json','lifecycle/signed-bundle-tree.json',
        'lifecycle/lease-expiry.json','lifecycle/replay-requests.json','lifecycle/replay-capture.json',
        'lifecycle/ciphertext-tamper.json','lifecycle/signature-tamper.json','lifecycle/rollback-51.json',
        'lifecycle/rollforward-53.json','lifecycle/server-controller.log',
        'cleanup-evidence/pro-native-key-deletion.json','cleanup-evidence/enterprise-native-key-deletion.json'}: return True
    parts=name.split('/')
    if len(parts)==3 and parts[0]=='lifecycle':
        if parts[2] not in {'launch.json','raw_reports.json','cache.json','signature.json','key-control.json','app.log',
            'variant-status.json','ffi-status.json','container.json','user-data.json','provider.json','http-window.log','http-transfer.json','http-cache-before.json','http-running-cache.json'}: return False
        return any(re.fullmatch(pattern,parts[1]) for _,pattern,_ in case_spec())
    if len(parts)==3 and parts[0]=='signing-evidence' and re.fullmatch('[0-9]{16,20}',parts[1]):
        return parts[2] in {'bundle-verify.log','runner-exports.log','inspection.json'} or bool(re.fullmatch(
            r'(?:0|[1-9][0-9]{0,2})-(?:verify\.log|details\.log|entitlements\.log|certificate\.log|certificate-[0-7]|arch\.log|dependencies\.log)',parts[2]))
    return False


def required_private(launches):
    result={'state.json','server.log','lifecycle/result.json','lifecycle/signed-bundle.json','lifecycle/signed-bundle-tree.json',
        'lifecycle/lease-expiry.json','lifecycle/replay-requests.json','lifecycle/replay-capture.json','lifecycle/ciphertext-tamper.json',
        'lifecycle/signature-tamper.json','lifecycle/rollback-51.json','lifecycle/rollforward-53.json','lifecycle/server-controller.log',
        'cleanup-evidence/pro-native-key-deletion.json','cleanup-evidence/enterprise-native-key-deletion.json'}
    for entry in launches:
        for leaf in ('launch.json','raw_reports.json','cache.json','signature.json','app.log','http-window.log','http-transfer.json','http-cache-before.json','http-running-cache.json'):
            result.add('lifecycle/'+entry['label']+'/'+leaf)
        for leaf in (('key-control.json',) if entry['expected_variant'] is None else
                     ('variant-status.json','ffi-status.json','container.json','user-data.json','provider.json')):
            result.add('lifecycle/'+entry['label']+'/'+leaf)
    return result


def retain_private(work, destination, *, launches=(), complete_required=False):
    """Keep auditable raw bytes privately before wiping disposable seed/root."""
    destination=Path(destination)
    require(destination==destination.resolve() and not destination.is_relative_to(work),'Unsafe private evidence destination')
    transport.absent(destination); destination.mkdir(mode=0o700)
    inventory={'schema':'macos-app-private-canonical-evidence-v1','original_root':str(work),'complete':False,'files':{}}
    total=0; error=None
    try:
        names=[]
        for leaf in ('state.json','server.log'):
            try: (work/leaf).lstat()
            except FileNotFoundError: continue
            names.append(leaf)
        for branch in ('lifecycle','signing-evidence','cleanup-evidence'):
            directory=work/branch
            try: info=directory.lstat()
            except FileNotFoundError: continue
            require(stat.S_ISDIR(info.st_mode),'Redirected canonical evidence tree')
            for root,dirs,files in os.walk(directory,followlinks=False):
                for child in dirs:
                    require(stat.S_ISDIR((Path(root)/child).lstat().st_mode),'Linked canonical evidence directory')
                names.extend((Path(root)/child).relative_to(work).as_posix() for child in files)
                require(len(names)<=MAX_PRIVATE_FILES,'Private evidence file-count bound exceeded')
        transport.inputs.check_names(names)
        for name in sorted(names):
            require(private_name(name),'Unallowlisted canonical private evidence file')
            with transport.inputs.host_file(work,name,MAX_PRIVATE_FILE) as (stream,info): data=stream.read(MAX_PRIVATE_FILE+1)
            require(len(data)==info.st_size and len(data)<=MAX_PRIVATE_FILE and total+len(data)<=MAX_PRIVATE_TOTAL,
                    'Private evidence file/total bound or stability failed')
            digest=transport.sha(data); transport.write(destination/name,data)
            require(transport.sha(transport.read(work,name,MAX_PRIVATE_FILE))==digest,'Canonical evidence changed during private retention')
            inventory['files'][name]={'size':len(data),'sha256':digest}; total+=len(data)
        if complete_required:
            require(required_private(launches)<=inventory['files'].keys(),'Required canonical raw audit evidence is missing')
            for name in ['lifecycle/signed-bundle.json',*('lifecycle/'+entry['label']+'/signature.json' for entry in launches)]:
                inspection=transport.json_file(destination,name)
                directory=Path(inspection['evidence_directory']).relative_to(work).as_posix()
                require(private_name(directory+'/inspection.json') and isinstance(inspection['signatures'],list) and
                        0<len(inspection['signatures'])<=1000,'Invalid canonical signature evidence reference')
                referenced={directory+'/'+leaf for leaf in ('inspection.json','bundle-verify.log','runner-exports.log')}
                for index in range(len(inspection['signatures'])):
                    referenced.update(directory+'/'+str(index)+'-'+leaf for leaf in
                        ('verify.log','details.log','entitlements.log','certificate.log','certificate-0','arch.log','dependencies.log'))
                require(referenced<=inventory['files'].keys(),'Canonical signature tool evidence was not retained')
        inventory['complete']=True
    except BaseException as caught:
        error=caught; inventory['error']=public_error(caught,'private_retention')
    finally:
        raw=transport.encoded(inventory); transport.write(destination/'inventory.json',raw)
    summary={'retained':True,'complete':inventory['complete'],'file_count':len(inventory['files']),
             'total_bytes':total,'inventory_sha256':transport.sha(raw)}
    if error is not None: return summary,error
    return summary,None


def audit_private_transfers(directory, launches, proofs):
    full=transport.read(directory,'server.log',MAX_SERVER_LOG)
    require(len(launches)==len(proofs)==62,'Incomplete retained transfer audit')
    for entry,proof in zip(launches,proofs):
        prefix='lifecycle/'+entry['label']+'/'
        raw=transport.read(directory,prefix+'http-window.log',MAX_HTTP_WINDOW)
        require(raw==full[proof['from_offset']:proof['to_offset']] and transport.sha(raw)==proof['window_sha256'] and
            transport.sha(full[:proof['from_offset']])==proof['prefix_before_sha256'] and
            transport.sha(full[:proof['to_offset']])==proof['prefix_after_sha256'], 'Retained HTTP window does not match complete private server log')
        require(transport.json_file(directory,prefix+'http-transfer.json')==proof and transport.sha(transport.encoded(
            transport.json_file(directory,prefix+'http-cache-before.json')))==proof['cache_probe_canonical_sha256'],
            'Retained transfer/cache receipt changed')
        require(transport.sha(transport.encoded(transport.json_file(directory,prefix+'http-running-cache.json')))==
            proof['running_cache_canonical_sha256'],'Retained running-artifact guard changed')
    return True


def validate_receipt(state, receipt, *, variant, pid, started, ended, absent_only=False):
    require(isinstance(receipt,dict) and set(receipt)=={'schema','action','result','bundle_id','service','session','account','time','pid'} and
        receipt['schema']=='macos-variant-key-control-v2' and receipt['action']=='delete' and
        receipt['bundle_id']==state['bundle_id'] and receipt['service']==state['key_service'] and receipt['session']==state['session'] and
        receipt['account']==state['key_ids'][variant] and type(receipt['pid']) is int and receipt['pid']==pid and
        started<=number(receipt['time'])<=ended+2 and receipt['result'] in (('absent',) if absent_only else ('deleted','absent')),
        'Foreign, stale, or non-pristine native deletion receipt')
    return {'variant':variant,'result':receipt['result'],'pid':pid,'started':started,'completed':ended,
            'receipt_sha256':transport.sha(transport.encoded(receipt)),'identity_verified':True}


def smoke(workspace, work, state, evidence, image):
    """Same native Python reader/image as full lifecycle; no engine or paid key."""
    results=[]
    with transport.fixture_modules(workspace) as (fixture,lifecycle):
        owner=lifecycle.OwnedHostProcesses(); app=Path(state['released_app']); tree=lifecycle.bundle_tree(app)
        require(tree,'Missing verified app tree')
        try:
            for variant in ('pro','enterprise'):
                path=fixture.v1.container_file(state,'key-control-'+state['key_ids'][variant]+'.json')
                transport.absent(path); started=time.time()
                env=fixture.app_environment(work,state,variant=variant,action='delete',control_only=True)
                require(set(env)<=set(clean_environment(image))|{'OPEN_SHOREBIRD_TEST_KEY_ACTION','OPEN_SHOREBIRD_TEST_KEY_ID','OPEN_SHOREBIRD_TEST_KEY_CONTROL_ONLY'},
                        'Unexpected native smoke environment')
                with (evidence/('smoke-'+variant+'.log')).open('xb') as log:
                    os.fchmod(log.fileno(),0o600)
                    process=owner.spawn(fixture.app_launch_command(work,state,deletion=True),env=env,stdout=log,stderr=log)
                    require(process.wait(timeout=30)==0,'Native receipt smoke failed')
                    ended=time.time(); raw=transport.read(path.parent,path.name,16384)
                    require(path.stat().st_mtime>=started,'Native smoke receipt predates launch')
                    receipt=transport.inputs.strict_json(raw)
                    results.append(validate_receipt(state,receipt,variant=variant,pid=process.pid,started=started,ended=ended,absent_only=True))
                    transport.write(evidence/('smoke-'+variant+'-receipt.json'),raw)
                    owner.stop(process)
                require(lifecycle.bundle_tree(app)==tree,'Signed app changed during capability smoke')
            marker=fixture.v1.container_file(state,'capability-'+secrets.token_hex(16)+'.json')
            data=secrets.token_bytes(32); transport.write(marker,data)
            info=marker.lstat()
            try: require(transport.read(marker.parent,marker.name,64)==data,'Owned container marker readback failed')
            finally:
                current=marker.lstat()
                require((current.st_dev,current.st_ino)==(info.st_dev,info.st_ino),'Owned marker identity changed')
                marker.unlink()
            transport.absent(lifecycle.cache_root(state))
            for name in ('variant-status.json','ffi-status.json','provider.json','container.json','user-data.json'):
                transport.absent(fixture.v1.container_file(state,name))
            return {'status':'capability_smoke_passed','runtime_acceptance':False,'receipts':results,
                    'owned_marker_read_write_removed':True,'same_native_python_reader':True}
        finally: owner.close()


def public_launch(entry, state, index, fixture, lifecycle, previous):
    require(index<len(case_spec()),'Extra lifecycle launch')
    role,pattern,variant=case_spec()[index]
    matched=re.fullmatch(pattern,entry.get('label',''))
    require(matched and entry.get('validated') is True and entry.get('signed_bundle_unchanged') is True and
            type(entry.get('pid')) is int and entry['pid']>0 and entry.get('expected_variant')==variant,
            'Missing, reordered, failed, or substituted lifecycle case')
    result={'role':role,'status':'passed','pid':entry['pid'],'started':number(entry['started']),'signed_bundle_unchanged':True}
    if variant is None:
        receipt=entry['key_control']; account=receipt.get('account')
        key_variant=next((v for v in ('pro','enterprise') if state['key_ids'][v]==account),None)
        require((key_variant,receipt.get('action'))==KEY_ROLES[role],'Foreign or substituted lifecycle key control')
        require(receipt.get('schema')=='macos-variant-key-control-v2' and receipt.get('session')==state['session'] and
                receipt.get('bundle_id')==state['bundle_id'] and receipt.get('service')==state['key_service'] and
                receipt.get('pid')==entry['pid'] and entry['started']<=number(receipt['time'])<=time.time()+2 and
                receipt.get('result') in (('absent','deleted') if receipt['action']=='delete' else ('stored',)),
                'Invalid lifecycle native key receipt')
        if role.startswith('final-delete-'): fixture.validate_key_deletion(state,receipt,key_variant)
        result['key_control']={'variant':key_variant,'action':receipt['action'],'result':receipt['result'],'identity_verified':True}
        return result,previous
    report=entry['report']; require(report['pid']==entry['pid'] and report['session']==state['session'] and report['variant']==variant and
        entry['started']<=number(report['started'])<=time.time()+2 and
        report['new_code_result']=={'free':'free:base','pro':'pro:42','enterprise':'enterprise:24'}[variant],
                                   'Unbound public runtime report')
    selection=report['selection']; current=fixture.selection_identity(selection['current']); accepted=fixture.selection_identity(selection['accepted'])
    require(not matched.groups() or matched[1]==accepted['epoch'],'Dynamic case epoch differs from actual accepted identity')
    next_boot=selection['next_boot']
    require(set(next_boot)=={'variant_id','license_type','patch_number','reason'} and next_boot['variant_id'] in VARIANTS and
        next_boot['license_type']==next_boot['variant_id'] and next_boot['patch_number']==fixture.EXPECTED_NUMBERS[next_boot['variant_id']] and
        next_boot['reason'] in ('no_authorization','expired','not_yet_valid','selected_base','ready','quarantined','content_unavailable') and
        type(selection['restart_required']) is bool and report['stage'] in ('boot','checked') and
        report['update_status'] in (None,'upToDate','restartRequired') and report['error'] is None,'Invalid public updater status')
    ffi=fixture.v1.shared.validate_launch_report(transport.encoded(entry['ffi']['verification']).decode(),state)
    require(ffi['phase']==('base' if variant=='free' else 'update'),'FFI phase differs from actual running variant')
    fixture.v1.validate_container_record(state,entry['container'],entry['started'])
    require(entry['container']['pid']==entry['pid'],'Foreign native container PID')
    counts=lifecycle.validate_provider_ledger(state,report,entry['provider'],pid=entry['pid'],nonce=entry['provider_nonce'],
        after=entry['started'],key_failure='missing' if role=='missing-key-free' else 'wrong' if role=='wrong-key-free' else None)
    require(counts==entry['provider_counts'],'Provider count mismatch')
    data=report['data']; history=data['history']
    require(data['session']==state['session'] and data['sentinel']==state['sentinel'] and type(data['schema']) is int and
        type(data['launches']) is int and isinstance(history,list) and 1<=len(history)<=100 and
        data['launches']==len(history) and all(v in VARIANTS for v in history) and history[-1]==variant and
        (previous is None and data['launches']==1 or previous is not None and history==previous['history']+[variant] and
         data['schema']>=previous['schema']), 'Data continuity predicate failed')
    installation=report['installation_id']; require(isinstance(installation,str) and 1<=len(installation)<=256,'Invalid installation identity')
    if previous: require(previous['installation_id']==installation,'Installation identity changed')
    result.update(variant=variant,current=current,accepted=accepted,next_boot={k:next_boot[k] for k in sorted(next_boot)},
        restart_required=selection['restart_required'],stage=report['stage'],update_status=report['update_status'],
        code_predicate_verified=True,ffi={'phase':ffi['phase'],'checks':ffi['checks'],'check_count':len(ffi['checks']),
            'structural_verified':True,'ffi_verified':True,'rendered_predicate_verified':True},
        provider={**counts,'identity_verified':True},data={'schema':data['schema'],'launches':data['launches'],
            'history_count':len(history),'history_sha256':transport.sha(transport.encoded(history)),
            'sentinel_sha256':transport.sha(state['sentinel'].encode()),'session_sha256':transport.sha(state['session'].encode()),
            'installation_sha256':transport.sha(installation.encode()),'continuity_verified':True})
    return result,{'history':history,'schema':data['schema'],'installation_id':installation}


def auxiliary_evidence(work):
    lease=transport.json_file(work,'lifecycle/lease-expiry.json')
    require(set(lease)=={'deadline','wait_completed','launch_started'} and
            number(lease['deadline'])<number(lease['wait_completed'])<=number(lease['launch_started']),'Unproven actual lease expiry')
    requests=transport.json_file(work,'lifecycle/replay-requests.json')
    require(isinstance(requests,list) and 0<len(requests)<=128,'Missing completed replay delivery')
    public=[]
    for request in requests:
        require(number(request['received_at'])<=number(request['completed_at']) and all(
            isinstance(request[k],str) and transport.SHA.fullmatch(request[k]) for k in ('request_body_sha256','response_body_sha256')),
            'Invalid completed replay hashes/timing')
        public.append({k:request[k] for k in ('request_body_sha256','response_body_sha256','received_at','completed_at')})
    return {'lease':lease,'replay':{'completed_deliveries':len(public),'request_response_hashes':public,
            'control':'combined_old_nonce_and_epoch','authority_and_journal_preserved':True}}


def preparation_public(work):
    build=transport.json_file(work,'provenance/build-inputs.json'); server=transport.json_file(work,'provenance/server-input.json')
    return {'host_producer':build['host']['producer'],'target_producer':build['target']['producer'],
        'target_archive_sha256':build['target']['archive_sha256'],'server_producer':server['producer'],
        'server_archive_sha256':server['archive_sha256'],'server_commit':server['server_commit']}


def cleanup_public(value):
    require(isinstance(value,dict),'Missing canonical cleanup result')
    flags={key:value.get(key) is True for key in ('all_variant_keys_deleted','bootstrap_secrets_removed','owned_app_removed',
        'container_payload_removed','container_removed','cleanup_complete')}
    error=value.get('metadata_removal_errno')
    require(error is None or type(error) is int and error in (1,13),'Invalid metadata cleanup errno')
    return dict(flags,metadata_removal_errno=error,os_metadata_remainder=bool(value.get('os_metadata_shell_remaining')))


def public_base(manifest, source, python_sha, image, chain):
    return {'schema':SCHEMA,'case_contract':CONTRACT,'status':'runtime_failed','runtime_acceptance':False,
        'source':source,'target':{'platform':'macos','arch':'x64','machine':'x86_64','translated':False},
        'python_sha256':python_sha,'host':host_metadata(),'image':image,'process_chain':chain,'signature':manifest['signature'],
        'scope_sha256':None,'bundle_inventory_sha256':transport.sha(transport.encoded({k:v for k,v in manifest['files'].items() if k.startswith(manifest['app']+'/')})),
        'input_pins':manifest['input_pins'],'cases':[],'capability_smoke':{'status':'not_started'},
        'outer_root_removed':False}


def write_public(directory, report):
    require(Path(directory).is_dir(),'Missing owned public export directory')
    data=transport.encoded(report)
    require(len(data)<=2*1024**2,'Public export bound exceeded')
    temporary=Path(directory)/('public-'+secrets.token_hex(12)+'.tmp')
    transport.write(temporary,data); os.replace(temporary,Path(directory)/'public.json')
    inventory={'schema':'macos-app-public-export-inventory-v1','files':{'public.json':{'size':len(data),'sha256':transport.sha(data)}}}
    temporary=Path(directory)/('inventory-'+secrets.token_hex(12)+'.tmp')
    transport.write(temporary,transport.encoded(inventory)); os.replace(temporary,Path(directory)/'inventory.json')


def validate_public(report, commit, python_sha):
    require(report.get('schema')==SCHEMA and report.get('case_contract')==CONTRACT and
        report.get('runtime_acceptance') is True and report.get('status')=='runtime_passed' and
        report.get('outer_root_removed') is True and report.get('source',{}).get('workspace_commit')==commit and
        report.get('python_sha256')==python_sha and report.get('signing_mode')=='verify_presigned' and
        report.get('bundle_verified') is True and report.get('target')=={'platform':'macos','arch':'x64','machine':'x86_64','translated':False},
        'Public evidence lacks native pinned pre-signed acceptance')
    capability=report.get('capability_smoke',{}); receipts=capability.get('receipts',[])
    require(capability.get('status')=='capability_smoke_passed' and capability.get('owned_marker_read_write_removed') is True and
        capability.get('same_native_python_reader') is True and len(receipts)==2 and
        [r.get('variant') for r in receipts]==['pro','enterprise'] and
        all(r.get('result')=='absent' and r.get('identity_verified') is True for r in receipts), 'Missing native capability smoke')
    cases=report.get('cases'); require(isinstance(cases,list) and len(cases)==62,'Missing exact full case inventory')
    for entry,(role,_,variant) in zip(cases,case_spec()):
        require(entry.get('role')==role and entry.get('status')=='passed' and entry.get('signed_bundle_unchanged') is True,
                'Failed or substituted public lifecycle case')
        if variant is not None:
            require(entry.get('variant')==variant and entry.get('ffi',{}).get('check_count')==(3 if variant=='free' else 16) and
                entry.get('provider',{}).get('identity_verified') is True and entry.get('data',{}).get('continuity_verified') is True,
                'Missing original FFI/provider/data predicates')
    cleanup=report.get('cleanup',{})
    require(all(cleanup.get(k) is True for k in ('all_variant_keys_deleted','bootstrap_secrets_removed','owned_app_removed',
        'container_payload_removed','container_removed','cleanup_complete')) and not cleanup.get('os_metadata_remainder'),
        'Public evidence has incomplete native cleanup')
    require(report.get('replay',{}).get('completed_deliveries',0)>0 and report['replay'].get('authority_and_journal_preserved') is True and
        report['replay'].get('control')=='combined_old_nonce_and_epoch','Missing completed actual replay control')
    lease=report['lease']; require(number(lease['deadline'])<number(lease['wait_completed'])<=number(lease['launch_started']),
                                  'Missing actual lease deadline observation')
    transfers=report.get('http_transfers',[])
    require(len(transfers)==62,'Missing real transfer windows')
    for proof,entry in zip(transfers,cases):
        target=paid_check_target(entry['role'])
        probe={'role':entry['role'],'target_patch_number':target,'verified_cached':proof.get('cache_verified'),
            **{key:proof.get(key) for key in ('baseline_available','pair_present','pair_pristine','survivors_pristine')}}
        require(proof.get('request_count') in transfer_counts(entry['role'],probe),'Transfer count contradicts cached-pair oracle')
        patch=target if proof['request_count']==1 else None
        require(proof.get('role')==entry['role'] and proof.get('pid')==entry['pid'] and proof.get('status')=='passed' and proof.get('log_present') is True and
            proof.get('request_count')==(1 if patch else 0) and proof.get('successful_transfers')==(1 if patch else 0) and
            proof.get('patch_number')==patch and proof.get('signed_download_grant_validated') is bool(patch) and
            (number(proof['transferred_bytes'])==number(proof['expected_size'])>0 and all(isinstance(proof.get(k),str) and
             transport.SHA.fullmatch(proof[k]) for k in ('path_sha256','artifact_hash','stored_sha256','window_sha256'))
             if patch else proof['transferred_bytes']==proof['expected_size']==0),
            'Cached/offline or real signed transfer proof failed')
        require(all(isinstance(proof.get(k),str) and transport.SHA.fullmatch(proof[k]) for k in
            ('window_sha256','cache_probe_canonical_sha256','prefix_before_sha256','prefix_after_sha256','running_cache_canonical_sha256')) and
            type(proof['from_offset']) is int and type(proof['to_offset']) is int and 0<=proof['from_offset']<=proof['to_offset'] and
            number(proof['started'])<=number(entry['started'])<=number(proof['completed']), 'Unbound transfer log fences/hash receipts')
        require(proof.get('running_cache_unchanged') is (True if entry.get('variant') in ('pro','enterprise') else None),
                'Running paid artifact was not protected')
    retained=report.get('private_evidence',{})
    require(retained.get('retained') is True and retained.get('complete') is True and retained.get('file_count',0)>0 and
        isinstance(retained.get('inventory_sha256'),str) and transport.SHA.fullmatch(retained['inventory_sha256']) and
        report.get('private_transfer_audit_verified') is True,
        'Private canonical raw audit evidence was not retained')
    return report


def fixed_worker_command(args, python):
    return [str(python),'-I',str(Path(args.workspace).resolve()/SELF),'_worker','--execute',
        '--workspace',str(Path(args.workspace).resolve()),'--source-commit',args.source_commit,
        '--components',str(Path(args.components).resolve()),'--components-sha256',args.components_sha256,
        '--certificate-sha1',args.certificate_sha1,'--archive',str(Path(args.archive).resolve()),'--sha256',args.sha256,
        '--python',str(python),'--python-sha256',args.python_sha256,'--image',str(Path(args.image).resolve()),
        '--image-sha256',args.image_sha256,'--evidence',str(Path(args.evidence).absolute())]


def worker(args):
    require(args.execute,'Explicit execution is required')
    components=transport.inputs.pinned_json(args.components,args.components_sha256)
    source,python,image=native_context(args.workspace,args.source_commit,components,args.python,args.python_sha256,args.image,args.image_sha256)
    evidence=Path(args.evidence).absolute()
    require(evidence==evidence.resolve() and evidence.parent.is_dir(),'Unsafe private consumer evidence directory')
    transport.absent(evidence); evidence.mkdir(mode=0o700)
    public=evidence/'public'; public.mkdir(mode=0o700)
    root=None; receipt=None; runner=None; report=None; phase='archive_admission'; state=None; smoke_passed=False; case_role=None
    def stop(*_): raise KeyboardInterrupt
    previous_handlers={name:signal.signal(name,stop) for name in (signal.SIGTERM,signal.SIGINT)}
    try:
        with clean_process_environment(image):
            chain=process_chain()
            root,manifest=transport.validate_archive(args.archive,args.sha256,args.workspace,args.source_commit,components,args.certificate_sha1)
            report=public_base(manifest,source,args.python_sha256,image,chain)
            transport.extract(args.archive,args.sha256,args.workspace,args.source_commit,components,args.certificate_sha1,evidence/'transport')
            receipt=evidence/'transport/owned-root.json'; state=transport.json_file(root,'state.json')
            report['scope_sha256']=transport.sha(transport.encoded(state['release_scope']))
            report['scope']=dict(state['release_scope']); report['preparation']=preparation_public(root)
            report['native_source_hashes']=dict(state['native_source_hashes'])
            phase='protected_container_access_unproven'; report['capability_smoke']={'status':'started','started':time.time()}; write_public(public,report)
            report['capability_smoke']=smoke(args.workspace,root,state,evidence,image)
            smoke_passed=True; write_public(public,report)
            phase='lifecycle'
            with transport.fixture_modules(args.workspace) as (fixture,lifecycle):
                lifecycle.require_fresh_fixture(root,state,preserve_scope=True)
                catalog=transfer_catalog(root,state,manifest)
                measured=measured_lifecycle(lifecycle.Lifecycle,fixture,lifecycle,catalog)
                runner=measured(root,state,args.certificate_sha1,verify_presigned=True)
                error=None
                try: runner.run_cases()
                except BaseException as caught: error=caught
                finally: cleanup_errors=runner.cleanup()
                if error is not None: report['canonical_error']=public_error(error,'lifecycle')
                report['http_transfers']=runner.http_transfers
                previous=None
                # Canonical validation happened during each launch. Re-project
                # with that launch's accepted identity state, not final grants.
                for index,entry in enumerate(runner.launches):
                    case_role=case_spec()[index][0] if index<len(case_spec()) else None
                    public_entry,previous=public_launch(entry,runner.state,index,fixture,lifecycle,previous)
                    report['cases'].append(public_entry)
                report['cases_completed']=len(report['cases'])==62 and runner.result.get('runtime_acceptance') is True
                report['signing_mode']='verify_presigned' if runner.result.get('signing_mode')=='verify_presigned' else 'unverified'
                report['bundle_verified']=runner.result.get('bundle_verified') is True
                if isinstance(runner.result.get('cleanup'),dict): report['cleanup']=cleanup_public(runner.result['cleanup'])
                require(error is None and not cleanup_errors and len(report['cases'])==62 and
                        runner.result.get('runtime_acceptance') is True and runner.result.get('signing_mode')=='verify_presigned' and
                        runner.result.get('bundle_verified') is True,'Full canonical lifecycle or cleanup did not pass')
                require(len(runner.http_transfers)==62 and all(proof['log_present'] is True and proof['role']==entry['role'] and
                    proof['pid']==entry['pid'] for proof,entry in zip(runner.http_transfers,report['cases'])),
                    'Missing exact per-launch real HTTP transfer observations')
                for item in catalog.values():
                    data=transport.read(root,'objects/'+item['path'].removeprefix('/storage/dl/'))
                    require(len(data)==item['size'] and transport.sha(data)==item['stored_sha256'],'Immutable signed-transfer object bytes changed')
                exports=transport.json_file(root,'lifecycle/signed-bundle.json')['exports']
                require(exports==list(fixture.v1.shared.FFI_EXPORTS),'Missing verified released native FFI exports')
                report['verified_ffi_exports']=list(exports)
                report.update(auxiliary_evidence(root)); phase='native_cleanup'
                require(all(report['cleanup'][k] for k in ('all_variant_keys_deleted','bootstrap_secrets_removed','owned_app_removed',
                    'container_payload_removed','container_removed','cleanup_complete')),'Canonical cleanup is incomplete')
                report.update(status='runtime_passed',runtime_acceptance=True,signing_mode='verify_presigned',bundle_verified=True)
            phase='finished'
    except BaseException as error:
        if report is None: report={'schema':SCHEMA,'status':'runtime_failed','runtime_acceptance':False,'cases':[],'outer_root_removed':False}
        report['error']=public_error(error,phase)
        if case_role is not None: report['error']['case_role']=case_role
        report['status']='protected_container_access_unproven' if phase=='protected_container_access_unproven' else 'runtime_failed'
        report['runtime_acceptance']=False
    finally:
        if report is None: report={'schema':SCHEMA,'status':'runtime_failed','runtime_acceptance':False,'cases':[],'outer_root_removed':False}
        if runner is None and smoke_passed:
            try:
                with transport.fixture_modules(args.workspace) as (fixture,_):
                    removed=fixture.v1.remove_owned_container(fixture.v1.container_root(state))
                report['pre_lifecycle_cleanup']={'keys_ever_provisioned':False,'both_absent_receipts_verified':True,
                    'container_payload_removed':removed['container_payload_removed'],'container_removed':removed['container_removed']}
            except BaseException as error: report['pre_lifecycle_cleanup_error']=public_error(error,'pre_lifecycle_cleanup')
        elif runner is None and receipt is not None:
            report['container_cleanup_verified']=False
        if receipt is not None:
            try:
                retained,retention_error=retain_private(root,evidence/'private-canonical',launches=runner.launches if runner else (),
                    complete_required=report.get('runtime_acceptance') is True)
                report['private_evidence']=retained
                if retention_error is not None: raise retention_error
                if report.get('runtime_acceptance') is True:
                    report['private_transfer_audit_verified']=audit_private_transfers(evidence/'private-canonical',runner.launches,runner.http_transfers)
            except BaseException as error:
                report['private_retention_error']=public_error(error,'private_retention')
                report['runtime_acceptance']=False; report['status']='private_evidence_incomplete'
        diagnostics={}
        for name,path in (('lifecycle_result',evidence/'private-canonical/lifecycle/result.json'),
                          ('smoke_pro_log',evidence/'smoke-pro.log'),('smoke_enterprise_log',evidence/'smoke-enterprise.log')):
            if path is None: continue
            try: path.lstat()
            except OSError: continue
            try: diagnostics[name]=transport.inputs.digest(path)
            except (OSError,ValueError): continue
        report['private_diagnostic_sha256']=diagnostics
        try: write_public(public,report)
        except Exception as error:
            report['export_error']=public_error(error,'public_export'); report['runtime_acceptance']=False; report['status']='export_failed'
        if receipt is not None:
            try:
                transport.cleanup(receipt); report['outer_root_removed']=True
            except BaseException as error:
                report['outer_cleanup_error']=public_error(error,'outer_cleanup'); report['runtime_acceptance']=False; report['status']='cleanup_incomplete'
        try: write_public(public,report)
        finally:
            for name,handler in previous_handlers.items(): signal.signal(name,handler)
    return report


def run(args):
    require(args.execute,'Explicit execution is required')
    components=transport.inputs.pinned_json(args.components,args.components_sha256)
    _,python,image=native_context(args.workspace,args.source_commit,components,args.python,args.python_sha256,args.image,args.image_sha256)
    evidence=Path(args.evidence).absolute()
    require(evidence==evidence.resolve() and evidence.parent.is_dir(),'Unsafe controller evidence path')
    transport.absent(evidence)
    log=evidence.with_name(evidence.name+'-controller.log'); transport.absent(log)
    error=None; code=None; controller_closed=False
    with clean_process_environment(image),transport.fixture_modules(args.workspace) as (_,lifecycle),log.open('xb') as stream:
        os.fchmod(stream.fileno(),0o600); owner=lifecycle.OwnedHostProcesses(term_timeout=120,kill_timeout=10)
        try:
            child=owner.spawn(fixed_worker_command(args,python),env=clean_environment(image),stdout=stream,stderr=stream)
            deadline=time.monotonic()+7200
            while child.poll() is None:
                require(time.monotonic()<deadline,'Native lifecycle controller deadline exceeded')
                try: checkpoint=transport.json_file(evidence,'public/public.json')
                except FileNotFoundError: checkpoint={}
                capability=checkpoint.get('capability_smoke',{})
                if capability.get('status')=='started':
                    require(time.time()-number(capability['started'])<=90,'Protected container capability deadline exceeded')
                time.sleep(.1)
            code=child.wait(timeout=1)
        except BaseException as caught: error=caught
        finally:
            try: owner.close(); controller_closed=True
            except BaseException as caught: error=error or caught
    try: report=transport.json_file(evidence,'public/public.json')
    except (FileNotFoundError,ValueError):
        # A worker rejected before root creation has no runtime cleanup work.
        # This new directory contains authored controller failure evidence only.
        evidence.mkdir(mode=0o700,exist_ok=True); (evidence/'public').mkdir(mode=0o700,exist_ok=True)
        report={'schema':SCHEMA,'status':'runtime_failed','runtime_acceptance':False,'outer_root_removed':False,'cases':[]}
    report['controller_diagnostic_sha256']=transport.inputs.digest(log)
    if controller_closed and report.get('outer_root_removed') is not True:
        receipt=evidence/'transport/owned-root.json'
        try: receipt.lstat()
        except FileNotFoundError: pass
        else:
            report.update(runtime_acceptance=False,status='cleanup_incomplete',native_cleanup_verified=False)
            try:
                owned=transport.json_file(receipt.parent,receipt.name)
                require(set(owned)=={'schema','root','device','inode'} and owned['schema']=='macos-app-owned-root-v1',
                        'Invalid owned private-retention receipt')
                root=transport.canonical(owned['root']); info=root.lstat()
                require(stat.S_ISDIR(info.st_mode) and (info.st_dev,info.st_ino)==(owned['device'],owned['inode']),
                        'Private-retention root ownership changed')
                retained,retention_error=retain_private(root,evidence/'private-canonical-controller')
                report['controller_private_evidence']=retained
                if retention_error is not None: report['controller_private_retention_error']=public_error(retention_error,'controller_private_retention')
            except Exception as caught: report['controller_private_retention_error']=public_error(caught,'controller_private_retention')
            write_public(evidence/'public',report)
            try: transport.cleanup(receipt); report['outer_root_removed']=True
            except Exception as caught: report['outer_cleanup_error']=public_error(caught,'outer_cleanup')
    if error is not None:
        report.update(status='controller_failed',runtime_acceptance=False,controller_error=public_error(error,'controller'))
        write_public(evidence/'public',report)
        raise ValueError('Native controller or owned cleanup did not finish') from None
    write_public(evidence/'public',report)
    require(code==0,'Native consumer process failed')
    validate_public(report,args.source_commit,args.python_sha256)
    return {'schema':SCHEMA,'status':'runtime_passed','runtime_acceptance':True,'public_evidence':str(evidence/'public')}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__); sub=parser.add_subparsers(dest='command',required=True)
    fetch=sub.add_parser('download'); fetch.add_argument('--repository',required=True)
    for name in ('release-id','asset-id'): fetch.add_argument('--'+name,type=int,required=True)
    fetch.add_argument('--sha256',required=True)
    for name in ('output','evidence'): fetch.add_argument('--'+name,type=Path,required=True)
    for command in ('run','_worker'):
        p=sub.add_parser(command); p.add_argument('--execute',action='store_true')
        for name in ('workspace','components','archive','python','image','evidence'): p.add_argument('--'+name,type=Path,required=True)
        for name in ('source-commit','components-sha256','certificate-sha1','sha256','python-sha256','image-sha256'): p.add_argument('--'+name,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='download': result=download(args.repository,args.release_id,args.asset_id,args.sha256,args.output,args.evidence)
        elif args.command=='run': result=run(args)
        else:
            report=worker(args); result={'schema':SCHEMA,'status':report['status'],'runtime_acceptance':report['runtime_acceptance']}
        print(json.dumps(result,sort_keys=True))
        return 0 if args.command=='download' or result['runtime_acceptance'] else 1
    except Exception as error:
        parser.exit(1,'Prepared app consumer failed: '+public_error(error,'controller')['error_type']+'\n')


if __name__=='__main__': raise SystemExit(main())
