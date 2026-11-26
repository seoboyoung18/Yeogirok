"""인증·인가·입력 검증 확인(TEST 09). 측정용 앱(18080)에 요청을 한 건씩 보내 응답 코드와 DB 변화를 본다.

  python security_check.py --label run1

측정 전용 DB·Redis 에서만 돈다. 공격 도구가 아니라 "막아야 할 요청이 막히는지"를 한 번씩 확인하는 스크립트다.
토큰은 측정용 JWT_SECRET 으로 만들고, 비밀값·토큰·응답 본문 원문은 결과에 남기지 않는다(코드·메시지 길이·판정만).
"""
import argparse
import base64
import hashlib
import hmac
import http.client
import json
import pathlib
import re
import subprocess
import time
import urllib.parse
import uuid

import smoke
from concurrency_footprint import sql

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = int(smoke.ENV["SERVER_PORT"])
SECRET = base64.b64decode(smoke.ENV["JWT_SECRET"])
REDIS = ["docker", "exec", "yeogirok-perf-redis-1", "redis-cli"]
OWNER, OTHER, WITHDRAWN, GHOST = 10, 20, 4999, 99999999
# 응답에 나오면 안 되는 내부 정보의 흔적
LEAK = re.compile(r"exception|org\.(spring|hibernate|postgres|apache)|com\.ssafy|\bat [a-z]+\.[a-z]+\.|select |insert |"
                  r"update .* set|sqlstate|jdbc|stack ?trace|hikari|tomcat/", re.I)


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_token(sub=OWNER, secret=SECRET, alg="HS256", **overrides):
    now = int(time.time())
    claims = {"iss": "yeogirok", "sub": str(sub), "iat": now, "exp": now + 1800, "jti": str(uuid.uuid4()), "role": "USER"}
    claims.update(overrides)
    claims = {key: value for key, value in claims.items() if value is not None}
    signing = b64url(json.dumps({"alg": alg, "typ": "JWT"}).encode()) + "." + b64url(json.dumps(claims).encode())
    signature = "" if alg == "none" else b64url(hmac.new(secret, signing.encode(), hashlib.sha256).digest())
    return signing + "." + signature


def send(method, path, authorization=None, body=None, headers=None, content_type="application/json"):
    """(상태 코드, 오류 코드, 메시지, 본문 원문, ms). body 는 dict·bytes·None."""
    payload = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    all_headers = {"Content-Type": content_type, **(headers or {})}
    if authorization is not None:
        all_headers["Authorization"] = authorization
    started = time.perf_counter()
    try:
        connection = http.client.HTTPConnection("localhost", PORT, timeout=120)
        connection.request(method, path, body=payload, headers=all_headers)
        response = connection.getresponse()
        text = response.read().decode(errors="replace")
        connection.close()
    except (OSError, http.client.HTTPException) as error:
        return 0, type(error).__name__, "", "", round((time.perf_counter() - started) * 1000)
    try:
        data = json.loads(text)
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    return response.status, data.get("code"), data.get("message") or "", text, round((time.perf_counter() - started) * 1000)


def bearer(sub=OWNER, **options):
    return "Bearer " + make_token(sub, **options)


class Report:
    def __init__(self):
        self.rows, self.leaks = [], []

    def check(self, group, name, result, expected, extra=None):
        """expected: 허용하는 '상태 코드' 또는 '상태 코드 오류코드' 문자열 목록. 5xx 는 어떤 경우에도 실패로 본다."""
        status, code, message, text, elapsed = result
        got = f"{status} {code}" if code else str(status)
        passed = status < 500 and status != 0 and any(got == want or str(status) == want for want in expected)
        if LEAK.search(text):
            self.leaks.append(f"{group} / {name}")
        row = {"구분": group, "시나리오": name, "기대": " 또는 ".join(expected), "결과": got, "판정": "통과" if passed else "확인 필요",
               "메시지 글자 수": len(message), "ms": elapsed, **(extra or {})}
        self.rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        return passed


def location(lat=37.5665, lng=126.978):
    return smoke.location(lat, lng)


def row_hash(table, row_id):
    return sql(f"select md5(t::text) from {table} t where id = {row_id}")


