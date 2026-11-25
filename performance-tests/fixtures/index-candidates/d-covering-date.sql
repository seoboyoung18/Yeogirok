-- 후보 D: C 의 좌표 덮개 인덱스에 visited_at 을 더 담은 것. 046 에 from·to 날짜 조건이 있어도 테이블을 읽지 않게 한다.
create index ix_fp_user on footprints (user_id) where deleted_at is null;
create index ix_fp_geo_cover on footprints (latitude, longitude) include (category_codes, user_id, visited_at) where deleted_at is null;
create index ix_tip_geo on ar_tips (latitude, longitude) where status = 'PUBLISHED' and deleted_at is null;
create index ix_tip_author on ar_tips (author_id) where deleted_at is null;
