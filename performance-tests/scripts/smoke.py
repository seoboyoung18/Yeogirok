"""측정 대상 API 를 한 번씩 불러 응답 코드를 확인한다(측정 전 확인용). python smoke.py
performance-tests/.env.perf 의 측정용 JWT_SECRET 으로 토큰을 만든다. 비밀값과 토큰은 출력하지 않는다."""
import base64, datetime, hashlib, hmac, json, pathlib, time, urllib.error, urllib.request, uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV = dict(line.split("=", 1) for line in (ROOT / ".env.perf").read_text().splitlines() if "=" in line)
BASE = "http://localhost:" + ENV["SERVER_PORT"]


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def token(user_id):
    now = int(time.time())
    head = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iss": "yeogirok", "sub": str(user_id), "iat": now, "exp": now + 1800, "jti": str(uuid.uuid4()), "role": "USER"}
    signing = head + "." + b64url(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(base64.b64decode(ENV["JWT_SECRET"]), signing.encode(), hashlib.sha256).digest()
    return signing + "." + b64url(sig)


def call(method, path, body=None, headers=None):
    request = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode())
    request.add_header("Authorization", "Bearer " + token(1))
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def location(lat, lng):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    return {"latitude": lat, "longitude": lng, "accuracyMeters": 5, "capturedAt": now.strftime("%Y-%m-%d %H:%M:%S+09")}


if __name__ == "__main__":
    checks = [
        ("046 방문 분포(도시)", "GET", "/api/v1/visit-statistics?bbox=126.85,37.45,127.15,37.65&zoom=13", None, None),
        ("046 방문 분포(카테고리)", "GET", "/api/v1/visit-statistics?bbox=126.85,37.45,127.15,37.65&zoom=13&category=CAFE", None, None),
        ("046 방문 분포(전국)", "GET", "/api/v1/visit-statistics?bbox=124,33,132,39&zoom=7", None, None),
        ("047 주변 팁", "POST", "/api/v1/tips/search", {"location": location(37.5665, 126.9780)}, None),
        ("042 내 발자취", "GET", "/api/v1/users/me/footprints", None, None),
        ("053 내 팁", "GET", "/api/v1/users/me/tips", None, None),
        ("041 발자취 저장", "POST", "/api/v1/footprints",
         {"location": location(34.0 + time.time() % 1000 / 1000, 127.5), "categoryCodes": ["CAFE"], "body": "측정"},
         {"Idempotency-Key": str(uuid.uuid4())}),
        ("지표", "GET", "/actuator/prometheus", None, None),
    ]
    for name, method, path, body, headers in checks:
        status, text = call(method, path, body, headers)
        print(f"{status} {name} ({len(text)}자)" + ("" if status < 400 else " " + text[:200]))
