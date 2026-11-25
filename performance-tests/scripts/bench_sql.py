"""데이터 규모별로 시드를 다시 넣고, API 가 실제로 보내는 SQL 을 EXPLAIN (ANALYZE, BUFFERS) 로 잰다.

  python bench_sql.py --label baseline --scales 1000,10000,100000,1000000

측정 전용 DB(performance-tests/docker-compose.perf.yml)에서만 돈다. 쿼리는 pg_stat_statements 로 확인한
Hibernate·네이티브 SQL 과 같은 조건이고, 매개변수 자리에 값을 직접 넣었다(JDBC 가 처음 몇 번 쓰는 실행 계획과 같다).
쿼리마다 준비 1회 뒤 --runs 회 재서 중앙값·최소·최대를 남긴다. 결과: results/sql-<label>.json, 가장 큰 규모의 실행 계획 원문.
"""
import argparse
import json
import pathlib
import statistics
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
PSQL = ["docker", "exec", "-i", "yeogirok-perf-db-1", "psql", "-U", "perf", "-d", "yeogirok_perf", "-q", "-At",
        "-v", "ON_ERROR_STOP=1"]
FOOTPRINT_COLS = ("id,address,body,category_codes,created_at,deleted_at,latitude,legal_area_code,location_captured_at,"
                  "location_name,location_policy_version,longitude,updated_at,user_id,version,visited_at")
SEOUL = "latitude >= 37.45 AND latitude < 37.65 AND longitude >= 126.85 AND longitude < 127.15"
GRID = """SELECT lat_index, lng_index, codes, COUNT(*), SUM(latitude), SUM(longitude),
       SUM(CASE WHEN user_id = -1 THEN 0 ELSE 1 END)
FROM (SELECT FLOOR(latitude / 0.01) AS lat_index, FLOOR(longitude / 0.01) AS lng_index,
             CAST(category_codes AS VARCHAR) AS codes, latitude, longitude, user_id
      FROM footprints WHERE deleted_at IS NULL AND {where}) cells
GROUP BY lat_index, lng_index, codes"""

QUERIES = {
    "046 방문 분포(서울 화면)": GRID.format(where=SEOUL),
    "046 방문 분포(동네 화면)": GRID.format(
        where="latitude >= 37.55 AND latitude < 37.57 AND longitude >= 126.97 AND longitude < 127.00"),
    "046 방문 분포(서울, 최근 30일)": GRID.format(
        where=SEOUL + " AND visited_at >= now() - interval '30 day' AND visited_at < now()"),
    "046 방문 분포(서울, 카테고리)": GRID.format(
        where=SEOUL + """ AND CAST(category_codes AS VARCHAR) LIKE '%"CAFE"%'"""),
    "046 방문 분포(전국, 도 단위)": """SELECT prefix, lat_index, lng_index, codes, COUNT(*), SUM(latitude), SUM(longitude)
FROM (SELECT SUBSTRING(legal_area_code, 1, 2) AS prefix,
             CASE WHEN legal_area_code IS NULL THEN FLOOR(latitude / 0.64) ELSE 0 END AS lat_index,
             CASE WHEN legal_area_code IS NULL THEN FLOOR(longitude / 0.64) ELSE 0 END AS lng_index,
             CAST(category_codes AS VARCHAR) AS codes, latitude, longitude
      FROM footprints WHERE deleted_at IS NULL AND latitude >= 33 AND latitude < 39 AND longitude >= 124
        AND longitude < 132) provinces
GROUP BY prefix, lat_index, lng_index, codes""",
    "047 주변 팁(100m 사각형)": """select * from ar_tips where status = 'PUBLISHED' and deleted_at is null
  and latitude between 37.5656 and 37.5674 and longitude between 126.97686 and 126.97914
  and (null is null or category_code = null)""",
    "041 같은 날 내 발자취": f"""select {FOOTPRINT_COLS} from footprints where user_id = 1 and deleted_at is null
  and created_at >= date_trunc('day', now()) + interval '4 hour' - interval '1 day'
  and created_at < date_trunc('day', now()) + interval '4 hour'""",
    "042 내 발자취 목록": f"""select {FOOTPRINT_COLS} from footprints where user_id = 1 and deleted_at is null
  order by visited_at desc, id desc fetch first 21 rows only""",
    "053 내 팁 목록": """select * from ar_tips where author_id = 1 and deleted_at is null
  and status in ('REGISTERING','PUBLISHED','FAILED')
  and (created_at < now() or (created_at = now() and id < 9223372036854775807))
  order by created_at desc, id desc fetch first 21 rows only""",
}
INSERT_1000 = """insert into footprints (latitude, longitude, version, created_at, location_captured_at, updated_at,
  user_id, visited_at, location_policy_version, category_codes)
select 37.5 + g * 0.00001, 127.0, 1, now(), now(), now(), 1 + g % 50, now(), 'location-v1', '["CAFE"]'::jsonb
from generate_series(1, 1000) g"""