def authentication(report):
    group, path = "인증", "/api/v1/users/me/footprints"
    valid = make_token()
    head, payload, signature = valid.split(".")
    forged = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    forged["sub"] = str(OTHER)
    tampered = head + "." + b64url(json.dumps(forged).encode()) + "." + signature
    subprocess.run(REDIS + ["set", f"auth:withdrawn:{WITHDRAWN}", "1", "EX", "120"], capture_output=True)
    cases = [
        ("정상 토큰", "Bearer " + valid, ["200"]),
        ("토큰 없음", None, ["401 A001"]),
        ("Bearer 뒤가 빈 값", "Bearer ", ["401 A001", "401 A003"]),
        ("토큰이 아닌 글자", "Bearer not-a-token", ["401 A003"]),
        ("Basic 방식 헤더", "Basic " + base64.b64encode(b"admin:admin").decode(), ["401 A001"]),
        ("만료된 토큰", bearer(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600), ["401 A002"]),
        ("다른 비밀값으로 서명", bearer(secret=b"x" * 32), ["401 A003"]),
        ("서명 없는 토큰(alg none)", bearer(alg="none"), ["401 A003"]),
        ("본문의 회원 번호만 바꿈(서명 그대로)", "Bearer " + tampered, ["401 A003"]),
        ("서명 끝 글자 바꿈", "Bearer " + valid[:-2] + ("AA" if not valid.endswith("AA") else "BB"), ["401 A003"]),
        ("발급자(iss)가 다름", bearer(iss="someone-else"), ["401 A003"]),
        ("역할(role) 없음", bearer(role=None), ["401 A003"]),
        ("없는 역할(SUPERUSER)", bearer(role="SUPERUSER"), ["401 A003"]),
        ("회원 번호가 숫자가 아님", bearer(sub="abc"), ["401 A003"]),
        ("회원 번호가 0", bearer(sub="0"), ["401 A003"]),
        ("만료 시각(exp) 없음", bearer(exp=None), ["401 A003", "401 A002"]),
        ("탈퇴 표시가 있는 회원의 토큰", bearer(WITHDRAWN), ["401 A003"]),
    ]
    for name, authorization, expected in cases:
        report.check(group, name, send("GET", path, authorization), expected)

    # 토큰은 맞지만 DB 에 없는 회원
    report.check(group, "DB 에 없는 회원 번호: 목록 조회", send("GET", path, bearer(GHOST)), ["200", "401 A003"])
    before = sql("select count(*) from footprints")
    report.check(group, "DB 에 없는 회원 번호: 발자취 저장",
                 send("POST", "/api/v1/footprints", bearer(GHOST),
                      {"location": location(34.2, 127.3), "categoryCodes": ["CAFE"]}, {"Idempotency-Key": str(uuid.uuid4())}),
                 ["401 A003"], {"발자취 행 수 변화": int(sql("select count(*) from footprints")) - int(before)})

    for name, method, target in [("문서 화면(swagger-ui)", "GET", "/swagger-ui/index.html"), ("API 문서(v3/api-docs)", "GET", "/v3/api-docs"),
                                 ("상태 확인(actuator/health)", "GET", "/actuator/health"),
                                 ("지표(actuator/prometheus)", "GET", "/actuator/prometheus"),
                                 ("환경 값(actuator/env)", "GET", "/actuator/env"),
                                 ("토큰 재발급(본문 없음)", "POST", "/api/v1/auth/token/refresh")]:
        result = send(method, target)
        report.rows.append({"구분": "토큰 없이 열리는 경로", "시나리오": name, "결과": f"{result[0]} {result[1] or ''}".strip(),
                            "본문 글자 수": len(result[3])})
        print(json.dumps(report.rows[-1], ensure_ascii=False), flush=True)
    result = send("GET", "/actuator/env", bearer())
    report.rows.append({"구분": "토큰 없이 열리는 경로", "시나리오": "환경 값(actuator/env), 로그인한 회원", "결과": str(result[0])})


