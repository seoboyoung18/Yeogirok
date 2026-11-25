# 성능 측정

운영 서버·운영 DB 에서는 실행하지 않는다. 이 PC 안의 측정 전용 컨테이너(DB 55432, Redis 56379)와 앱(18080)만 쓴다. 개발용 컨테이너(5432·6379)와는 포트·볼륨이 달라 서로 건드리지 않는다.

## 준비

```bash
# 1) 측정 전용 DB·Redis
docker compose -f performance-tests/docker-compose.perf.yml up -d --wait

# 2) performance-tests/.env.perf 만들기 (Git 에 올리지 않는다)
#    SPRING_PROFILES_ACTIVE=local,perf / DB_URL=jdbc:postgresql://localhost:55432/yeogirok_perf / DB_USERNAME=perf / DB_PASSWORD=perf
#    REDIS_HOST=localhost / REDIS_PORT=56379 / SERVER_PORT=18080
#    JWT_SECRET(Base64 32바이트 이상)·ROOM_INVITE_TOKEN_KEY(Base64 64바이트)는 이 PC 에서 새로 만든 측정용 값
#    ROOM_INVITE_URL_BASE·GOOGLE_WEB_CLIENT_ID·KAKAO_APP_KEY 는 아무 값

# 3) 앱 (Flyway 가 스키마를 만든다)
cd backend && ./gradlew bootJar
set -a && . ../performance-tests/.env.perf && set +a
java -Xms1g -Xmx1g -jar build/libs/yeogirok-0.0.1-SNAPSHOT.jar
```

외부 API 키(카카오·TourAPI·AWS)는 넣지 않는다. 키가 없으면 역지오코딩은 null 로 저장하고 넘어간다.

## 실행

| 무엇을 | 명령 | 결과 |
|---|---|---|
| API 한 번씩 확인 | `python performance-tests/scripts/smoke.py` | 응답 코드 출력 |
| 시드만 넣기 | `docker exec -i yeogirok-perf-db-1 psql -U perf -d yeogirok_perf -q -v n=100000 < performance-tests/fixtures/seed-footprints-tips.sql` | 발자취·팁 n건, 회원 n/20명 |
| SQL 규모별 측정 | `python performance-tests/scripts/bench_sql.py --label <이름>` | `results/sql-<이름>.json`, `-plans.txt` |
| k6 부하 측정 | `python performance-tests/scripts/run_load.py --label <이름> --users <시드 회원 수>` | `results/load-<이름>.json` |

- `bench_sql.py` 는 규모마다 시드를 다시 넣는다(기존 측정용 데이터는 지워진다).
- `run_load.py` 는 지금 들어 있는 데이터로 잰다. 먼저 원하는 규모로 시드를 넣는다.
- k6 는 도커 이미지 `grafana/k6:0.57.0` 으로 돈다. 따로 설치하지 않는다.

## 읽는 법

- 부하 발생기(k6)와 서버·DB 가 같은 PC 에서 돈다. 절대 수치가 아니라 같은 조건의 전후 비교로만 쓴다.
- VU 는 "쉬지 않고 요청을 보내는 반복 실행 수"다. 실제 사용자 수가 아니다.
- 측정 구간은 준비 10초 뒤 30초, 3회 반복이 기본이다.
- `results/raw*` 와 `.env.perf` 는 Git 에 올리지 않는다.

## 정리

```bash
docker compose -f performance-tests/docker-compose.perf.yml down -v
```
