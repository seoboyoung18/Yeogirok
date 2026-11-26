"""카카오 역지오코딩이 느리거나 실패할 때 발자취 저장(API-041)과 다른 요청이 어떻게 되는지 잰다.

  python fake_kakao.py &                       # 가짜 카카오(18200)
  (앱을 .env.perf 에 아래 값을 넣고 띄운다)
     KAKAO_REST_API_KEY=perf-dummy
     YEOGIROK_KAKAO_LOCAL_BASE_URL=http://localhost:18200
     YEOGIROK_KAKAO_MOBILITY_BASE_URL=http://localhost:18200
  python external_api_failure.py --label run1

실제 카카오는 부르지 않는다. 모드마다: 저장 1건 → 서로 다른 회원 50명 동시 저장 → (hang 만) 300명 동시 저장 중 다른 회원 조회 20건.
동시 저장 동안 /actuator/prometheus 를 0.5초마다 읽어 커넥션 풀·서버 스레드 사용을 남긴다.
"""
import argparse
import json
import pathlib
import statistics
import threading
import time
import urllib.request
import uuid
from collections import Counter

import smoke
from concurrency_footprint import fire, request, sql
from run_load import scrape

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT, FAKE = 18080, "http://localhost:18200"
MODES = ["ok", "delay:1", "delay:5", "hang", "status:429", "status:500", "status:503", "badjson", "empty", "close"]
USERS = (4600, 4999)


def fake(path):
    with urllib.request.urlopen(FAKE + path, timeout=5) as response:
        return json.loads(response.read().decode())


def body(index):
    # 회원마다 다른 곳(서로 500m 넘게 떨어짐)이라 같은 날 500m 규칙에 걸리지 않는다.
    now = time.strftime("%Y-%m-%d %H:%M:%S+09", time.gmtime(time.time() + 9 * 3600))
    return json.dumps({"location": {"latitude": round(34.5 + index * 0.01, 7), "longitude": 127.9, "accuracyMeters": 5,
                                    "capturedAt": now}, "categoryCodes": ["CAFE"]}).encode()


def create(user, index):
    status, code, data, elapsed = request(PORT, "POST", "/api/v1/footprints", smoke.token(user), body(index),
                                          {"Idempotency-Key": str(uuid.uuid4())})
    return status, code, None, elapsed


def shape(results):
    seconds = sorted(elapsed / 1000 for *_, elapsed in results)
    return {"응답": dict(Counter(f"{status}" + (f" {code}" if code else "") for status, code, *_ in results)),
            "중앙값 초": round(statistics.median(seconds), 2), "최대 초": round(seconds[-1], 2)}


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.rows, self.done = [], threading.Event()

    def run(self):
        while not self.done.wait(0.5):
            try:
                self.rows.append(scrape())
            except OSError:
                pass

    def peak(self):
        return {name: max((row.get(name, 0) for row in self.rows), default=None)
                for name in ("hikari_active", "hikari_pending")}


def reset():
    sql(f"delete from footprints where user_id between {USERS[0]} and {USERS[1]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    report = {}
    for mode in MODES:
        reset()
        fake("/__mode?m=" + mode)
        fake("/__reset")
        single = shape([create(USERS[0], 0)])
        sampler = Sampler()
        sampler.start()
        burst = shape(fire([(lambda i=index: create(USERS[0] + 1 + i, 1 + i)) for index in range(50)]))
        sampler.done.set()
        entry = {"저장 1건": single, "동시 50건": burst, "동시 50건 중 최대": sampler.peak(),
                 "가짜 카카오가 받은 호출": fake("/__stats")["calls"],
                 "저장된 행": int(sql(f"select count(*) from footprints where user_id between {USERS[0]} and {USERS[1]}")),
                 # 저장 응답에는 주소가 없어 DB 에서 센다.
                 "그중 주소가 채워진 행": int(sql(
                     f"select count(*) from footprints where user_id between {USERS[0]} and {USERS[1]} and address is not null"))}
        if mode == "hang":
            reset()
            sampler = Sampler()
            sampler.start()
            jobs = [(lambda i=index: create(USERS[0] + i, i)) for index in range(300)]
            jobs += [(lambda: request(PORT, "GET", "/api/v1/users/me/footprints", smoke.token(4590))[::3])
                     for _ in range(20)]
            results = fire(jobs)
            sampler.done.set()
            reads = sorted(elapsed / 1000 for _, elapsed in results[300:])
            entry["동시 300건"] = shape(results[:300])
            entry["동시 300건 중 다른 회원 조회 20건"] = {
                "응답": dict(Counter(str(status) for status, _ in results[300:])),
                "중앙값 초": round(statistics.median(reads), 2), "최대 초": round(reads[-1], 2)}
            entry["동시 300건 중 최대"] = sampler.peak()
        report[mode] = entry
        print(mode, json.dumps(entry, ensure_ascii=False), flush=True)
    fake("/__mode?m=ok")
    started = time.time()
    after = create(USERS[1], 399)
    report["정상으로 돌린 뒤 첫 저장"] = {**shape([after]), "걸린 초": round(time.time() - started, 2), "주소가 채워졌나": bool(sql(
        f"select 1 from footprints where user_id = {USERS[1]} and address is not null"))}
    print("정상으로 돌린 뒤", json.dumps(report["정상으로 돌린 뒤 첫 저장"], ensure_ascii=False))
    reset()
    (ROOT / "results" / f"external-api-{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                                      encoding="utf-8")


if __name__ == "__main__":
    main()