def authorization(report):
    group = "인가(다른 회원의 자원)"
    footprint = sql(f"select id from footprints where user_id = {OWNER} and deleted_at is null order by id limit 1")
    tip = sql(f"select id from ar_tips where author_id = {OWNER} and status = 'PUBLISHED' and deleted_at is null order by id limit 1")
    before = (row_hash("footprints", footprint), row_hash("ar_tips", tip))
    other, key = bearer(OTHER), lambda: {"Idempotency-Key": str(uuid.uuid4())}

    report.check(group, "주인이 자기 발자취 상세 조회(대조용)", send("GET", f"/api/v1/footprints/{footprint}", bearer(OWNER)), ["200"])
    report.check(group, "다른 회원이 발자취 상세 조회", send("GET", f"/api/v1/footprints/{footprint}", other), ["403 F002", "404 F001"])
    report.check(group, "다른 회원이 발자취 본문 수정",
                 send("PATCH", f"/api/v1/footprints/{footprint}", other, {"version": 1, "body": "남이 쓴 글"}), ["403 F002", "404 F001"])
    report.check(group, "다른 회원이 발자취 삭제", send("DELETE", f"/api/v1/footprints/{footprint}?version=1", other),
                 ["403 F002", "404 F001"])
    result = send("GET", "/api/v1/users/me/footprints?limit=50", other)
    ids = re.findall(r'"id":\s*(\d+)', result[3])
    owners = sql(f"select count(*) from footprints where id in ({','.join(ids) or 0}) and user_id <> {OTHER}")
    report.check(group, "내 발자취 목록에 남의 것이 섞이는지", result, ["200"], {"남의 발자취 수": int(owners)})

    report.check(group, "주인이 자기 팁 상세 조회(대조용)", send("GET", f"/api/v1/users/me/tips/{tip}", bearer(OWNER)), ["200"])
    report.check(group, "다른 회원이 내 팁 상세 경로로 조회", send("GET", f"/api/v1/users/me/tips/{tip}", other), ["403 T002", "404 T012"])
    report.check(group, "다른 회원이 팁 등록 상태 조회", send("GET", f"/api/v1/tips/{tip}/status", other), ["403 T002", "404 T012"])
    report.check(group, "다른 회원이 팁 수정",
                 send("PATCH", f"/api/v1/tips/{tip}", other, {"version": 1, "body": "남이 쓴 글"}, key()), ["403 T002", "404 T012"])
    report.check(group, "다른 회원이 팁 삭제", send("DELETE", f"/api/v1/tips/{tip}?version=1", other), ["403 T002", "404 T012"])
    report.check(group, "다른 회원이 팁 재시도", send("POST", f"/api/v1/tips/{tip}/retry", other, {}, key()),
                 ["403 T002", "404 T012"])
    report.check(group, "다른 회원이 팁 사진 주소 요청", send("GET", f"/api/v1/tips/{tip}/images/1/access", other),
                 ["403 M005", "403 T002", "404 M004", "404 T012"])
    far = send("POST", f"/api/v1/tips/{tip}/view", other, {"location": location(33.5, 126.5)})
    body_text = sql(f"select body from ar_tips where id = {tip}")
    report.check(group, "멀리 있는 회원이 팁 본문 열람", far, ["422 T004"], {"응답에 팁 본문이 들어 있음": body_text in far[3]})
    report.check(group, "없는 기록의 사진 주소 요청", send("GET", "/api/v1/records/999999/images/1/access", other),
                 ["404 RE001", "404 M004", "403 RE002", "404"])
    report.check(group, "없는 업로드의 완료 요청", send("POST", f"/api/v1/uploads/{uuid.uuid4()}/complete", other, {}, key()),
                 ["404 M004"])

    after = (row_hash("footprints", footprint), row_hash("ar_tips", tip))
    report.rows.append({"구분": group, "시나리오": "시도 뒤 대상 발자취·팁 행이 그대로인지", "결과": "그대로" if before == after else "바뀜",
                        "판정": "통과" if before == after else "확인 필요"})
    print(json.dumps(report.rows[-1], ensure_ascii=False), flush=True)


