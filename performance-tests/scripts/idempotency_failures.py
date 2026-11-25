"""멱등키 처리를 실제 API(발자취 저장 041, 팁 등록 049)와 실제 Redis·PostgreSQL 로 확인한다.

  python idempotency_failures.py --label run1                 # 앱 1대
  python idempotency_failures.py --label run1 --ports 18080,18081

측정 전용 환경에서만 돈다. 뒤쪽 시나리오는 측정용 Redis 컨테이너를 멈췄다 다시 띄운다(측정용 Redis 는 디스크에 쓰지 않아 키가 사라진다).
"회원 행 잠금을 밖에서 잡아 두기"로 요청을 트랜잭션 안에서 원하는 만큼 멈춰 세운다(psql 이 users 행을 FOR UPDATE 로 잡고 기다린다).
"""
import argparse
import datetime
import json
import pathlib
import subprocess
import threading
import time
import uuid
from collections import Counter

import smoke
from concurrency_footprint import PSQL, fire, request, sql

ROOT = pathlib.Path(__file__).resolve().parents[1]
REDIS = ["docker", "exec", "yeogirok-perf-redis-1", "redis-cli"]
KST = datetime.timezone(datetime.timedelta(hours=9))


def now_text(offset_days=0):
    return (datetime.datetime.now(KST) + datetime.timedelta(days=offset_days)).strftime("%Y-%m-%d %H:%M:%S+09")


def location(lat, lng):
    return {"latitude": lat, "longitude": lng, "accuracyMeters": 5, "capturedAt": now_text()}


def footprint_body(lat, lng, reorder=False):
    payload = {"location": location(lat, lng), "categoryCodes": ["CAFE"], "body": "멱등 확인"}
    if reorder:
        payload = {"body": payload["body"], "categoryCodes": payload["categoryCodes"],
                   "location": dict(reversed(list(payload["location"].items())))}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def tip_body(lat, lng):
    return json.dumps({"category": "TIP", "body": "멱등 확인", "uploadIds": [], "location": location(lat, lng),
                       "spatialReference": {"provider": "ARCORE_CLOUD_ANCHOR", "referenceId": "anchor-" + uuid.uuid4().hex,
                                            "expiresAt": now_text(30)},
                       "placement": {"schemaVersion": 1}}, ensure_ascii=False).encode("utf-8")


def post(port, path, user_id, key, payload):
    status, code, data, elapsed = request(port, "POST", path, smoke.token(user_id), payload, {"Idempotency-Key": key})
    return {"status": status, "code": code, "id": (data.get("data") or {}).get("id") or (data.get("data") or {}).get("tipId"),
            "ms": round(elapsed)}


def tally(results):
    return dict(Counter(f"{r['status']}" + (f" {r['code']}" if r["code"] else "") for r in results))


def rows(table, column, user_id):
    return int(sql(f"select count(*) from {table} where {column} = {user_id} and deleted_at is null"))


def reset(user_id):
    sql(f"delete from footprints where user_id = {user_id}")
    sql(f"delete from ar_tips where author_id = {user_id}")


def redis(*args):
    return subprocess.run(REDIS + list(args), capture_output=True).stdout.decode().strip()


def redis_keys(user_id):
    return redis("--scan", "--pattern", f"request:{user_id}:*").split()