def psql(sql, extra=()):
    result = subprocess.run(PSQL + list(extra), input=sql.encode("utf-8"), capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace"))
    return result.stdout.decode("utf-8")


def explain(sql, rollback=False):
    statement = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql};"
    out = psql(f"BEGIN; {statement} ROLLBACK;" if rollback else statement)
    return json.loads(out)[0]


def walk(node):
    yield node
    for child in node.get("Plans", []):
        yield from walk(child)


def measure(sql, runs, rollback=False):
    explain(sql, rollback)
    plans = [explain(sql, rollback) for _ in range(runs)]
    times = [plan["Execution Time"] for plan in plans]
    last = plans[-1]
    scans = [node for node in walk(last["Plan"]) if "Scan" in node["Node Type"]]
    return {
        "median_ms": round(statistics.median(times), 3), "min_ms": round(min(times), 3), "max_ms": round(max(times), 3),
        "scan": ", ".join(node["Node Type"] + ("(" + node["Index Name"] + ")" if "Index Name" in node else "")
                          for node in scans),
        "rows_out": last["Plan"]["Actual Rows"],
        "rows_scanned": sum((node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node.get("Actual Loops", 1)
                            for node in scans),
        "buffers": last["Plan"].get("Shared Hit Blocks", 0) + last["Plan"].get("Shared Read Blocks", 0),
    }


def drop_extra_indexes():
    """앞선 측정에서 넣은 후보 인덱스를 지운다. 기본 키·유일 제약 인덱스는 남긴다."""
    names = psql("""select i.indexrelid::regclass from pg_index i join pg_class t on t.oid = i.indrelid
                    where t.relname in ('footprints','ar_tips') and not i.indisprimary and not i.indisunique""")
    for name in names.split():
        psql(f"drop index {name};")


def sizes():
    out = psql("""select relname || '=' || pg_relation_size(oid) || '/' || pg_indexes_size(oid)
                  from pg_class where relname in ('footprints','ar_tips') order by relname""")
    return {name: dict(zip(("table_bytes", "index_bytes"), map(int, value.split("/"))))
            for name, value in (line.split("=") for line in out.split())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--scales", default="1000,10000,100000,1000000")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--after-seed", help="시드 뒤에 실행할 SQL 파일(인덱스 후보 등)")
    args = parser.parse_args()
    seed = (ROOT / "fixtures" / "seed-footprints-tips.sql").read_text(encoding="utf-8")
    report = {"label": args.label, "runs": args.runs, "scales": {}}
    for scale in (int(value) for value in args.scales.split(",")):
        drop_extra_indexes()
        psql(seed, ["-v", f"n={scale}"])
        if args.after_seed:
            psql(pathlib.Path(args.after_seed).read_text(encoding="utf-8"))
        # 인덱스만 읽는 계획(Index Only Scan)도 공정하게 잡히도록 통계와 가시성 정보를 모두 갱신한다.
        psql("vacuum analyze footprints; vacuum analyze ar_tips;")
        entry = {"sizes": sizes(), "queries": {name: measure(sql, args.runs) for name, sql in QUERIES.items()}}
        entry["queries"]["INSERT 발자취 1,000행"] = measure(INSERT_1000, args.runs, rollback=True)
        report["scales"][scale] = entry
        print(f"\n== {scale:,}건 (발자취 테이블 {entry['sizes']['footprints']['table_bytes'] // 1024:,}KB, "
              f"인덱스 {entry['sizes']['footprints']['index_bytes'] // 1024:,}KB)")
        for name, row in entry["queries"].items():
            print(f"{row['median_ms']:>10.3f}ms [{row['min_ms']:.3f}~{row['max_ms']:.3f}] "
                  f"읽은 행 {row['rows_scanned']:>9,} 버퍼 {row['buffers']:>7,} {row['scan']:<42} {name}")
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / f"sql-{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                             encoding="utf-8")
    plans = "\n\n".join(f"-- {name}\n" + psql(f"EXPLAIN (ANALYZE, BUFFERS) {sql};") for name, sql in QUERIES.items())
    (ROOT / "results" / f"sql-{args.label}-plans.txt").write_text(plans, encoding="utf-8")


if __name__ == "__main__":
    main()
