"""
서버 파일을 Cloud Storage에 업로드 (SSL 우회)
"""
import warnings, urllib3, json, base64, os
warnings.filterwarnings('ignore')
urllib3.disable_warnings()

import requests
from google.oauth2 import service_account
import google.auth.transport.requests as gtr

# SSL 검증 비활성화 세션
session = requests.Session()
session.verify = False

creds = service_account.Credentials.from_service_account_file(
    'gcp-key.json',
    scopes=['https://www.googleapis.com/auth/cloud-platform']
)
req = gtr.Request(session=session)
creds.refresh(req)
token = creds.token
print(f"토큰 획득: {token[:20]}...")

PROJECT = "cafe-parking-frehet"
BUCKET  = "washnpay-deploy-temp"

# 버킷 생성
r = session.post(
    f"https://storage.googleapis.com/storage/v1/b?project={PROJECT}",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    json={"name": BUCKET, "location": "asia-northeast3", "storageClass": "STANDARD"},
)
if r.status_code in (200, 409):  # 409 = 이미 존재
    print(f"버킷 준비: gs://{BUCKET}")
else:
    print(f"버킷 생성 오류: {r.status_code} {r.text[:200]}")

# 파일 업로드
files = ["server.py", "point_auto.py", "requirements.txt", "Dockerfile"]
for fname in files:
    fpath = fname if fname == "Dockerfile" else fname
    # Dockerfile은 별도 생성
    if fname == "Dockerfile":
        content = b"""FROM mcr.microsoft.com/playwright/python:v1.60.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
RUN playwright install chromium --with-deps
COPY . .
ENV PORT=8080
CMD exec gunicorn --bind :$PORT --workers 1 --threads 4 --timeout 0 server:app
"""
    else:
        with open(fname, 'rb') as f:
            content = f.read()

    r = session.post(
        f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o?uploadType=media&name={fname}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"},
        data=content,
    )
    if r.status_code == 200:
        print(f"  OK {fname} uploaded ({len(content):,} bytes)")
    else:
        print(f"  FAIL {fname}: {r.status_code}")

print(f"\nCloud Shell에서 실행:\ngsutil cp gs://{BUCKET}/* ~/washnpay/")
