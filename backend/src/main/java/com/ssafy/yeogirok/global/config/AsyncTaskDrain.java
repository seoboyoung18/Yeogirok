package com.ssafy.yeogirok.global.config;

import java.time.Duration;
import java.util.concurrent.TimeUnit;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.web.server.context.WebServerGracefulShutdownLifecycle;
import org.springframework.context.SmartLifecycle;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.stereotype.Component;

import lombok.extern.slf4j.Slf4j;

/**
 * 종료할 때 @Async 작업(팁 게시·수정 등)을 Redis·DB 가 닫히기 전에 끝낸다.
 *
 * <p>기본 실행기의 종료 대기(await-termination)는 빈을 없앨 때 돈다. 그때는 Redis 연결이 이미 멈췄고 DB 도 닫히는 중이라,
 * 대기 중이던 작업이 실행은 되지만 실패했다(docs/engineering/08-async-recovery-test.md: 정상 종료에서 저장된 팁의 67~84% 가 끊김).
 * 그래서 종료 단계 중 "웹 서버가 닫힌 뒤, Redis 연결이 멈추기 전"에 실행기를 닫고 남은 작업을 기다린다.
 */
@Slf4j
@Component
public class AsyncTaskDrain implements SmartLifecycle {

	/**
	 * 종료는 단계 값이 큰 것부터 한다. 웹 서버(요청을 더 받지 않고 진행 중인 요청을 끝냄)보다 작고
	 * Redis 연결(LettuceConnectionFactory, 0)보다 커야 한다. 웹 서버 시작·종료 단계는 graceful 단계보다 1024 작다.
	 */
	static final int PHASE = WebServerGracefulShutdownLifecycle.SMART_LIFECYCLE_PHASE - 4096;

	private final ThreadPoolTaskExecutor executor;
	private final Duration wait;
	private volatile boolean running;

	public AsyncTaskDrain(@Qualifier("applicationTaskExecutor") ThreadPoolTaskExecutor executor,
		@Value("${spring.task.execution.shutdown.await-termination-period}") Duration wait) {
		this.executor = executor;
		this.wait = wait;
	}

	@Override
	public void start() {
		this.running = true;
	}

	/** 새 작업을 받지 않고, 실행 중·대기 중인 작업이 끝날 때까지 기다린다. 기다리는 시간은 기본 실행기의 종료 대기 기간과 같다. */
	@Override
	public void stop() {
		this.running = false;
		executor.initiateShutdown();
		try {
			if (!executor.getThreadPoolExecutor().awaitTermination(wait.toMillis(), TimeUnit.MILLISECONDS)) {
				log.warn("종료 대기 {} 안에 끝나지 않은 비동기 작업이 있습니다. 게시 중이던 팁은 StuckTipRecoveryJob 이 정리합니다.", wait);
			}
		} catch (InterruptedException e) {
			Thread.currentThread().interrupt();
		}
	}

	@Override
	public boolean isRunning() {
		return running;
	}

	@Override
	public int getPhase() {
		return PHASE;
	}
}
