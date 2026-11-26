package com.ssafy.yeogirok.domain.file;

import java.net.URI;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.S3ClientBuilder;
import software.amazon.awssdk.services.s3.S3Configuration;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;

/**
 * S3 클라이언트. 자격 증명은 SDK 기본 순서(로컬 AWS_PROFILE, 서버 AWS_ACCESS_KEY_ID·AWS_SECRET_ACCESS_KEY)로 찾는다.
 * 자격 증명은 실제 S3 를 부를 때 찾으므로 키가 없어도 앱은 뜬다.
 *
 * <p>{@code yeogirok.file.endpoint} 는 측정·시험용이다. 값을 주면 AWS 대신 그 주소의 S3 호환 저장소(MinIO)를 쓴다
 * (performance-tests/README.md). 운영·로컬 개발에서는 비워 둔다. 비어 있으면 지금까지와 같이 AWS 를 쓴다.
 */
@Configuration
@EnableConfigurationProperties(FileProperties.class)
public class FileStorageConfig {

	private final String endpoint;

	public FileStorageConfig(@Value("${yeogirok.file.endpoint:}") String endpoint) {
		this.endpoint = endpoint;
	}

	@Bean
	public S3Client s3Client(FileProperties properties) {
		S3ClientBuilder builder = S3Client.builder().region(Region.of(properties.region()));
		if (!endpoint.isBlank()) {
			// S3 호환 저장소는 버킷 이름을 주소 앞에 붙이는 방식(bucket.host)을 쓰지 못해 경로 방식으로 부른다.
			builder.endpointOverride(URI.create(endpoint)).forcePathStyle(true);
		}
		return builder.build();
	}

	@Bean
	public S3Presigner s3Presigner(FileProperties properties) {
		S3Presigner.Builder builder = S3Presigner.builder().region(Region.of(properties.region()));
		if (!endpoint.isBlank()) {
			builder.endpointOverride(URI.create(endpoint))
				.serviceConfiguration(S3Configuration.builder().pathStyleAccessEnabled(true).build());
		}
		return builder.build();
	}
}
