package com.ssafy.yeogirok.global.config;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.time.Duration;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.boot.web.server.context.WebServerGracefulShutdownLifecycle;
import org.springframework.core.task.TaskRejectedException;
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

class AsyncTaskDrainTest {

	private final ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();

	@AfterEach
	void shutdown() {
		executor.shutdown();
	}

	@Test
	void 멈출_때_실행_중인_작업과_대기_중인_작업을_모두_끝낸다() {
		executor.setCorePoolSize(2);
		executor.setMaxPoolSize(2);
		executor.initialize();
		AtomicInteger done = new AtomicInteger();
		for (int i = 0; i < 20; i++) {
			executor.execute(() -> {
				sleep(20);
				done.incrementAndGet();
			});
		}
		AsyncTaskDrain drain = new AsyncTaskDrain(executor, Duration.ofSeconds(10));
		drain.start();

		drain.stop();

		assertThat(done).hasValue(20);
		assertThat(drain.isRunning()).isFalse();
		assertThatThrownBy(() -> executor.execute(() -> { })).isInstanceOf(TaskRejectedException.class);
	}

	@Test
	void 대기_기간이_지나면_기다리지_않고_돌아온다() {
		executor.setCorePoolSize(1);
		executor.initialize();
		executor.execute(() -> sleep(2_000));
		AsyncTaskDrain drain = new AsyncTaskDrain(executor, Duration.ofMillis(100));
		drain.start();

		long started = System.nanoTime();
		drain.stop();

		assertThat(Duration.ofNanos(System.nanoTime() - started)).isLessThan(Duration.ofSeconds(1));
	}

	@Test
	void 웹_서버보다_늦게_Redis_연결보다_먼저_멈춘다() {
		// 종료는 단계 값이 큰 것부터 한다. 웹 서버 시작·종료 단계는 graceful 단계보다 1024 작다.
		int webServer = WebServerGracefulShutdownLifecycle.SMART_LIFECYCLE_PHASE - 1024;
		int redis = new LettuceConnectionFactory().getPhase();

		assertThat(AsyncTaskDrain.PHASE).isLessThan(webServer).isGreaterThan(redis);
	}

	private static void sleep(long millis) {
		try {
			Thread.sleep(millis);
		} catch (InterruptedException e) {
			Thread.currentThread().interrupt();
		}
	}
}
