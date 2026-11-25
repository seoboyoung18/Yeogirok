# 00. 프로젝트 사전 분석

- 분석일: 2026-11-25
- 기준: 개인 리포지토리 `main` `069941b` (팀 `main` + `infra/dev`·`ai/dev`·`frontend/dev`·`backend/dev` 병합)
- 방법: 코드·설정·테스트를 읽고 집계했다. 이 문서의 "예상"은 코드만 보고 판단한 것이고 재현·측정한 것이 아니다. 측정 결과는 `03-baseline-report.md`부터 적는다.

## 1. 구조와 기술 스택

| 항목 | 내용 | 근거 |
|---|---|---|
| 언어·프레임워크 | Java 21, Spring Boot 4.0.8, JPA, Spring Security + JWT | `backend/build.gradle` |
| DB | PostgreSQL 16 (`postgis/postgis:16-3.4` 이미지). 공간 컬럼·GiST 인덱스는 쓰지 않고 좌표는 `numeric(10,7)` | `infra/docker-compose.yml`, `V1__init.sql` |
| 마이그레이션 | Flyway V1~V5, local·prod `ddl-auto: validate` | `backend/src/main/resources/db/migration` |
| Redis | 7.4. 멱등키, 업로드 세션, 팁 게시 상태, 초안, 잠금 | `global/idempotency`, `global/lock/RedisLock.java` |
| 파일 | AWS SDK v2 S3 (Presigned URL) | `domain/file` |
| 규모 | 운영 589개 파일 34,509줄, 테스트 235개 파일 36,810줄, 컨트롤러 27개, 엔드포인트 83개, 도메인 18개 | 집계 |
| 실행 구조 | 단일 인스턴스. `@Async`(스프링 기본 실행기)와 `@Scheduled` 9개. 메시지 큐 없음 | `global/config/AsyncConfig.java` |
| 외부 연동 | `RestClient` 9개(카카오 로컬·모빌리티, TourAPI, OCR, OAuth). 제한 시간 설정 있음, 재시도·서킷 브레이커 없음 | `application.yaml` |
| 커넥션 풀·스레드 | 설정 없음 → HikariCP 최대 10, Tomcat 최대 200(기본값) | 설정 파일 전체 검색 |
| 지표 | 분석 시점에는 Actuator·Micrometer 없음(이번 작업에서 추가) | `backend/build.gradle` |

## 2. 기존 테스트

- 전체 2,120개: 통과 2,110, 건너뜀 10, 실패 0 (2026-11-25, H2 + Redis 컨테이너, 힙 2GB).
- 도메인별 케이스 수: record 330, tip 168, course 146, room 139, schedule 134, file 104, footprint 78, global 74, chat 72, statistics 61.
- 동시성 도구(`CountDownLatch` 등)를 쓰는 테스트 클래스 18개.
- Redis 는 `TestRedis`(Testcontainers `redis:7.4-alpine`)로 실제 Redis 를 쓴다.
- PostgreSQL 컨테이너(`postgres:16-alpine`)를 쓰는 클래스는 4개(`FlywayMigrationTest`, `PostgresChatRoomLockTest`, `PostgresPreservedVisitCountsTest`, `WithdrawnFootprintsPostgresTest`). 나머지는 H2 PostgreSQL 모드 + `ddl-auto: update`.
- 부하·성능 테스트는 없었다.

## 3. 포트폴리오에 적은 내용과 코드 대조

