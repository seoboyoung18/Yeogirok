-- 후보 B: 쿼리마다 조건·정렬 순서에 맞춘 복합 인덱스.
create index ix_fp_user_created on footprints (user_id, created_at) where deleted_at is null;
create index ix_fp_user_visited on footprints (user_id, visited_at desc, id desc) where deleted_at is null;
create index ix_fp_geo on footprints (latitude, longitude) where deleted_at is null;
create index ix_tip_status_geo on ar_tips (status, latitude, longitude);
create index ix_tip_author_created on ar_tips (author_id, created_at desc, id desc) where deleted_at is null;
