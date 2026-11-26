"""공식 코스 적재(course-seed)를 측정용 DB·Redis·S3Mock 으로 돌려, 반복 실행·동시 실행·강제 종료 뒤 재실행을 확인한다.

  python seed_pipeline.py --label run1 --small <묶음 폴더(작은 것)> --large <묶음 폴더(큰 것)>

묶음 폴더는 `backend/tools/tour-course-fetch` 의 bundle 결과다(bundle-*.json + photos/). 적재가 폴더에 처리 표시(.done)를
쓸 수 있으므로 원본이 아니라 사본을 넘긴다. 묶음 데이터는 Git 에 올리지 않는다.
앱을 적재 설정으로 따로 띄운다(포트 18096·18097). 실제 AWS·투어API 는 부르지 않는다.
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET

from concurrency_footprint import sql

ROOT = pathlib.Path(__file__).resolve().parents[1]
S3 = "http://localhost:59090"
JAR = ROOT / "results" / "raw" / "app.jar"
DONE = re.compile(r"공식 코스 적재 끝\. 넣음=(\d+), 건너뜀=(\d+), 실패=(\d+)")
BUSY = "공식 코스 적재가 이미 돌고 있어 건너뜁니다"
REDIS = ["docker", "exec", "yeogirok-perf-redis-1", "redis-cli"]


def start(port, location, name):
    env = dict(os.environ)
    env.update(line.split("=", 1) for line in (ROOT / ".env.perf").read_text().splitlines() if "=" in line)
    env.update({"SERVER_PORT": str(port), "COURSE_SEED_ENABLED": "true", "COURSE_SEED_SOURCE": "local",
                "COURSE_SEED_LOCATION": str(location)})
    log = ROOT / "results" / f"raw-seed-{name}.log"
    process = subprocess.Popen(["java", "-Xms512m", "-Xmx1g", "-jar", str(JAR)], env=env,
                               stdout=open(log, "w", encoding="utf-8"), stderr=subprocess.STDOUT)
    return process, log


def outcome(log, timeout=600):
    """적재가 끝났다는 로그(또는 '이미 돌고 있어 건너뜀')가 나올 때까지 기다린다."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = log.read_text(encoding="utf-8", errors="replace")
        found = DONE.search(text)
        if found:
            return {"넣음": int(found[1]), "건너뜀": int(found[2]), "실패": int(found[3])}
        if BUSY in text:
            return {"결과": "이미 돌고 있어 건너뜀"}
        if "APPLICATION FAILED" in text:
            return {"결과": "앱 시작 실패"}
        time.sleep(1)
    return {"결과": "기한 안에 끝나지 않음"}


def s3_keys(prefix="record-photo/"):
    found, token = [], None
    while True:
        url = f"{S3}/perf-photos?list-type=2&prefix={prefix}" + (f"&continuation-token={token}" if token else "")
        with urllib.request.urlopen(url, timeout=30) as response:
            root = ET.fromstring(response.read())
        space = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
        found += [node.text for node in root.findall("s:Contents/s:Key", space)]
        token = root.findtext("s:NextContinuationToken", namespaces=space)
        if not token:
            return found


def counts():
    row = sql("""select (select count(*) from travel_rooms where title like 'tourapi-course:%'),
                        (select count(distinct title) from travel_rooms where title like 'tourapi-course:%'),
                        (select count(*) from travel_records where published_at is not null),
                        (select count(*) from actual_visits), (select count(*) from visit_images),
                        (select count(*) from places where provider = 'TOUR_API')""").split("|")
    keys = set(s3_keys())
    paths = set(sql("select image_path from visit_images").split())
    return {"방": int(row[0]), "방 제목 가짓수": int(row[1]), "게시된 기록": int(row[2]), "방문": int(row[3]),
            "사진 행": int(row[4]), "투어API 장소": int(row[5]), "저장소 사진": len(keys),
            "DB 에 없는 저장소 사진(고아)": len(keys - paths), "저장소에 없는 사진 행": len(paths - keys)}


def reset():
    sql("truncate actual_visits, visit_stories, visit_images, travel_records, room_members, travel_rooms, room_places, places cascade")
    sql("delete from users where provider_subject = 'system:tourapi'")
    for key in s3_keys():
        urllib.request.urlopen(urllib.request.Request(f"{S3}/perf-photos/{key}", method="DELETE"), timeout=30)
    subprocess.run(REDIS + ["del", "course-seed:lock"], capture_output=True)


def stop(*processes):
    for process in processes:
        process.kill()
        process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--small", required=True)
    parser.add_argument("--large", required=True)
    args = parser.parse_args()
    small, large = pathlib.Path(args.small).resolve(), pathlib.Path(args.large).resolve()
    report = {}

    def record(name, value):
        report[name] = value
        print(name, json.dumps(value, ensure_ascii=False), flush=True)

    # 1. 빈 상태에서 적재 → 2. 같은 묶음으로 다시
    reset()
    started = time.time()
    process, log = start(18096, small, "first")
    record("1 처음 적재(작은 묶음)", {**outcome(log), "앱 시작부터 초": round(time.time() - started, 1), **counts()})
    stop(process)
    process, log = start(18096, small, "again")
    record("2 같은 묶음으로 다시", {**outcome(log), **counts()})
    stop(process)

    # 3. 두 서버가 동시에 시작
    reset()
    first, log1 = start(18096, small, "two-a")
    second, log2 = start(18097, small, "two-b")
    record("3 두 서버 동시 시작", {"서버 A": outcome(log1), "서버 B": outcome(log2), **counts()})
    stop(first, second)

    # 4. 큰 묶음 적재 중 강제 종료 → 5. 바로 다시 띄움 → 6. 잠금을 지우고 다시 띄움
    reset()
    process, log = start(18096, large, "kill")
    deadline = time.time() + 300
    while int(sql("select count(*) from travel_records")) < 60 and time.time() < deadline:
        time.sleep(0.5)
    stop(process)
    killed = counts()
    lock_ttl = subprocess.run(REDIS + ["ttl", "course-seed:lock"], capture_output=True).stdout.decode().strip()
    record("4 큰 묶음 적재 중 강제 종료", {**killed, "남은 적재 잠금 수명(초)": int(lock_ttl)})
    process, log = start(18096, large, "restart-locked")
    record("5 바로 다시 띄움(잠금이 남은 상태)", {**outcome(log, 120), **counts()})
    stop(process)
    subprocess.run(REDIS + ["del", "course-seed:lock"], capture_output=True)
    started = time.time()
    process, log = start(18096, large, "restart")
    record("6 잠금을 지우고 다시 띄움", {**outcome(log, 900), "앱 시작부터 초": round(time.time() - started, 1), **counts()})
    stop(process)

    reset()
    (ROOT / "results" / f"seed-pipeline-{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                                       encoding="utf-8")


if __name__ == "__main__":
    main()
