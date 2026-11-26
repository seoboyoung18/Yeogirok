"""파일 업로드 흐름(API-012 발급 → S3 에 직접 PUT → API-086 완료 확인)을 S3 호환 저장소(S3Mock)로 확인한다.

  python file_upload.py --label run1

앱은 YEOGIROK_FILE_ENDPOINT=http://localhost:59090 로 떠 있어야 한다(README). 실제 AWS 는 부르지 않는다.
팁 사진(TIP_PHOTO)으로 본다(기록이 없어도 올릴 수 있다). S3Mock 은 서명·만료를 검사하지 않으므로
"만료된 URL", "서명한 크기와 다른 PUT 은 403" 은 여기서 확인할 수 없다.
"""
import argparse
import json
import pathlib
import statistics
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections import Counter

import smoke
from concurrency_footprint import fire, request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT, S3 = 18080, "http://localhost:59090"
JPEG, PNG = b"\xff\xd8\xff\xe0", b"\x89PNG\r\n\x1a\n"
MB = 1024 * 1024


def image(signature, size):
    return signature + b"\0" * (size - len(signature))


def api(method, path, user, payload=None):
    status, code, data, elapsed = request(PORT, method, path, smoke.token(user),
                                          None if payload is None else json.dumps(payload).encode(),
                                          {"Idempotency-Key": str(uuid.uuid4())})
    return status, code, data.get("data") or {}, elapsed


def issue(user, size, content_type="image/jpeg", name="a.jpg"):
    return api("POST", "/api/v1/uploads", user,
               {"purpose": "TIP_PHOTO", "fileName": name, "contentType": content_type, "sizeBytes": size})


def put(upload, data):
    req = urllib.request.Request(upload["uploadUrl"], data=data, method=upload.get("uploadMethod", "PUT"))
    for key, value in (upload.get("uploadHeaders") or {}).items():
        req.add_header(key, value)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as error:
        return error.code, (time.perf_counter() - started) * 1000


def complete(user, upload_id):
    return api("POST", f"/api/v1/uploads/{upload_id}/complete", user)


def keys(prefix):
    with urllib.request.urlopen(f"{S3}/perf-photos?list-type=2&prefix={prefix}", timeout=10) as response:
        root = ET.fromstring(response.read())
    space = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
    return [node.text for node in root.findall("s:Contents/s:Key", space)]


def body_of(key):
    with urllib.request.urlopen(f"{S3}/perf-photos/{key}", timeout=30) as response:
        return response.read()


def brief(result):
    status, code, data, elapsed = result
    return {"status": status, "code": code, "ms": round(elapsed), **({"uploadStatus": data["status"]} if "status" in data else {})}


