"""발자취 저장(API-041) 동시성 확인. 측정용 앱에 HTTP 요청을 한꺼번에 보내고, 응답과 DB 에 남은 행을 대조한다.

  python concurrency_footprint.py --label lock                       # 앱 1대(18080)
  python concurrency_footprint.py --label two-instances --ports 18080,18081
  python concurrency_footprint.py --label no-lock --ports 18082      # 잠금을 뺀 비교용 빌드

측정 전용 DB 에서만 돈다. 시드 회원(1~)이 있어야 한다. 요청마다 멱등키가 달라 멱등 처리와는 따로, 사용자 잠금만 본다.
모든 스레드가 준비된 뒤 한꺼번에 출발한다(Barrier). 실제로 서버 안에서 겹친 정도는 커넥션 풀(10)·서버 스레드(200)에 막힌다.
"""
import argparse
import datetime
import http.client
import json
import pathlib
import statistics
import subprocess
import threading
import time
import uuid
from collections import Counter

import smoke

ROOT = pathlib.Path(__file__).resolve().parents[1]
PSQL = ["docker", "exec", "-i", "yeogirok-perf-db-1", "psql", "-U", "perf", "-d", "yeogirok_perf", "-q", "-At"]
BASE_LAT, BASE_LNG = 36.0, 128.0  # 시드 도시와 떨어진 곳
SCALES = [5, 10, 50, 100, 200, 500]


def sql(statement):
    return subprocess.run(PSQL + ["-c", statement], capture_output=True, check=True).stdout.decode().strip()


def captured_at():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    return now.strftime("%Y-%m-%d %H:%M:%S+09")


def body(lat, lng):
    return json.dumps({"location": {"latitude": round(lat, 7), "longitude": round(lng, 7), "accuracyMeters": 5,
                                    "capturedAt": captured_at()}, "categoryCodes": ["CAFE"]})


