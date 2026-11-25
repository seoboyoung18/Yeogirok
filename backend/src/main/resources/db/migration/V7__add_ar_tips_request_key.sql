-- V7: AR 팁 등록 요청 키.
--
-- 팁 등록(API-049)의 중복 방지는 Redis 멱등 기록에만 기대고 있었다. 그 기록이 없어지면(Redis 가 키를 잃음, 완료 기록 저장 실패,
-- 처리가 표시 수명 30초를 넘김, 보관 24시간 경과) 같은 Idempotency-Key 로 다시 온 요청이 팁을 하나 더 만들었다
-- (docs/engineering/07-idempotency-test.md 문제 A: 같은 키로 2건 저장 재현).
-- 요청 키를 팁에 남기고 (작성자, 요청 키) 유일 제약을 걸어 DB 가 막게 한다. 엔티티는 domain/tip/entity/ArTip.
--
-- 이미 있는 팁은 키가 없다(null). PostgreSQL 유일 제약은 null 끼리 겹친다고 보지 않아 그대로 둘 수 있다.
alter table ar_tips add column request_key varchar(255);

alter table ar_tips add constraint uq_ar_tips_author_request unique (author_id, request_key);
