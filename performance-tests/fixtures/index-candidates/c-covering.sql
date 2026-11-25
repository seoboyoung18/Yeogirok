-- 후보 C: A 에서 발자취 좌표 인덱스만 "인덱스만 읽는" 형태로 바꾼 것. 046 이 쓰는 열을 모두 담아 테이블을 읽지 않게 한다.
create index ix_fp_user on footprints (user_id) where deleted_at is null;
create index ix_fp_geo_cover on footprints (latitude, longitude) include (category_codes, user_id) where deleted_at is null;
create index ix_tip_geo on ar_tips (latitude, longitude) where status = 'PUBLISHED' and deleted_at is null;
create index ix_tip_author on ar_tips (author_id) where deleted_at is null;