def inputs(report):
    token = bearer()
    group = "페이징·조회 조건"
    quote = urllib.parse.quote
    listing = "/api/v1/users/me/footprints"
    for name, query, expected in [
        ("limit=0", "limit=0", ["400 C007"]), ("limit=-1", "limit=-1", ["400 C007"]), ("limit=51", "limit=51", ["400 C007"]),
        ("limit=1000000", "limit=1000000", ["400 C007"]), ("limit=글자", "limit=abc", ["400 C002"]),
        ("limit=20자리 숫자", "limit=99999999999999999999", ["400 C002"]),
        ("cursor=아무 글자", "cursor=abc", ["400 C006"]),
        ("cursor=Base64 인 쓰레기 값", "cursor=" + base64.urlsafe_b64encode(b"9999-99-99|x").decode(), ["400 C006"]),
        ("cursor=10,000자", "cursor=" + "A" * 10000, ["400 C006", "400"]),
        ("category=없는 값", "category=HACK", ["400 C003"]),
        ("from > to", "from=2026-12-31&to=2026-01-01", ["400 C012"]),
        ("from=날짜 아님", "from=yesterday", ["400 C002"]),
    ]:
        report.check(group, name, send("GET", f"{listing}?{query}", token), expected)
    stats = "/api/v1/visit-statistics"
    for name, query, expected in [
        ("bbox 값 3개", "bbox=1,2,3&zoom=13", ["400 C013"]), ("bbox 글자", "bbox=a,b,c,d&zoom=13", ["400 C013"]),
        ("bbox NaN", "bbox=NaN,NaN,NaN,NaN&zoom=13", ["400 C013"]),
        ("bbox 지구 전체", "bbox=-180,-90,180,90&zoom=13", ["400 C013", "200"]),
        ("bbox 뒤집힘", "bbox=127.1,37.6,126.9,37.4&zoom=13", ["400 C013"]),
        ("zoom=-1", "bbox=126.9,37.4,127.1,37.6&zoom=-1", ["400 C003", "400 C013"]),
        ("zoom=999", "bbox=126.9,37.4,127.1,37.6&zoom=999", ["400 C003", "400 C013", "200"]),
        ("zoom 없음", "bbox=126.9,37.4,127.1,37.6", ["400 C001"]),
    ]:
        report.check(group, name, send("GET", f"{stats}?{query}", token), expected)
    for name, method, target, expected in [
        ("경로 번호가 글자", "GET", "/api/v1/footprints/abc", ["400 C002"]),
        ("경로 번호가 음수", "GET", "/api/v1/footprints/-1", ["404 F001", "400 C002", "400 C003"]),
        ("경로 번호가 20자리", "GET", "/api/v1/footprints/99999999999999999999", ["400 C002"]),
        ("없는 경로", "GET", "/api/v1/does-not-exist", ["404 C015"]),
        ("허용하지 않는 메서드", "PUT", "/api/v1/users/me/footprints", ["405 C016"]),
        ("경로 거슬러 올라가기", "GET", "/api/v1/footprints/..%2F..%2Factuator%2Fenv", ["400", "404 C015", "400 C002"]),
    ]:
        report.check(group, name, send(method, target, token), expected)

    group = "SQL 로 해석될 수 있는 문자열"
    tables = sql("select count(*) from information_schema.tables where table_schema = 'public'")
    footprints = sql("select count(*) from footprints")
    for name, query, expected in [
        ("category 에 ' OR '1'='1", "category=" + quote("CAFE' OR '1'='1"), ["400 C003"]),
        ("cursor 에 '; DROP TABLE", "cursor=" + quote("'; DROP TABLE footprints;--"), ["400 C006"]),
        ("limit 에 1; DELETE", "limit=" + quote("1; DELETE FROM footprints"), ["400 C002"]),
        ("from 에 UNION SELECT", "from=" + quote("2026-01-01' UNION SELECT 1--"), ["400 C002"]),
    ]:
        report.check(group, name, send("GET", f"{listing}?{query}", token), expected)
    report.check(group, "bbox 에 SQL 조각", send("GET", f"{stats}?bbox={quote('126,37,127,38); DROP TABLE users;--')}&zoom=13", token),
                 ["400 C013"])
    report.check(group, "경로 번호에 OR 1=1", send("GET", "/api/v1/footprints/" + quote("1 OR 1=1"), token), ["400 C002"])
    text = "'; DROP TABLE footprints;-- <script>alert(1)</script> ${7*7}"
    writer = bearer(4000)
    saved = send("POST", "/api/v1/footprints", writer,
                 {"location": location(34.31 + time.time() % 500 / 10000, 127.41), "categoryCodes": ["CAFE"], "body": text},
                 {"Idempotency-Key": str(uuid.uuid4())})
    created = re.search(r'"id":\s*(\d+)', saved[3])
    stored = sql(f"select body from footprints where id = {created[1]}") if created else None
    read_back = send("GET", f"/api/v1/footprints/{created[1]}", writer) if created else (0, None, "", "", 0)
    returned = json.loads(read_back[3]).get("data", {}).get("body") if read_back[0] == 200 else None
    report.check(group, "본문에 SQL·스크립트 문자열을 넣어 저장", saved, ["201"],
                 {"DB 에 글자 그대로 저장": stored == text, "조회 응답도 글자 그대로": returned == text})
    if created:
        sql(f"delete from footprints where id = {created[1]}")
    unchanged = (tables == sql("select count(*) from information_schema.tables where table_schema = 'public'")
                 and footprints == sql("select count(*) from footprints"))
    report.rows.append({"구분": group, "시나리오": "시도 뒤 테이블 수·발자취 행 수가 그대로인지", "결과": "그대로" if unchanged else "바뀜",
                        "판정": "통과" if unchanged else "확인 필요"})
    print(json.dumps(report.rows[-1], ensure_ascii=False), flush=True)

    group = "본문 형식·크기"
    post = lambda body, **options: send("POST", "/api/v1/footprints", bearer(4001), body,
                                        {"Idempotency-Key": str(uuid.uuid4())}, **options)
    report.check(group, "JSON 이 중간에 끊김", post(b'{"location": {"latitude": 37.5,'), ["400 C002"])
    report.check(group, "본문 없음", post(None), ["400 C002", "400 C001"])
    report.check(group, "JSON 대신 배열", post([1, 2, 3]), ["400 C002"])
    report.check(group, "Content-Type 이 text/plain", post(b"hello", content_type="text/plain"), ["415 C017"])
    report.check(group, "위도에 글자", post({"location": {**location(), "latitude": "north"}, "categoryCodes": ["CAFE"]}), ["400 C002"])
    report.check(group, "위도 1e400", post(b'{"location":{"latitude":1e400,"longitude":127,"accuracyMeters":5,"capturedAt":"'
                                          + location()["capturedAt"].encode() + b'"},"categoryCodes":["CAFE"]}'), ["400 C011", "400 C002"])
    report.check(group, "카테고리 1,000개", post({"location": location(), "categoryCodes": ["CAFE"] * 1000}), ["400 C003"])
    nested = b'{"location":' * 5000 + b"1" + b"}" * 5000
    report.check(group, "5,000겹 중첩 JSON", post(nested), ["400 C002"])
    report.check(group, "멱등키 없음", send("POST", "/api/v1/footprints", bearer(4001),
                                         {"location": location(), "categoryCodes": ["CAFE"]}), ["400 C001"])
    report.check(group, "멱등키 10,000자", send("POST", "/api/v1/footprints", bearer(4001),
                                            {"location": location(), "categoryCodes": ["CAFE"]}, {"Idempotency-Key": "k" * 10000}),
                 ["400", "400 C003", "431"])
    for megabytes in (1, 10, 30):
        big = {"location": location(34.4, 127.6), "categoryCodes": ["CAFE"], "body": "가" * (megabytes * 349525)}
        report.check(group, f"본문 {megabytes}MB(방명록 200자 한도)", post(big), ["400 C003", "400 C002", "413"],
                     {"요청 크기 MB": round(len(json.dumps(big).encode()) / 1048576, 1)})
    report.check(group, "Authorization 헤더 20KB", send("GET", listing, "Bearer " + "a" * 20000), ["400", "431", "401 A003"])
    left = sql("select count(*) from footprints where user_id = 4001 and created_at > now() - interval '10 minutes'")
    report.rows.append({"구분": group, "시나리오": "거부된 요청이 남긴 발자취 행", "결과": left, "판정": "통과" if left == "0" else "확인 필요"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    report = Report()
    authentication(report)
    authorization(report)
    inputs(report)
    health = send("GET", "/api/v1/users/me/footprints", bearer())
    judged = [row for row in report.rows if "판정" in row]
    summary = {"확인한 항목": len(judged), "통과": sum(row["판정"] == "통과" for row in judged),
               "확인 필요": [f'{row["구분"]} / {row["시나리오"]}: {row["결과"]}' for row in judged if row["판정"] != "통과"],
               "5xx 응답": sum(str(row.get("결과", "")).startswith("5") for row in report.rows),
               "내부 정보 흔적이 보인 응답": report.leaks, "끝난 뒤 정상 요청": health[0]}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    (ROOT / "results" / f"security-{args.label}.json").write_text(
        json.dumps({"summary": summary, "rows": report.rows}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
