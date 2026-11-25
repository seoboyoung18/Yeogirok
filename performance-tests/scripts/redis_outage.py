"""측정용 Redis 를 멈춘 동안 요청이 얼마 뒤 어떤 응답으로 끝나는지 잰다. 끝나면 Redis 를 다시 띄우고 복구 시간을 잰다.

  python redis_outage.py --label before

측정 전용 환경에서만 돈다. 순서: (1) Redis 정상일 때 기준, (2) Redis 중단 → 조회 1건·저장 1건, (3) 조회 50건 동시,
(4) Redis 다시 띄움 → 새 요청이 성공할 때까지 걸린 시간. 클라이언트 제한 시간은 200초로 넉넉히 둔다.
"""
import argparse
import http.client
import json
import pathlib
import statistics
import subprocess
import time
import uuid
from collections import Counter

import smoke
from concurrency_footprint import fire, sql
from idempotency_failures import footprint_body, wait_redis

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 18080


def call(method, path, user, payload=None, key=None):
    started = time.perf_counter()
    try:
        connection = http.client.HTTPConnection("localhost", PORT, timeout=200)
        headers = {"Authorization": "Bearer " + smoke.token(user), "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        code = json.loads(response.read().decode() or "{}").get("code")
        return response.status, code, round(time.perf_counter() - started, 2)
    except (OSError, ValueError, http.client.HTTPException) as error:
        return 0, type(error).__name__, round(time.perf_counter() - started, 2)


def read(user):
    return call("GET", "/api/v1/users/me/footprints", user)


def write(user, lat):
    return call("POST", "/api/v1/footprints", user, footprint_body(lat, 129.5), str(uuid.uuid4()))


def shape(result):
    status, code, seconds = result
    return {"status": status, "code": code, "seconds": seconds}


def burst(count):
    results = fire([(lambda user=4300 + index: read(user)) for index in range(count)])
    seconds = sorted(result[2] for result in results)
    return {"응답": dict(Counter(f"{status}" + (f" {code}" if code else "") for status, code, _ in results)),
            "중앙값 초": round(statistics.median(seconds), 2), "최대 초": seconds[-1]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    users = ",".join(str(user) for user in (4291, 4292, 4293))
    sql(f"delete from footprints where user_id in ({users})")
    report = {"Redis 정상": {"조회": shape(read(4291)), "저장": shape(write(4291, 38.1)), "조회 50건 동시": burst(50)}}

    subprocess.run(["docker", "stop", "yeogirok-perf-redis-1"], capture_output=True)
    try:
        report["Redis 중단"] = {"조회": shape(read(4292)), "저장": shape(write(4292, 38.2)), "조회 50건 동시": burst(50)}
        report["Redis 중단"]["저장된 행(저장 요청 회원)"] = int(sql(
            "select count(*) from footprints where user_id = 4292 and deleted_at is null"))
    finally:
        subprocess.run(["docker", "start", "yeogirok-perf-redis-1"], capture_output=True)
    wait_redis()
    started = time.time()
    while True:
        result = write(4293, 38.3)
        if result[0] == 201 or time.time() - started > 180:
            break
        time.sleep(1)
    report["Redis 다시 뜬 뒤"] = {"새 저장": shape(result), "성공까지 초": round(time.time() - started, 1),
                              "조회": shape(read(4293))}
    sql(f"delete from footprints where user_id in ({users})")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    (ROOT / "results" / f"redis-outage-{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                                      encoding="utf-8")


if __name__ == "__main__":
    main()
