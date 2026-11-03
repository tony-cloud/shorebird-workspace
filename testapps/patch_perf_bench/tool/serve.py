#!/usr/bin/env python3
"""Disposable local Shorebird API for the patch-perf benchmark.

Reuses the prebuilt private server binary and mirrors the bootstrap used by
testapps/license_flavor_patch_test/tool/ios_variant_e2e.py serve --api-only
(sqlite + local object storage, bootstrap admin, one org/app). Writes
state.json, secrets.json and the app's shorebird.yaml into --work.
"""
import argparse, base64, json, os, secrets, signal, socket, subprocess, sys, time, urllib.request
from pathlib import Path

def req(base, method, path, body=None, token=None):
    r = urllib.request.Request(base + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + token} if token else {})})
    with urllib.request.urlopen(r, timeout=20) as f:
        d = f.read()
        return json.loads(d) if d else {}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', type=Path, required=True)
    ap.add_argument('--server', type=Path, required=True)
    ap.add_argument('--app', type=Path, required=True)
    ap.add_argument('--host', default='192.168.93.205')
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--extra-yaml', default='')
    a = ap.parse_args()
    w = a.work; w.mkdir(parents=True, exist_ok=True)
    sf = w / 'state.json'
    if sf.exists():
        st = json.load(open(sf)); sec = json.load(open(w / 'secrets.json'))
    else:
        with socket.socket() as s:
            s.bind(('', a.port)); port = s.getsockname()[1]
        st = dict(api_url=f'http://{a.host}:{port}', port=port, key_id='perf-' + secrets.token_hex(4))
        sec = dict(boot=secrets.token_urlsafe(24), admin=secrets.token_urlsafe(24), jwt=secrets.token_urlsafe(32),
                   sign=base64.b64encode(secrets.token_bytes(32)).decode(), aes=secrets.token_hex(32))
    env = dict(os.environ, DB_DRIVER='sqlite', DB_PATH=str(w / 'state.sqlite'), STORAGE_DRIVER='local',
        STORAGE_LOCAL_DIR=str(w / 'objects'), SERVER_HOST='0.0.0.0', SERVER_PORT=str(st['port']),
        SERVER_BASE_URL=st['api_url'], JWT_SECRET=sec['jwt'], PATCH_SIGNING_MASTER_KEY=sec['sign'],
        PATCH_DELIVERY_ENCRYPTION='off', SMTP_HOST='', DEFAULT_ADMIN_EMAIL='perf@example.invalid',
        DEFAULT_ADMIN_PASSWORD=sec['boot'])
    log = open(w / 'server.log', 'ab')
    p = subprocess.Popen([str(a.server)], env=env, stdout=log, stderr=log)
    def stop(*_):
        p.terminate(); sys.exit(0)
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    for _ in range(100):
        try: req(st['api_url'], 'GET', '/health'); break
        except Exception: time.sleep(.2)
    pw = sec['admin'] if sec.get('active') else sec['boot']
    login = req(st['api_url'], 'POST', '/auth/token', dict(email='perf@example.invalid', password=pw))
    tok = login['token']
    if login.get('must_change_password'):
        req(st['api_url'], 'PATCH', '/api/v1/users/me/password', dict(current_password=sec['boot'], new_password=sec['admin']), tok)
        sec['active'] = True
    sec['access_token'] = tok
    if 'app_id' not in st:
        org = req(st['api_url'], 'POST', '/api/v1/organizations', {'name': 'perf bench'}, tok)
        st['app_id'] = req(st['api_url'], 'POST', '/api/v1/apps', dict(organization_id=org['id'], display_name='patch perf bench'), tok)['id']
    json.dump(st, open(sf, 'w')); json.dump(sec, open(w / 'secrets.json', 'w'))
    os.chmod(w / 'secrets.json', 0o600)
    yaml = (f"app_id: {st['app_id']}\nbase_url: {st['api_url']}\nauto_update: true\n"
            "aot_patch_runtime_mode: dart-bytecode-interpreter\naot_patch_base_flavor_id: free\n"
            f"aot_patch_base_license_type: free\naot_patch_key_id: {st['key_id']}\n"
            "patch_verification: strict\npatch_response_verification: required\n" + a.extra_yaml)
    (a.app / 'shorebird.yaml').write_text(yaml)
    print('READY', st['api_url'], flush=True)
    p.wait()

main()
