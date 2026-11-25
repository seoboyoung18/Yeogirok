-- 후보 A: 컬럼 하나짜리(또는 좌표 둘) 인덱스. 조건에 맞는 행만 담는 부분 인덱스.
create index ix_fp_user on footprints (user_id) where deleted_at is null;
create index ix_fp_geo on footprints (latitude, longitude) where deleted_at is null;
create index ix_tip_geo on ar_tips (latitude, longitude) where status = 'PUBLISHED' and deleted_at is null;
create index ix_tip_author on ar_tips (author_id) where deleted_at is null;
