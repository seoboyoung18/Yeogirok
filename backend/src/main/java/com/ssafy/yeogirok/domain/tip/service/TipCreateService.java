package com.ssafy.yeogirok.domain.tip.service;

import org.springframework.dao.DataIntegrityViolationException;
import java.util.Optional;
import java.time.Clock;
import java.time.Instant;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;

import org.springframework.context.ApplicationEventPublisher;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;

import com.ssafy.yeogirok.domain.tip.dto.TipCreateRequest.TipCreateCommand;
import com.ssafy.yeogirok.domain.tip.dto.TipCreateResponse;
import com.ssafy.yeogirok.domain.tip.entity.ArTip;
import com.ssafy.yeogirok.domain.tip.entity.ArTipCategory;
import com.ssafy.yeogirok.domain.tip.entity.SpatialReference;
import com.ssafy.yeogirok.domain.tip.entity.TipBody;
import com.ssafy.yeogirok.domain.tip.entity.TipStatus;
import com.ssafy.yeogirok.domain.tip.publish.TipPublishRequested;
import com.ssafy.yeogirok.domain.tip.publish.TipPublishState;
import com.ssafy.yeogirok.domain.tip.publish.TipPublishStore;
import com.ssafy.yeogirok.domain.tip.repository.ArTipRepository;
import com.ssafy.yeogirok.global.location.LocationEvidencePolicy;

/**
 * API-049 팁 등록. 멱등키는 컨트롤러가 이 밖에서 처리한다. 값 검사를 모두 마친 뒤 REGISTERING 으로 저장하고 작업 상태를 남긴다.
 * 사진 연결·PUBLISHED 전환은 커밋 뒤 {@link com.ssafy.yeogirok.domain.tip.publish.TipPublisher} 가 한다.
 * 팁 좌표는 요청 위치(기기 GPS)이고 생성 때 배치 거리는 보지 않는다(specs/ar-tip-api.md 5절 7·8).
 */
@Service
public class TipCreateService {

	private final ArTipRepository arTipRepository;
	private final MyTipFinder myTipFinder;
	private final TipPhotoUploads tipPhotoUploads;
	private final TipPhotoClaims tipPhotoClaims;
	private final TipPublishStore publishStore;
	private final TipPlacements tipPlacements;
	private final LocationEvidencePolicy locationEvidencePolicy;
	private final ApplicationEventPublisher events;
	private final TransactionTemplate transactionTemplate;
	private final Clock clock;

	public TipCreateService(ArTipRepository arTipRepository, MyTipFinder myTipFinder,
		TipPhotoUploads tipPhotoUploads, TipPhotoClaims tipPhotoClaims, TipPublishStore publishStore,
		TipPlacements tipPlacements, LocationEvidencePolicy locationEvidencePolicy, ApplicationEventPublisher events,
		PlatformTransactionManager transactionManager, Clock clock) {
		this.arTipRepository = arTipRepository;
		this.myTipFinder = myTipFinder;
		this.tipPhotoUploads = tipPhotoUploads;
		this.tipPhotoClaims = tipPhotoClaims;
		this.publishStore = publishStore;
		this.tipPlacements = tipPlacements;
		this.locationEvidencePolicy = locationEvidencePolicy;
		this.events = events;
		this.transactionTemplate = new TransactionTemplate(transactionManager);
		this.clock = clock;
	}

	/** request_key 열 길이. 이보다 긴 멱등키는 DB 에 남기지 않는다(Redis 멱등 처리만 받는다). */
	private static final int REQUEST_KEY_MAX_LENGTH = 255;

	/**
	 * @param idempotencyKey 등록 요청의 Idempotency-Key. Redis 멱등 기록이 없어 여기까지 다시 들어온 같은 요청은
	 *                       새로 만들지 않고 처음 만든 팁을 돌려준다(docs/engineering/07-idempotency-test.md 문제 A)
	 */
	public TipCreateResponse create(Long userId, String idempotencyKey, TipCreateCommand command) {
		String requestKey = idempotencyKey == null || idempotencyKey.length() > REQUEST_KEY_MAX_LENGTH
			? null : idempotencyKey;
		Optional<TipCreateResponse> existing = findCreated(userId, requestKey);
		if (existing.isPresent()) {
			return existing.get();
		}
		Instant now = clock.instant();
		locationEvidencePolicy.verify(command.location(), now);
		ArTipCategory category = ArTipCategory.fromCode(command.category());
		String body = TipBody.normalize(command.body());
		SpatialReference spatialReference = SpatialReference.read(command.spatialReference(), now);
		Map<String, Object> placement = tipPlacements.toMap(command.placement());
		String imagePath = tipPhotoUploads.confirmedPath(userId, null, command.uploadIds());

		String jobId = UUID.randomUUID().toString();
		AtomicReference<Long> claimedBy = new AtomicReference<>();
		try {
			Long tipId = transactionTemplate.execute(status -> {
				ArTip tip = arTipRepository.saveAndFlush(ArTip.register(userId, requestKey, category, body, command.location(),
					locationEvidencePolicy.policyVersion(), spatialReference, placement, now));
				if (tipPhotoClaims.claim(tip.getId(), command.uploadIds())) {
					claimedBy.set(tip.getId());
				}
				// Redis 저장이 실패하면 팁도 남기지 않는다. 롤백돼 남은 작업 상태는 050 이 팁을 못 찾아 쓰이지 않는다.
				publishStore.save(tip.getId(), TipPublishState.queued(jobId, imagePath));
				events.publishEvent(new TipPublishRequested(tip.getId(), jobId, imagePath));
				return tip.getId();
			});
			return new TipCreateResponse(tipId, jobId, TipStatus.REGISTERING);
		} catch (DataIntegrityViolationException e) {
			// 같은 요청 키의 두 요청이 겹쳐 유일 제약에 걸린 경우 먼저 들어간 팁을 돌려준다. 다른 제약 위반이면 그대로 던진다.
			return findCreated(userId, requestKey).orElseThrow(() -> e);
		} catch (RuntimeException e) {
			if (claimedBy.get() != null) {
				tipPhotoClaims.release(claimedBy.get(), command.uploadIds());
			}
			throw e;
		}
	}

	private Optional<TipCreateResponse> findCreated(Long userId, String requestKey) {
		if (requestKey == null) {
			return Optional.empty();
		}
		return arTipRepository.findByAuthorIdAndRequestKey(userId, requestKey)
			.map(tip -> getCreated(userId, tip.getId()));
	}

	/** 같은 멱등키로 다시 보낸 요청. 그사이 삭제했으면 T012. */
	@Transactional(readOnly = true)
	public TipCreateResponse getCreated(Long userId, Long tipId) {
		ArTip tip = myTipFinder.getOwn(tipId, userId);
		String jobId = publishStore.find(tipId).map(TipPublishState::jobId).orElse(null);
		return new TipCreateResponse(tipId, jobId, tip.getStatus());
	}
}
