"""장시간 안정성(TEST 10). k6(도커)로 조회·저장을 섞어 일정한 속도로 보내면서 서버·DB·Redis 상태를 주기적으로 읽는다.

  python soak.py --label run1 --windows 6 --window-seconds 600

k6 를 구간마다 한 번씩 이어서 돌린다(구간별 지연 시간을 따로 얻으려고). 구간 사이에 컨테이너가 다시 뜨는 몇 초 동안은 요청이 없다.
앱은 performance-tests/.env.perf 로 떠 있어야 한다. 측정이 끝나면 이번에 저장한 발자취를 지운다.
부하 발생기와 서버가 같은 PC 에서 돈다. 수치는 시간에 따른 변화(늘어나는지)를 보는 용도다.
"""
import argparse
import json
import os
import re
import subprocess
import threading
import time
import urllib.request

import run_load
from concurrency_footprint import sql

ROOT = run_load.ROOT
REDIS = ["docker", "exec", "yeogirok-perf-redis-1", "redis-cli"]
SCENARIOS = ["statsCity", "tipsSearch", "myFootprints", "myTips", "saveFootprint"]
SINGLE = {
    "live_threads": r'^jvm_threads_live_threads(?:\{[^}]*\})? (\S+)',
    "hikari_active": r'^hikaricp_connections_active\{[^}]*\} (\S+)',
    "hikari_pending": r'^hikaricp_connections_pending\{[^}]*\} (\S+)',
    "hikari_timeout_total": r'^hikaricp_connections_timeout_total\{[^}]*\} (\S+)',
    "tomcat_busy": r'^tomcat_threads_busy_threads\{[^}]*\} (\S+)',
    "process_cpu": r'^process_cpu_usage(?:\{[^}]*\})? (\S+)',
    "classes_loaded": r'^jvm_classes_loaded_classes(?:\{[^}]*\})? (\S+)',
    "live_data_mb": r'^jvm_gc_live_data_size_bytes(?:\{[^}]*\})? (\S+)',
}
SUMS = {
    "heap_used_mb": r'^jvm_memory_used_bytes\{area="heap"[^}]*\} (\S+)',
    "nonheap_used_mb": r'^jvm_memory_used_bytes\{area="nonheap"[^}]*\} (\S+)',
    "gc_pause_count": r'^jvm_gc_pause_seconds_count\{[^}]*\} (\S+)',
    "gc_pause_seconds": r'^jvm_gc_pause_seconds_sum\{[^}]*\} (\S+)',
    "http_5xx_total": r'^http_server_requests_seconds_count\{[^}]*status="5\d\d"[^}]*\} (\S+)',
    "http_total": r'^http_server_requests_seconds_count\{[^}]*\} (\S+)',
}


def sample():
    request = urllib.request.Request(f"http://localhost:{run_load.PORT}/actuator/prometheus")
    request.add_header("Authorization", "Bearer " + run_load.token())
    with urllib.request.urlopen(request, timeout=10) as response:
        text = response.read().decode()
    values = {}
    for name, pattern in {**SINGLE, **SUMS}.items():
        values[name] = sum(float(value) for value in re.findall(pattern, text, re.M))
    for name in ("heap_used_mb", "nonheap_used_mb", "live_data_mb"):
        values[name] = round(values[name] / 1048576, 1)
    info = subprocess.run(REDIS + ["info", "memory"], capture_output=True).stdout.decode()
    values["redis_used_mb"] = round(int(re.search(r"used_memory:(\d+)", info)[1]) / 1048576, 2)
    values["redis_keys"] = int(subprocess.run(REDIS + ["dbsize"], capture_output=True).stdout.decode().split()[-1])
    row = sql("select (select count(*) from pg_stat_activity where datname = current_database()),"
              " (select count(*) from pg_stat_activity where datname = current_database() and state = 'idle in transaction'),"
              " pg_database_size(current_database()), (select count(*) from footprints)").split("|")
    values.update({"db_connections": int(row[0]), "db_idle_in_transaction": int(row[1]),
                   "db_size_mb": round(int(row[2]) / 1048576, 1), "footprints": int(row[3])})
    return values