| 주장 | 판정 | 근거 |
|---|---|---|
| 회원 행 `FOR UPDATE` 사용자 잠금 | 코드 확인 | `global/lock/UserLock.java`, `UserRepository.findByIdForUpdate` |
| PostgreSQL advisory lock | 발자취에는 현재 없음(과거 이력). 지금은 채팅만 | `domain/chat/service/PostgresChatRoomLock.java` |
| 외부 API 를 트랜잭션·잠금 밖으로 | 코드 확인 | `FootprintCreateService.create()` |
| 동시 5건 중 1건 저장·4건 거부 | 테스트는 있으나 H2·CI 에서는 건너뜀. 서비스 예외(F003)를 확인하며 HTTP 409 를 확인하지는 않음 | `FootprintCreateServiceTest.java:290` `assumeTrue(isPostgres())` |
| 잠금을 빼면 테스트 실패 | 자동화된 근거 없음(수동 확인 기록) | - |
| Redis SET NX, 요청 해시, 키 정렬, 처리 중·완료 | 코드 확인 | `global/idempotency/IdempotencyService.java` |
| 컨트롤러 12개·28곳 적용 | 일치 | `execute(` 호출 28건, 12개 파일 |
| 비동기 게시, 종료 대기 30초, 5분 복구 | 코드·설정 확인 | `TipPublisher.java`, `StuckTipRecoveryJob.java`, `application.yaml` |
| 최신 작업 번호 검증 | 게시 경로 확인. 수정 경로의 Lua 는 아직 읽지 않음 | `TipPublisher.link()` |
| 공개 코스 918개, S3 ETag 조건부 복사 | 이번 단계에서 확인 안 함 | 918은 운영 데이터 |

## 4. 인덱스 현황

인덱스 12개(V1 10개, V3·V4 1개씩)는 `actual_visits`, `room_messages`, `room_places`, `schedule_items`, `user_push_tokens`, `visit_images`, `recommendation_logs`, `regions` 용이다. `footprints`, `ar_tips`, `travel_records`, `users` 에는 기본 키(일부 유일 제약) 외 인덱스가 없다.

## 5. 예상 문제 (측정 전)

| 구분 | 내용 | 코드 근거 | 다룰 테스트 |
|---|---|---|---|
| 성능 | 방문 분포(046)가 `footprints` 전체를 읽고 `GROUP BY` | `statistics/VisitStatisticsQuery.java` | TEST 02·01 |
| 성능 | 주변 팁(047)이 `status + 위도·경도` 범위 조회인데 인덱스 없음 | `tip/repository/ArTipRepository.findPublicInBox` | TEST 02·01 |
| 성능 | 발자취 저장(041)의 같은 날 조회가 회원 행 잠금 안에서 인덱스 없이 실행 | `FootprintRepository.findActiveCreatedBetween` | TEST 02·03 |
| 성능 | 코스 검색(014)이 후보 전부를 읽어 메모리에서 필터·정렬·커서 | `course/service/CourseSearchService.java:44-52` | TEST 02·01 |
| 성능 | 커넥션 풀 10·스레드 200 기본값 | 설정 없음 | TEST 01·03 |
| 정합성 | DB 커밋 뒤 Redis 완료 기록 실패 시 30초 뒤 같은 키로 처리가 다시 실행됨 | `IdempotencyService.runAndRecord` | TEST 04 |
| 정합성 | 처리가 30초를 넘으면 처리 중 표시가 만료되어 두 번째 요청이 동시에 실행 | 같은 파일 | TEST 04 |
| 정합성 | `users (provider, provider_subject)` 유일 제약 없음(팀원 담당 코드) | `V1__init.sql` | 보고만 |
| 복구 | 앱 종료 대기 30초, 컨테이너 종료 유예는 Docker 기본 10초 | `application.yaml`, `infra/docker-compose.yml` backend | TEST 05 |
| 복구 | backend 컨테이너에 헬스체크·메모리 제한·JVM 옵션 없음 | `infra/docker-compose.yml:64-` | TEST 05·10 |
| 복구 | 복구 작업은 `REGISTERING` 팁만 다룸. 실행 중 끊긴 수정 작업은 대상 아님 | `tip/publish/StuckTipRecoveryJob.java` | TEST 05 |

## 6. 테스트 순서

TEST 02(쿼리·인덱스) → TEST 01(k6 부하) → TEST 03(발자취 동시성) → TEST 04(멱등 장애) → TEST 05(비동기 복구). P1 항목(06~10)은 그 뒤.