def hold_user_lock(user_id, seconds):
    """psql 이 회원 행을 seconds 초 잡고 있다가 놓는다. 그동안 그 회원의 발자취 저장은 트랜잭션 안에서 기다린다."""
    script = f"begin; select 1 from users where id = {user_id} for update; select pg_sleep({seconds}); commit;"
    process = subprocess.Popen(PSQL + ["-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    return process


def background(function):
    box = {}
    thread = threading.Thread(target=lambda: box.update(result=function()))
    thread.start()
    return thread, box


def wait_redis():
    for _ in range(60):
        if redis("ping") == "PONG":
            return
        time.sleep(1)
    raise RuntimeError("측정용 Redis 가 다시 뜨지 않았습니다")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--ports", default="18080")
    args = parser.parse_args()
    ports = [int(port) for port in args.ports.split(",")]
    port, user = ports[0], 4201
    lat, lng = 36.5, 128.5
    report = {}

    def record(name, value):
        report[name] = value
        print(name, json.dumps(value, ensure_ascii=False), flush=True)

    # 1. 같은 키를 100번 차례로
    reset(user)
    key, payload = str(uuid.uuid4()), footprint_body(lat, lng)
    results = [post(port, "/api/v1/footprints", user, key, payload) for _ in range(100)]
    record("1 같은 키 100번 차례로", {"응답": tally(results), "서로 다른 id 수": len({r["id"] for r in results}),
                                "저장된 행": rows("footprints", "user_id", user)})

    # 2. 같은 키를 100건 한꺼번에 (앱이 여러 대면 나눠 보낸다)
    reset(user)
    key, payload = str(uuid.uuid4()), footprint_body(lat, lng)
    results = fire([(lambda p=ports[i % len(ports)]: post(p, "/api/v1/footprints", user, key, payload)) for i in range(100)])
    record(f"2 같은 키 100건 동시(앱 {len(ports)}대)", {"응답": tally(results), "저장된 행": rows("footprints", "user_id", user)})
    after = post(port, "/api/v1/footprints", user, key, payload)
    record("2-1 끝난 뒤 같은 키 다시", after)

    # 3. 같은 키·다른 본문 / 4. 필드 순서만 바꿈 / 5. 자료형을 바꿈
    reset(user)
    key = str(uuid.uuid4())
    first = post(port, "/api/v1/footprints", user, key, footprint_body(lat, lng))
    record("3 같은 키·다른 위치", post(port, "/api/v1/footprints", user, key, footprint_body(lat + 0.01, lng)))
    reset(user)
    key, fixed = str(uuid.uuid4()), location(lat, lng)

    def fixed_body(reorder=False, as_text=False):
        place = dict(reversed(list(fixed.items()))) if reorder else dict(fixed)
        if as_text:
            place["latitude"] = str(place["latitude"])
        payload = {"location": place, "categoryCodes": ["CAFE"], "body": "멱등 확인"}
        return json.dumps(dict(reversed(list(payload.items()))) if reorder else payload, ensure_ascii=False).encode()

    first = post(port, "/api/v1/footprints", user, key, fixed_body())
    record("4 필드 순서만 바꿔 다시", {"처음": first, "다시": post(port, "/api/v1/footprints", user, key, fixed_body(reorder=True))})
    record("5 위도를 문자열로 바꿔 다시", post(port, "/api/v1/footprints", user, key, fixed_body(as_text=True)))

    # 6. 서로 다른 사용자가 같은 키
    reset(user), reset(user + 1)
    key = str(uuid.uuid4())
    record("6 다른 사용자·같은 키", [post(port, "/api/v1/footprints", user, key, footprint_body(lat, lng)),
                              post(port, "/api/v1/footprints", user + 1, key, footprint_body(lat, lng))])

    # 7. 업무 오류(F003)로 실패한 요청은 표시를 남기지 않는다
    key = str(uuid.uuid4())
    failed = post(port, "/api/v1/footprints", user, key, footprint_body(lat, lng))
    record("7 실패한 요청 뒤 Redis 키", {"응답": failed, "이 키가 Redis 에 있나": any(key in k for k in redis_keys(user))})

    # 8. 완료 기록이 사라진 뒤 재전송 (Redis 재시작으로 키 유실·보관 기간 만료와 같은 상태)
    reset(user)
    redis("flushall")
    key, payload = str(uuid.uuid4()), footprint_body(lat, lng)
    first = post(port, "/api/v1/footprints", user, key, payload)
    redis("del", *redis_keys(user))
    again = post(port, "/api/v1/footprints", user, key, payload)
    record("8 발자취: 완료 기록 유실 뒤 같은 키", {"처음": first, "다시": again, "저장된 행": rows("footprints", "user_id", user)})

    key, payload = str(uuid.uuid4()), tip_body(lat, lng)
    first = post(port, "/api/v1/tips", user, key, payload)
    redis("del", *[k for k in redis_keys(user) if key in k])
    again = post(port, "/api/v1/tips", user, key, payload)
    record("8-1 팁 등록: 완료 기록 유실 뒤 같은 키", {"처음": first, "다시": again, "저장된 팁 행": rows("ar_tips", "author_id", user)})

    # 9. 처리가 처리 중 표시 수명(30초)보다 오래 걸릴 때
    reset(user)
    redis("flushall")
    key, payload = str(uuid.uuid4()), footprint_body(lat, lng)
    holder = hold_user_lock(user, 36)
    thread1, box1 = background(lambda: post(port, "/api/v1/footprints", user, key, payload))
    time.sleep(10)
    early = post(port, "/api/v1/footprints", user, key, payload)
    time.sleep(22)  # 처음 요청 뒤 32초: 표시가 사라진 뒤
    thread2, box2 = background(lambda: post(port, "/api/v1/footprints", user, key, payload))
    holder.wait(), thread1.join(), thread2.join()
    time.sleep(1)
    record("9 처리 36초(표시 수명 30초)", {
        "10초 뒤 같은 키": early, "처음 요청": box1["result"], "32초 뒤 같은 키": box2["result"],
        "저장된 행": rows("footprints", "user_id", user), "끝난 뒤 Redis 키 수": len(redis_keys(user)),
        "그 뒤 같은 키 다시": post(port, "/api/v1/footprints", user, key, payload)})

    # 10. DB 커밋 뒤 Redis 완료 기록 실패: 요청이 트랜잭션 안에서 기다리는 동안 Redis 를 멈춘다
    reset(user)
    redis("flushall")
    key, payload = str(uuid.uuid4()), footprint_body(lat, lng)
    holder = hold_user_lock(user, 6)
    thread1, box1 = background(lambda: post(port, "/api/v1/footprints", user, key, payload))
    time.sleep(2)
    subprocess.run(["docker", "stop", "yeogirok-perf-redis-1"], capture_output=True)
    holder.wait()
    thread1.join()
    saved = rows("footprints", "user_id", user)
    # 11. Redis 가 멈춘 동안의 새 요청(쓰기·읽기)
    down_write = post(port, "/api/v1/footprints", user + 1, str(uuid.uuid4()), footprint_body(lat + 1, lng))
    status, code, _, elapsed = request(port, "GET", "/api/v1/users/me/footprints", smoke.token(user))
    down_read = {"status": status, "code": code, "ms": round(elapsed)}
    subprocess.run(["docker", "start", "yeogirok-perf-redis-1"], capture_output=True)
    wait_redis()
    restarted = time.time()
    again = post(port, "/api/v1/footprints", user, key, payload)
    record("10 커밋 뒤 Redis 중단", {"처음 요청": box1["result"], "저장된 행": saved, "Redis 다시 뜬 뒤 같은 키": again,
                                 "끝난 뒤 저장된 행": rows("footprints", "user_id", user)})
    record("11 Redis 중단 중 새 요청", {"저장(041)": down_write, "조회(042)": down_read})
    fresh = post(port, "/api/v1/footprints", user + 2, str(uuid.uuid4()), footprint_body(lat + 2, lng))
    record("12 Redis 다시 뜬 뒤 새 요청", {"응답": fresh, "다시 뜬 뒤 걸린 초": round(time.time() - restarted, 1)})

    for user_id in (user, user + 1, user + 2):
        reset(user_id)
    (ROOT / "results" / f"idempotency-{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                                     encoding="utf-8")


if __name__ == "__main__":
    main()
