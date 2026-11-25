"""AR 팁 비동기 게시(API-049)가 서버 종료로 끊겼을 때 무엇이 남고, 다시 뜬 뒤 어떻게 되는지 잰다.

  python async_recovery.py --label fast --plan kill:10x3,kill:50x3,kill:100x3,stop40:100x3,stop10:100x3 --fast
  python async_recovery.py --label default --plan kill:50x1            # 운영과 같은 5분·5분 설정(한 번에 10분 안팎)

앱을 리눅스 컨테이너로 띄워 종료 신호를 구분한다(윈도우 프로세스로는 SIGTERM 을 보낼 수 없다).
  kill   = docker kill          (SIGKILL, 강제 종료)
  stop40 = docker stop -t 40    (SIGTERM 뒤 40초 기다림)
  stop10 = docker stop -t 10    (SIGTERM 뒤 10초 기다림, 도커 기본값 = 지금 서버 compose)
측정 전용 DB·Redis 만 쓴다. PC 에서 직접 띄운 측정용 앱(18080)은 내려 둔다(그 앱의 복구 작업이 같은 DB 를 건드린다).

한 번의 실행: 팁 등록 N건을 한꺼번에 보내고, 첫 202 가 오자마자 앱을 끈다 → DB 상태를 센다 → 앱을 다시 띄운다
→ REGISTERING 이 없어질 때까지 기다린다(복구 작업이 FAILED 로 돌림) → FAILED 팁을 API-051 로 재시도 → PUBLISHED 확인.
--fast 는 복구 주기·기준 시간을 20초로 줄인 설정이다(반복 측정용). 운영 값(5분·5분)과 다르다.
"""
import argparse
import json
import pathlib
import subprocess
import threading
import time
import uuid

import smoke
from concurrency_footprint import request, sql
from idempotency_failures import REDIS, post, tip_body, wait_redis

ROOT = pathlib.Path(__file__).resolve().parents[1]
NAME, PORT, IMAGE = "yeogirok-perf-app", 18090, "eclipse-temurin:21-jre"
AUTHORS = (4400, 4899)


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True)


def start_app(fast):
    env = dict(line.split("=", 1) for line in (ROOT / ".env.perf").read_text().splitlines() if "=" in line)
    env.update({"DB_URL": "jdbc:postgresql://db:5432/yeogirok_perf", "REDIS_HOST": "redis", "REDIS_PORT": "6379",
                "SERVER_PORT": "8080"})
    if fast:
        env.update({"YEOGIROK_TIP_STUCK_CHECK_INTERVAL": "PT20S", "YEOGIROK_TIP_STUCK_REGISTERING_TIMEOUT": "PT20S"})
    env_file = ROOT / "results" / "raw" / "app-container.env"
    env_file.write_text("\n".join(f"{key}={value}" for key, value in env.items()) + "\n")
    docker("rm", "-f", NAME)
    result = docker("run", "-d", "--name", NAME, "--network", "yeogirok-perf_default", "-p", f"{PORT}:8080",
                    "--env-file", str(env_file), "-v", f"{(ROOT / 'results' / 'raw' / 'app.jar').as_posix()}:/app/app.jar:ro",
                    IMAGE, "java", "-Xms1g", "-Xmx1g", "-jar", "/app/app.jar")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode())
    started = time.time()
    while time.time() - started < 180:
        if request(PORT, "GET", "/api/v1/users/me/tips", smoke.token(AUTHORS[0]))[0] == 200:
            return time.time()
        time.sleep(1)
    raise RuntimeError("컨테이너 앱이 뜨지 않았습니다: " + docker("logs", "--tail", "30", NAME).stdout.decode()[-2000:])


def statuses():
    out = sql(f"select status || '=' || count(*) from ar_tips where author_id between {AUTHORS[0]} and {AUTHORS[1]} group by status")
    return {key: int(value) for key, value in (line.split("=") for line in out.split())}


def registering_ids():
    return [int(value) for value in sql(
        f"select id from ar_tips where author_id between {AUTHORS[0]} and {AUTHORS[1]} and status = 'REGISTERING'").split()]


def redis_state(tip_id):
    raw = subprocess.run(REDIS + ["get", f"tip-publish:{tip_id}"], capture_output=True).stdout.decode().strip()
    try:
        state = json.loads(raw)
        return {key: state.get(key) for key in ("status", "retryable", "errorCode") if key in state} or raw[:120]
    except ValueError:
        return raw[:120] or None


