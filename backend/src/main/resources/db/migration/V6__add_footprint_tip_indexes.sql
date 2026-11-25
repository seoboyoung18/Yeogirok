-- V6: 발자취·AR 팁 조회 인덱스.
--
-- 두 테이블에는 기본 키 말고 인덱스가 없어 아래 조회가 모두 테이블 전체를 읽었다(Seq Scan). 데이터 1천~100만 건에서
-- 실행 계획과 시간을 재고 후보 네 가지를 비교해 고른 것이다(docs/engineering/05-database-optimization.md).
-- 모두 조건에 맞는 행만 담는 부분 인덱스다. 삭제한 발자취·공개가 아닌 팁은 이 조회들이 읽지 않는다.

-- 내 발자취: 같은 날 작성 제한 확인(API-041, 회원 행을 잠근 채 실행)과 내 목록(API-042).
-- 한 사람의 발자취는 수십 건이라 user_id 하나로 충분했다. (user_id, created_at)·(user_id, visited_at) 복합 인덱스는
-- 크기가 2배인데 줄어드는 시간이 0.1ms 아래였다.
create index ix_footprints_user_active on footprints (user_id) where deleted_at is null;

-- 방문 분포 격자(API-046): 화면 영역의 발자취를 칸·카테고리별로 센다. 쿼리가 쓰는 열을 모두 담아 테이블을 읽지 않게 한다
-- (Index Only Scan). 좌표만 담은 인덱스는 넓은 화면(전체의 5~9%)에서 전체를 읽는 것보다 느렸다.
-- visited_at 은 from·to 날짜 조건용이다. 전국 보기(도 단위)는 전체를 세는 쿼리라 이 인덱스를 쓰지 않는다.
create index ix_footprints_geo_cover on footprints (latitude, longitude)
    include (category_codes, user_id, visited_at) where deleted_at is null;

-- 주변 팁 탐색(API-047): 공개 팁 중 위경도 사각형 안.
create index ix_ar_tips_public_geo on ar_tips (latitude, longitude) where status = 'PUBLISHED' and deleted_at is null;

-- 내 팁 목록(API-053).
create index ix_ar_tips_author_active on ar_tips (author_id) where deleted_at is null;
