-- 발자취·AR 팁 시드(측정 전용 DB 에서만). psql -v n=100000 -f seed-footprints-tips.sql
-- 분포: 60% 는 도시 12곳 주변(중심에서 대략 ±0.15도 안에 몰림), 40% 는 한국 범위에 고르게.
-- 회원 수 = n/20(최소 100), 방문 시각은 최근 365일, 발자취 5% 는 삭제 상태, 팁 90% 는 PUBLISHED(나머지는 FAILED·DELETED).
-- REGISTERING 은 만들지 않는다: 앱이 떠 있으면 복구 작업(StuckTipRecoveryJob)이 FAILED 로 바꿔 데이터가 달라진다.
-- 난수 씨앗을 고정해 같은 n 이면 같은 데이터가 나온다.
\set ON_ERROR_STOP on
select setseed(0.42);
truncate footprints, ar_tips, ar_tip_images, ar_tip_reactions, users restart identity;

insert into users (created_at, updated_at, nickname, provider, provider_subject, status)
select now(), now(), 'perf' || g, 'KAKAO', 'perf:' || g, 'ACTIVE'
from generate_series(1, greatest(100, :n / 20)) g;

create temp table city (idx int, lat numeric, lng numeric, code text);
insert into city values
 (0,37.5665,126.9780,'11'),(1,35.1796,129.0756,'26'),(2,35.8714,128.6014,'27'),(3,37.4563,126.7052,'28'),
 (4,35.1595,126.8526,'29'),(5,36.3504,127.3845,'30'),(6,35.5384,129.3114,'31'),(7,37.2636,127.0286,'41'),
 (8,37.8813,127.7298,'51'),(9,35.8242,127.1480,'52'),(10,33.4996,126.5312,'50'),(11,37.7519,128.8761,'51');
create temp table cat (idx int, code text);
insert into cat values (0,'NATURE'),(1,'CULTURE'),(2,'SIGHT'),(3,'FOOD'),(4,'CAFE'),(5,'ACTIVITY'),(6,'SHOPPING'),(7,'EVENT');

create temp table pts as
select g,
       (random() < 0.6) as clustered,
       floor(random() * 12)::int as city_idx,
       (random() + random() + random() - 1.5) * 0.1 as dlat,
       (random() + random() + random() - 1.5) * 0.1 as dlng,
       33.2 + random() * 5.3 as ulat, 126.0 + random() * 3.5 as ulng,
       1 + floor(random() * greatest(100, :n / 20))::bigint as user_id,
       now() - (random() * 365) * interval '1 day' as at,
       floor(random() * 8)::int as c1, floor(random() * 8)::int as c2, floor(random() * 8)::int as c3,
       random() as r1, random() as r2
from generate_series(1, :n) g;

insert into footprints (latitude, longitude, version, created_at, deleted_at, location_captured_at, updated_at, user_id,
                        visited_at, legal_area_code, location_policy_version, address, body, location_name, category_codes)
select round((case when p.clustered then c.lat + p.dlat else p.ulat end)::numeric, 7),
       round((case when p.clustered then c.lng + p.dlng else p.ulng end)::numeric, 7),
       1, p.at, case when p.r1 < 0.05 then p.at + interval '1 hour' end, p.at, p.at, p.user_id, p.at,
       case when p.clustered and p.r2 < 0.8 then c.code || '11010100' end, 'location-v1',
       '측정용 주소 ' || p.g, case when p.r2 < 0.5 then '측정용 방명록 ' || p.g end, '측정용 위치',
       (select jsonb_agg(code order by code) from (select distinct code from cat
          where idx = p.c1 or (p.r1 > 0.5 and idx = p.c2) or (p.r1 > 0.8 and idx = p.c3)) s)
from pts p join city c on c.idx = p.city_idx;

insert into ar_tips (accuracy_meters, latitude, longitude, version, author_id, created_at, deleted_at, location_captured_at,
                     published_at, spatial_expires_at, updated_at, category_code, status, location_policy_version,
                     spatial_provider, body, spatial_reference_id, placement)
select 5, round((case when p.clustered then c.lat + p.dlng else p.ulat end)::numeric, 7),
       round((case when p.clustered then c.lng + p.dlat else p.ulng end)::numeric, 7),
       1, p.user_id, p.at, case when p.r1 < 0.03 then p.at end, p.at, p.at, null, p.at,
       (array['PHOTO','RESTAURANT','CAFE','TIP','TMI'])[1 + p.c1 % 5],
       case when p.r1 < 0.03 then 'DELETED' when p.r1 < 0.10 then 'FAILED' else 'PUBLISHED' end,
       'location-v1', 'ARCORE_CLOUD_ANCHOR', '측정용 팁 ' || p.g, 'anchor-' || p.g, '{}'::jsonb
from pts p join city c on c.idx = p.city_idx;

analyze footprints; analyze ar_tips; analyze users;
select (select count(*) from footprints) as footprints, (select count(*) from ar_tips) as tips, (select count(*) from users) as users;