def run(count, mode, fast, twice=False, lose_redis=False):
    sql(f"delete from ar_tips where author_id between {AUTHORS[0]} and {AUTHORS[1]}")
    first, results = threading.Event(), [None] * count

    def create(index):
        results[index] = post(PORT, "/api/v1/tips", AUTHORS[0] + index, str(uuid.uuid4()), tip_body(36.7, 128.7))
        if results[index]["status"] == 202:
            first.set()

    threads = [threading.Thread(target=create, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    first.wait(30)
    stop_started = time.time()
    if mode == "kill":
        docker("kill", NAME)
    else:
        docker("stop", "-t", mode.removeprefix("stop"), NAME)
    stop_seconds = time.time() - stop_started
    exit_code = docker("inspect", "-f", "{{.State.ExitCode}}", NAME).stdout.decode().strip()
    for thread in threads:
        thread.join()
    after_stop = statuses()
    stuck = registering_ids()
    entry = {"요청": count, "종료": mode, "종료에 걸린 초": round(stop_seconds, 1), "컨테이너 종료 코드": exit_code,
             "202 받은 요청": sum(1 for r in results if r["status"] == 202),
             "응답 못 받은 요청": sum(1 for r in results if r["status"] == 0),
             "종료 직후 DB": after_stop, "끊긴 팁(REGISTERING)": len(stuck)}
    if stuck:
        entry["끊긴 팁의 Redis 상태 예"] = redis_state(stuck[0])
    if lose_redis:
        docker("restart", "yeogirok-perf-redis-1")
        wait_redis()
        entry["Redis 재시작(키 유실)"] = True
    ready = start_app(fast)
    if twice and stuck:
        time.sleep(5)
        docker("kill", NAME)
        entry["복구 전에 한 번 더 강제 종료"] = True
        ready = start_app(fast)
    deadline = time.time() + (120 if fast else 900)
    while registering_ids() and time.time() < deadline:
        time.sleep(2)
    entry["복구까지 초(끊긴 때부터)"] = round(time.time() - stop_started, 1) if stuck else 0
    entry["복구까지 초(다시 뜬 때부터)"] = round(time.time() - ready, 1) if stuck else 0
    entry["복구 뒤 DB"] = statuses()
    entry["기한 안에 복구 안 된 팁"] = len(registering_ids())
    failed = [int(value) for value in sql(
        f"select id || ':' || author_id from ar_tips where author_id between {AUTHORS[0]} and {AUTHORS[1]} "
        "and status = 'FAILED'").replace(":", " ").split()]
    pairs = list(zip(failed[0::2], failed[1::2]))
    if pairs:
        entry["복구된 팁의 Redis 상태 예"] = redis_state(pairs[0][0])
    retries = [post(PORT, f"/api/v1/tips/{tip_id}/retry", author, str(uuid.uuid4()), b"{}") for tip_id, author in pairs]
    entry["재시도(051) 응답"] = {}
    for result in retries:
        key = f"{result['status']}" + (f" {result['code']}" if result["code"] else "")
        entry["재시도(051) 응답"][key] = entry["재시도(051) 응답"].get(key, 0) + 1
    deadline = time.time() + 30
    while statuses().get("REGISTERING") and time.time() < deadline:
        time.sleep(1)
    entry["재시도 뒤 DB"] = statuses()
    entry["전체 행 수 = 종료 직후 행 수"] = sum(entry["재시도 뒤 DB"].values()) == sum(after_stop.values())
    return entry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--plan", required=True, help="예: kill:10x3,stop40:100x3,kill2:50x1,killredis:50x1")
    parser.add_argument("--fast", action="store_true")
    args = parser.parse_args()
    report = {"fast": args.fast, "runs": []}
    start_app(args.fast)
    for item in args.plan.split(","):
        mode, rest = item.split(":")
        count, repeats = (int(value) for value in rest.split("x"))
        for _ in range(repeats):
            entry = run(count, "kill" if mode.startswith("kill") else mode, args.fast,
                        twice=mode == "kill2", lose_redis=mode == "killredis")
            report["runs"].append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    docker("rm", "-f", NAME)
    sql(f"delete from ar_tips where author_id between {AUTHORS[0]} and {AUTHORS[1]}")
    (ROOT / "results" / f"async-recovery-{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                                        encoding="utf-8")


if __name__ == "__main__":
    main()
