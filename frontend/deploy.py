#!/usr/bin/env python3
import json, time, base64, os, gzip, hashlib, requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

with open(os.environ.get('FIREBASE_KEY_PATH', '/opt/ojoia/config/firebase-key.json')) as f:
    sa = json.load(f)
key = serialization.load_pem_private_key(sa['private_key'].encode(), password=None)

def b64(d): 
    if isinstance(d, str): d = d.encode()
    return base64.urlsafe_b64encode(d).rstrip(b'=').decode()

def get_token():
    hd = b64(json.dumps({'alg':'RS256','typ':'JWT'}))
    cl = b64(json.dumps({'iss':sa['client_email'],'scope':'https://www.googleapis.com/auth/cloud-platform','aud':'https://oauth2.googleapis.com/token','exp':int(time.time())+3600,'iat':int(time.time())}))
    sig = b64(key.sign(f'{hd}.{cl}'.encode(), padding.PKCS1v15(), hashes.SHA256()))
    r = requests.post('https://oauth2.googleapis.com/token', data={'grant_type':'urn:ietf:params:oauth:grant-type:jwt-bearer','assertion':f'{hd}.{cl}.{sig}'})
    d = r.json()
    if 'access_token' not in d:
        print(f"OAuth2 error: {d}")
        print(f"Response status: {r.status_code}")
        exit(1)
    return d['access_token']

tok = get_token()
h = {'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'}
base = os.path.join(os.path.dirname(os.path.abspath(__file__)), '')

config = {'headers':[{'headers':{'Cache-Control':'no-cache, no-store, must-revalidate'},'glob':'**/*.@(js|css|html)'},{'headers':{'Cache-Control':'max-age=86400'},'glob':'**/*.@(png|jpg|jpeg|gif|svg|ico)'}],'rewrites':[{'glob':'/api/**','path':'https://api.ojoia.com.do/'},{'glob':'/admin','path':'/admin2/index.html'},{'glob':'/admin/**','path':'/admin2/index.html'},{'glob':'/admin/','path':'/admin2/index.html'},{'glob':'/admin2/**','path':'/admin2/index.html'},{'glob':'/**','path':'/index.html'}]}

print('[deploy] Creating version...', end=' ', flush=True)
r = requests.post('https://firebasehosting.googleapis.com/v1beta1/sites/ojoia-67216/versions', headers=h, json={'config': config})
r.raise_for_status()
vid = r.json()['name'].split('/')[-1]
print(vid)

hashes = {}
gzs = {}
files = []
IGNORE_DIRS = {'.git', '.kilo', 'node_modules', '__pycache__', 'legacy'}
for root, dirs, fnames in os.walk(base):
    dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.')]
    for fn in fnames:
        if fn.startswith('.') or fn in ('deploy.py', 'server.py', 'firebase-debug.log'): continue
        fp = os.path.join(root, fn); rp = os.path.relpath(fp, base)
        with open(fp, 'rb') as f: content = f.read()
        gz = gzip.compress(content, 9)
        hashes['/'+rp] = hashlib.sha256(gz).hexdigest(); gzs[rp] = gz; files.append(rp)

print(f'[deploy] {len(files)} files')

pop = requests.post(f'https://firebasehosting.googleapis.com/v1beta1/sites/ojoia-67216/versions/{vid}:populateFiles', headers=h, json={'files': hashes})
pop.raise_for_status()
pd = pop.json()
url = pd['uploadUrl']
required = pd.get('uploadRequiredHashes', list(hashes.values()))
print(f'[deploy] Uploading {len(required)} files...')

uh = {'Authorization': f'Bearer {tok}', 'Content-Type': 'application/octet-stream'}
for rp in files:
    hsh = hashes['/'+rp]
    if hsh in required:
        print(f'  {rp}', flush=True)
        requests.post(f'{url}/{hsh}', headers=uh, data=gzs[rp]).raise_for_status()

r = requests.patch(f'https://firebasehosting.googleapis.com/v1beta1/sites/ojoia-67216/versions/{vid}?update_mask=status', headers=h, json={'status': 'FINALIZED'})
r.raise_for_status()
print('[deploy] Finalized')

rel = requests.post(f'https://firebasehosting.googleapis.com/v1beta1/sites/ojoia-67216/releases?versionName=sites/ojoia-67216/versions/{vid}', headers=h, json={})
rel.raise_for_status()
print(f'[deploy] Released!')