def request(port, method, path, token, payload=None, headers=None):
    """(상태 코드, 오류 코드 또는 None, 응답 본문 dict, 걸린 ms). 연결 실패는 상태 코드 0."""
    started = time.perf_counter()
    try:
        connection = http.client.HTTPConnection("localhost", port, timeout=60)
        connection.request(method, path, body=payload, headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        data = json.loads(response.read().decode() or "{}")
        connection.close()
        return response.status, data.get("code"), data, (time.perf_counter() - started) * 1000
    except (OSError, ValueError, http.client.HTTPException) as error:
        return 0, type(error).__name__, {}, (time.perf_counter() - started) * 1000


def fire(jobs):
    """jobs: 인자 없는 함수 목록. 모두 준비된 뒤 한꺼번에 실행하고 결과를 돌려준다."""
    barrier, results = threading.Barrier(len(jobs)), [None] * len(jobs)

    def run(index, job):
        barrier.wait()
        results[index] = job()

    threads = [threading.Thread(target=run, args=(index, job)) for index, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def summarize(results):
    outcome = Counter(f"{status}" + (f" {code}" if code else "") for status, code, _, _ in results)
    times = sorted(elapsed for _, _, _, elapsed in results)
    return {"outcome": dict(outcome), "server_error_5xx": sum(1 for status, *_ in results if status >= 500),
            "connection_failed": sum(1 for status, *_ in results if status == 0),
            "p50_ms": round(statistics.median(times), 1), "p95_ms": round(times[int(len(times) * 0.95) - 1], 1),
            "max_ms": round(times[-1], 1)}


def active_rows(user_ids):
    ids = ",".join(str(user_id) for user_id in user_ids)
    return int(sql(f"select count(*) from footprints where user_id in ({ids}) and deleted_at is null"))


def reset(user_ids):
    ids = ",".join(str(user_id) for user_id in user_ids)
    sql(f"delete from footprints where user_id in ({ids})")


def deadlocks():
    return int(sql("select deadlocks from pg_stat_database where datname = 'yeogirok_perf'"))


def create_job(port, token, lat, lng):
    return lambda: request(port, "POST", "/api/v1/footprints", token, body(lat, lng),
                           {"Idempotency-Key": str(uuid.uuid4())})


def same_user(ports, user_id, count, offset_meters=0.0):
    """같은 사용자가 같은 곳(또는 offset_meters 간격으로 늘어선 곳)에 count 건. 500m 안이면 1건만 저장돼야 한다."""
    reset([user_id])
    token = smoke.token(user_id)
    step = offset_meters / 111_000  # 위도 1도 ≈ 111km
    results = fire([create_job(ports[index % len(ports)], token, BASE_LAT + index * step, BASE_LNG)
                    for index in range(count)])
    summary = summarize(results)
    summary["rows_saved"] = active_rows([user_id])
    return summary


def with_bystander(port, user_id, count):
    """같은 사용자의 저장 count 건이 몰리는 동안, 다른 회원의 목록 조회(042) 20건이 얼마나 걸리는지 본다."""
    reset([user_id])
    token, other = smoke.token(user_id), smoke.token(user_id + 1)
    jobs = [create_job(port, token, BASE_LAT, BASE_LNG) for _ in range(count)]
    jobs += [lambda: request(port, "GET", "/api/v1/users/me/footprints", other) for _ in range(20)]
    results = fire(jobs)
    return {"writer": summarize(results[:count]), "bystander_reads": summarize(results[count:]),
            "rows_saved": active_rows([user_id])}


def create_and_delete(port, user_id, count):
    """발자취 1건이 있는 상태에서 그 삭제 1건과 근처 저장 count 건을 동시에. 끝난 뒤 500m 안에 남은 것이 1건 이하여야 한다."""
    reset([user_id])
    token = smoke.token(user_id)
    status, _, data, _ = create_job(port, token, BASE_LAT, BASE_LNG)()
    footprint = data["data"]
    jobs = [create_job(port, token, BASE_LAT, BASE_LNG) for _ in range(count)]
    jobs.append(lambda: request(port, "DELETE", f"/api/v1/footprints/{footprint['id']}?version=1", token))
    results = fire(jobs)
    return {"creates": summarize(results[:count]), "delete": summarize(results[count:])["outcome"],
            "rows_saved": active_rows([user_id])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--ports", default="18080")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--scales", default=",".join(map(str, SCALES)))
    args = parser.parse_args()
    ports = [int(port) for port in args.ports.split(",")]
    scales = [int(scale) for scale in args.scales.split(",")]
    user_id, started_deadlocks = 4001, deadlocks()
    report = {"label": args.label, "ports": ports, "repeats": args.repeats, "scenarios": {}}

    def record(name, run):
        runs = [run() for _ in range(args.repeats)]
        report["scenarios"][name] = runs
        print(name, json.dumps(runs, ensure_ascii=False), flush=True)

    for count in scales:
        record(f"같은 사용자·같은 위치 {count}건", lambda: same_user(ports, user_id, count))
    record("같은 사용자·10m 간격 50건(모두 500m 안)", lambda: same_user(ports, user_id, 50, 10))
    record("같은 사용자·600m 간격 50건(모두 500m 밖)", lambda: same_user(ports, user_id, 50, 600))

    def different_users():
        users = list(range(4100, 4150))
        reset(users)
        results = fire([create_job(ports[index % len(ports)], smoke.token(user), BASE_LAT, BASE_LNG)
                        for index, user in enumerate(users)])
        return {**summarize(results), "rows_saved": active_rows(users)}

    record("서로 다른 사용자 50명·같은 위치", different_users)
    record("저장 20건 + 삭제 1건 동시", lambda: create_and_delete(ports[0], user_id, 20))
    record("같은 사용자 200건 중 다른 회원 조회 20건", lambda: with_bystander(ports[0], user_id, 200))
    report["deadlocks"] = deadlocks() - started_deadlocks
    print("데드락", report["deadlocks"])
    (ROOT / "results" / f"concurrency-{args.label}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