def flow(user, declared, data, content_type="image/jpeg"):
    """발급 → PUT → 완료. 단계별 결과를 돌려준다."""
    issued = issue(user, declared, content_type)
    if issued[0] >= 400:
        return {"발급": brief(issued)}
    upload = issued[2]
    put_status, put_ms = put(upload, data)
    done = complete(user, upload["uploadId"])
    return {"발급": brief(issued), "PUT": put_status, "완료": brief(done), "uploadId": upload["uploadId"],
            "ms": {"발급": round(issued[3]), "PUT": round(put_ms), "완료": round(done[3])}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    user, report = 4701, {}

    def record(name, value):
        report[name] = value
        print(name, json.dumps(value, ensure_ascii=False), flush=True)

    before = (len(keys("tmp/")), len(keys("tip-photo/")))
    normal = flow(user, 100_000, image(JPEG, 100_000))
    record("1 정상 업로드(JPEG 100KB)", normal)
    record("1-1 확정 경로에 같은 내용이 있나", body_of("tip-photo/" + normal["uploadId"]) == image(JPEG, 100_000))
    record("2 선언보다 1바이트 작은 파일", flow(user, 100_000, image(JPEG, 99_999)))
    record("3 선언보다 1바이트 큰 파일", flow(user, 100_000, image(JPEG, 100_001)))
    record("4 최대 크기(20MB) 그대로 발급", brief(issue(user, 20 * MB)))
    record("5 최대 크기 + 1바이트 발급", brief(issue(user, 20 * MB + 1)))
    record("6 크기 0 발급", brief(issue(user, 0)))
    record("7 허용하지 않는 형식(image/gif) 발급", brief(issue(user, 1000, "image/gif", "a.gif")))
    record("8 PNG 로 선언하고 JPEG 내용", flow(user, 100_000, image(JPEG, 100_000), "image/png"))
    record("9 JPEG 로 선언하고 글자 내용", flow(user, 100_000, b"hello " * 16_666 + b"abcd"))
    record("10 PUT 없이 완료", brief(complete(user, issue(user, 1000)[2]["uploadId"])))
    theirs = issue(user, 1000)[2]
    put(theirs, image(JPEG, 1000))
    record("11 다른 회원이 완료 요청", brief(complete(user + 1, theirs["uploadId"])))
    record("12 없는 uploadId 완료", brief(complete(user, str(uuid.uuid4()))))

    # 완료 확인 뒤 같은 임시 주소에 다른 파일을 다시 올려도 확정본은 바뀌지 않아야 한다.
    upload = issue(user, 50_000)[2]
    put(upload, image(JPEG, 50_000))
    first = complete(user, upload["uploadId"])
    put_again, _ = put(upload, PNG + b"\1" * (50_000 - len(PNG)))
    again = complete(user, upload["uploadId"])
    record("13 완료 뒤 같은 주소에 다른 파일 PUT", {
        "처음 완료": brief(first), "다시 PUT": put_again, "다시 완료": brief(again),
        "확정본이 처음 내용 그대로인가": body_of("tip-photo/" + upload["uploadId"]) == image(JPEG, 50_000)})
    record("13-1 완료된 업로드에 URL 재발급 요청 없이 새 발급은 다른 uploadId 인가",
           issue(user, 50_000)[2]["uploadId"] != upload["uploadId"])

    # 같은 업로드의 완료 요청 20건 동시
    upload = issue(user, 50_000)[2]
    put(upload, image(JPEG, 50_000))
    results = fire([(lambda: complete(user, upload["uploadId"])) for _ in range(20)])
    record("14 같은 업로드 완료 20건 동시", {
        "응답": dict(Counter(f"{status}" + (f" {code}" if code else "") for status, code, *_ in results)),
        "확정본 수": len([key for key in keys("tip-photo/") if key.endswith(upload["uploadId"])])})

    # 크기별 걸린 시간(발급 + PUT + 완료), 각 5회
    sizes = {}
    for label, size in (("100KB", 100_000), ("1MB", MB), ("5MB", 5 * MB), ("20MB", 20 * MB)):
        runs = [flow(user, size, image(JPEG, size))["ms"] for _ in range(5)]
        sizes[label] = {step: round(statistics.median(run[step] for run in runs)) for step in ("발급", "PUT", "완료")}
    record("15 크기별 단계 시간 중앙값(ms, 5회)", sizes)

    # 서로 다른 회원 20명이 1MB 를 동시에
    started = time.perf_counter()
    results = fire([(lambda u=user + 10 + index: flow(u, MB, image(JPEG, MB))) for index in range(20)])
    record("16 20명 동시 1MB 업로드", {
        "완료 응답": dict(Counter(str(result["완료"]["status"]) for result in results)),
        "전체 걸린 초": round(time.perf_counter() - started, 2)})

    after = (len(keys("tmp/")), len(keys("tip-photo/")))
    record("17 저장소에 남은 객체(임시 tmp/, 확정 tip-photo/)", {"전": before, "후": after})
    (ROOT / "results" / f"file-upload-{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                                     encoding="utf-8")


if __name__ == "__main__":
    main()