class Sampler(threading.Thread):
    def __init__(self, interval):
        super().__init__(daemon=True)
        self.interval, self.samples, self.stop = interval, [], threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                self.samples.append({"at": round(time.time()), **sample()})
            except (OSError, subprocess.CalledProcessError, TypeError, IndexError) as error:
                self.samples.append({"at": round(time.time()), "error": type(error).__name__})
            self.stop.wait(self.interval)


def k6(label, index, seconds, users, scale):
    out = f"soak-{label}-{index}.json"
    command = ["docker", "run", "--rm", "-v", f"{ROOT.as_posix()}:/perf",
               "-e", f"BASE_URL=http://host.docker.internal:{run_load.PORT}", "-e", "JWT_SECRET=" + run_load.ENV["JWT_SECRET"],
               "-e", f"DURATION={seconds}", "-e", f"USERS={users}", "-e", f"SCALE={scale}",
               "grafana/k6:0.57.0", "run", "--quiet", "--summary-export", f"/perf/results/raw/{out}", "/perf/k6/soak.js"]
    subprocess.run(command, capture_output=True, env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    metrics = json.loads((ROOT / "results" / "raw" / out).read_text())["metrics"]
    window = {"dropped_iterations": metrics.get("dropped_iterations", {}).get("count", 0),
              "checks_failed": metrics.get("checks", {}).get("fails", 0)}
    for name in SCENARIOS:
        duration = metrics[f"http_req_duration{{scenario:{name}}}"]
        window[name] = {"count": metrics[f"http_reqs{{scenario:{name}}}"]["count"],
                        "failed_rate": metrics[f"http_req_failed{{scenario:{name}}}"]["value"],
                        "p50_ms": round(duration["med"], 1), "p95_ms": round(duration["p(95)"], 1),
                        "p99_ms": round(duration["p(99)"], 1), "max_ms": round(duration["max"], 1)}
    return window


def average(samples, name):
    values = [row[name] for row in samples if name in row]
    return round(sum(values) / len(values), 2) if values else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--windows", type=int, default=6)
    parser.add_argument("--window-seconds", type=int, default=600)
    parser.add_argument("--users", type=int, default=5000)
    parser.add_argument("--scale", type=float, default=1)
    args = parser.parse_args()
    (ROOT / "results" / "raw").mkdir(parents=True, exist_ok=True)
    started_sql = sql("select now()")
    before = sample()
    sampler = Sampler(10)
    sampler.start()
    report = {"label": args.label, "window_seconds": args.window_seconds, "scale": args.scale, "before": before, "windows": []}
    for index in range(1, args.windows + 1):
        opened, first = time.time(), len(sampler.samples)
        window = k6(args.label, index, args.window_seconds, args.users, args.scale)
        rows = sampler.samples[first:]
        ends = [row for row in rows if "heap_used_mb" in row]
        window["server"] = {
            "seconds": round(time.time() - opened),
            **{name + "_avg": average(rows, name) for name in ("heap_used_mb", "process_cpu", "hikari_active", "tomcat_busy")},
            **{name + "_max": max((row.get(name, 0) for row in rows), default=None)
               for name in ("heap_used_mb", "hikari_active", "hikari_pending", "tomcat_busy", "live_threads", "db_connections",
                            "db_idle_in_transaction")},
            **{name + "_end": ends[-1][name] if ends else None
               for name in ("live_data_mb", "nonheap_used_mb", "live_threads", "classes_loaded", "gc_pause_count", "gc_pause_seconds",
                            "http_5xx_total", "http_total", "hikari_timeout_total", "redis_used_mb", "redis_keys", "db_size_mb",
                            "footprints")},
            "sample_errors": sum("error" in row for row in rows)}
        report["windows"].append(window)
        print(index, json.dumps(window, ensure_ascii=False), flush=True)
        (ROOT / "results" / f"soak-{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    sampler.stop.set()
    time.sleep(60)  # 부하가 끝난 뒤 가라앉은 상태
    report["after_idle_60s"] = sample()
    report["footprints_written"] = int(sql(f"select count(*) from footprints where created_at >= '{started_sql}'"))
    sql(f"delete from footprints where created_at >= '{started_sql}'")
    report["samples"] = sampler.samples
    (ROOT / "results" / f"soak-{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("끝", json.dumps({"before": before, "after_idle_60s": report["after_idle_60s"],
                           "footprints_written": report["footprints_written"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
