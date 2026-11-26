package com.ssafy.yeogirok.domain.auth.token;

import java.time.Clock;
import java.util.Arrays;
import java.util.Base64;
import java.util.Objects;
import java.util.Set;
import java.util.stream.Collectors;

import javax.crypto.SecretKey;
import javax.crypto.spec.SecretKeySpec;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.oauth2.core.DelegatingOAuth2TokenValidator;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwtClaimNames;
import org.springframework.security.oauth2.jwt.JwtClaimValidator;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtIssuerValidator;
import org.springframework.security.oauth2.jwt.NimbusJwtDecoder;
import org.springframework.security.oauth2.jwt.NimbusJwtEncoder;
import org.springframework.util.StringUtils;

import com.ssafy.yeogirok.domain.user.domain.UserRole;

/**
 * 서비스 액세스 토큰(HS256 JWT) 발급·검증. 이 앱의 {@code JwtDecoder} 빈은 이것 하나뿐이다.
 */
@Configuration
public class JwtConfig {

	static final String ROLE_CLAIM = "role";
	/** 소셜 로그인(API-005) 시각. 재발급(API-006) 토큰에는 넣지 않는다 — 탈퇴 전 최근 재인증(A011) 판단에 쓴다. */
	public static final String AUTH_TIME_CLAIM = "auth_time";

	private static final Set<String> ALLOWED_ROLES = Arrays.stream(UserRole.values())
		.map(Enum::name)
		.collect(Collectors.toUnmodifiableSet());

	@Bean
	public SecretKey serviceJwtSecretKey(JwtProperties properties) {
		byte[] secretBytes;
		try {
			secretBytes = Base64.getDecoder().decode(properties.secret());
		} catch (IllegalArgumentException e) {
			throw new IllegalArgumentException("JWT_SECRET 은 Base64 형식이어야 합니다.", e);
		}

		if (secretBytes.length < 32) {
			throw new IllegalArgumentException("JWT_SECRET 은 디코드했을 때 32바이트 이상이어야 합니다.");
		}
		return new SecretKeySpec(secretBytes, "HmacSHA256");
	}

	@Bean
	public JwtEncoder serviceJwtEncoder(SecretKey serviceJwtSecretKey) {
		return NimbusJwtEncoder.withSecretKey(serviceJwtSecretKey).algorithm(MacAlgorithm.HS256).build();
	}

	@Bean
	public JwtDecoder serviceJwtDecoder(SecretKey serviceJwtSecretKey, JwtProperties properties, Clock clock,
		WithdrawnUserTokens withdrawnUserTokens) {
		NimbusJwtDecoder decoder = NimbusJwtDecoder.withSecretKey(serviceJwtSecretKey)
			.macAlgorithm(MacAlgorithm.HS256)
			.build();

		decoder.setJwtValidator(new DelegatingOAuth2TokenValidator<>(
			new AccessTokenExpiryValidator(clock),
			new JwtIssuerValidator(properties.issuer()),
			// 스프링의 만료 검사는 exp 가 없으면 통과시킨다. 만료 없는 토큰을 받지 않는다.
			new JwtClaimValidator<Object>(JwtClaimNames.EXP, Objects::nonNull),
			new JwtClaimValidator<String>(JwtClaimNames.SUB, StringUtils::hasText),
			// 변경 불가 Set 은 contains(null) 에서 예외를 던진다. role 이 없는 토큰이 500 이 되지 않게 먼저 거른다.
			new JwtClaimValidator<String>(ROLE_CLAIM, role -> role != null && ALLOWED_ROLES.contains(role)),
			withdrawnUserTokens));
		return decoder;
	}
}
