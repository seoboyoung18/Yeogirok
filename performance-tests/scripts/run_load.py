"""k6(도커)로 조회 API 를 시나리오별로 여러 번 재고, 그동안 서버 지표(/actuator/prometheus)를 1초마다 읽는다.

  python run_load.py --label baseline-100k --users 5000

앱은 performance-tests/.env.perf 로 떠 있어야 한다(README). 결과: results/load-<label>.json, k6 요약 원본은 results/raw/.
부하 발생기와 서버가 같은 PC 에서 돌아 서로 CPU 를 나눠 쓴다. 수치는 같은 조건의 전후 비교용이다.
"""
import argparse
import base64
import hashlib
import hmac
import json
import pathlib
import re
import statistics
import subprocess
import threading
import time
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV = dict(line.split("=", 1) for line in (ROOT / ".env.perf").read_text().splitlines() if "=" in line)
PORT = ENV["SERVER_PORT"]
GAUGES = {
    "hikari_active": r'^hikaricp_connections_active\{[^}]*\} (\S+)',
    "hikari_pending": r'^hikaricp_connections_pending\{[^}]*\} (\S+)',
    "process_cpu": r'^process_cpu_usage(?:\{[^}]*\})? (\S+)',
    "system_cpu": r'^system_cpu_usage(?:\{[^}]*\})? (\S+)',
    "tomcat_busy": r'^tomcat_threads_busy_threads\{[^}]*\} (\S+)',
}


def token():
    def b64url(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    now = int(time.time())
    head = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iss": "yeogirok", "sub": "1", "iat": now, "exp": now + 1800, "jti": str(uuid.uuid4()), "role": "USER"}
    signing = head + "." + b64url(json.dumps(claims, separators=(",", ":")).encode())
    return signing + "." + b64url(hmac.new(base64.b64decode(ENV["JWT_SECRET"]), signing.encode(), hashlib.sha256).digest())


def scrape():
    request = urllib.request.Request(f"http://localhost:{PORT}/actuator/prometheus")
    request.add_header("Authorization", "Bearer " + token())
    with urllib.request.urlopen(request, timeout=5) as response:
        text = response.read().decode()
    values = {}
    for name, pattern in GAUGES.items():
        found = re.findall(pattern, text, re.M)
        if found:
            values[name] = sum(float(value) for value in found)
    heap = re.findall(r'^jvm_memory_used_bytes\{area="heap"[^}]*\} (\S+)', text, re.M)
    values["heap_mb"] = sum(float(value) for value in heap) / 1048576
    return values


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.samples, self.stop = [], threading.Event()

    def run(self):
        while not self.stop.wait(1):
            try:
                self.samples.append((time.time(), scrape()))
            except OSError:
                pass

    def summary(self, since):
        rows = [values for at, values in self.samples if at >= since]
        return {name: {"avg": round(statistics.mean(row[name] for row in rows), 3),
                       "max": round(max(row[name] for row in rows), 3)}
                for name in (rows[0] if rows else {})}


def k6(scenario, args, out, rate):
    command = ["docker", "run", "--rm", "-v", f"{ROOT.as_posix()}:/perf", "-e", f"RATE={rate}",
               "-e", f"BASE_URL=http://host.docker.internal:{PORT}", "-e", "JWT_SECRET=" + ENV["JWT_SECRET"],
               "-e", f"SCENARIO={scenario}", "-e", f"VUS={args.vus}", "-e", f"WARMUP={args.warmup}",
               "-e", f"DURATION={args.duration}", "-e", f"USERS={args.users}",
               "grafana/k6:0.57.0", "run", "--quiet", "--summary-export", f"/perf/results/raw/{out}", "/perf/k6/read-api.js"]
    subprocess.run(command, check=True, capture_output=True, env={**__import__("os").environ, "MSYS_NO_PATHCONV": "1"})
    return json.loads((ROOT / "results" / "raw" / out).read_text())["metrics"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--scenarios", default="stats-city,stats-nation,tips-search,my-footprints,my-tips")
    parser.add_argument("--vus", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--users", type=int, required=True, help="시드 회원 수(= 시드 n/20, 최소 100)")
    parser.add_argument("--rates", default="", help="열린 모델 유입률. 예: stats-city=80,tips-search=200 (없는 시나리오는 닫힌 모델)")
    args = parser.parse_args()
    (ROOT / "results" / "raw").mkdir(parents=True, exist_ok=True)
    rates = dict(pair.split("=") for pair in args.rates.split(",") if pair)
    report = {"label": args.label, "rates": rates, "vus": args.vus, "warmup_s": args.warmup, "duration_s": args.duration, "runs": {}}
    for scenario in args.scenarios.split(","):
        runs = []
        for repeat in range(1, args.repeats + 1):
            sampler = Sampler()
            sampler.start()
            started = time.time()
            metrics = k6(scenario, args, f"{args.label}-{scenario}-{repeat}.json", rates.get(scenario, 0))
            sampler.stop.set()
            duration = metrics["http_req_duration{phase:main}"]
            run = {"rps": round(metrics["http_reqs{phase:main}"]["count"] / args.duration, 1),
                   "p50_ms": round(duration["med"], 2), "p95_ms": round(duration["p(95)"], 2),
                   "p99_ms": round(duration["p(99)"], 2), "max_ms": round(duration["max"], 2),
                   "failed_rate": metrics["http_req_failed{phase:main}"]["value"],
                   "dropped": metrics.get("dropped_iterations", {}).get("count", 0),
                   "server": sampler.summary(started + args.warmup + 5)}
            runs.append(run)
            server = run["server"]
            print(f"{scenario:<14} #{repeat} RPS {run['rps']:>8.1f} P50 {run['p50_ms']:>8.2f} P95 {run['p95_ms']:>8.2f} "
                  f"P99 {run['p99_ms']:>8.2f} 실패 {run['failed_rate']:.4f} 못 보냄 {run['dropped']} | 풀 사용 평균 "
                  f"{server.get('hikari_active', {}).get('avg')} 최대 "
                  f"{server.get('hikari_active', {}).get('max')} 대기 최대 {server.get('hikari_pending', {}).get('max')} "
                  f"앱 CPU 평균 {server.get('process_cpu', {}).get('avg')} PC CPU 평균 {server.get('system_cpu', {}).get('avg')}",
                  flush=True)
        report["runs"][scenario] = runs
    (ROOT / "results" / f"load-{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                              encoding="utf-8")


if __name__ == "__main__":
    main()
