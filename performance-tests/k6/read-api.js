// 조회 API 한 종류에 일정한 수의 가상 사용자(VU)가 쉬지 않고 요청을 보낸다(닫힌 모델: 응답이 와야 다음 요청).
// VU 수는 "동시에 요청을 보내는 반복 실행 수"이지 실제 사용자 수가 아니다.
//
// 환경 변수: BASE_URL, JWT_SECRET(측정용, Base64), SCENARIO, VUS, WARMUP(초), DURATION(초), USERS(시드 회원 수)
// 준비 구간(phase:warmup)과 측정 구간(phase:main)을 태그로 나누고, 결과는 main 만 읽는다.
import crypto from "k6/crypto";
import encoding from "k6/encoding";
import http from "k6/http";
import { check } from "k6";

const BASE = __ENV.BASE_URL;
const VUS = Number(__ENV.VUS || 30);
const WARMUP = Number(__ENV.WARMUP || 10);
const DURATION = Number(__ENV.DURATION || 30);
const USERS = Number(__ENV.USERS || 100);

// RATE(초당 요청 수)를 주면 열린 모델로 바꾼다: 응답이 늦어도 정해진 속도로 요청을 보낸다.
// 전후 비교는 같은 유입률에서 지연 시간을 보는 쪽이 공정하다(닫힌 모델은 빨라질수록 요청 수도 늘어 조건이 달라진다).
const RATE = Number(__ENV.RATE || 0);

function scenario(seconds, startTime, phase) {
  const common = { duration: `${seconds}s`, startTime: `${startTime}s`, tags: { phase }, gracefulStop: "10s" };
  return RATE > 0
    ? { ...common, executor: "constant-arrival-rate", rate: RATE, timeUnit: "1s", preAllocatedVUs: VUS, maxVUs: VUS * 4 }
    : { ...common, executor: "constant-vus", vus: VUS };
}

export const options = {
  scenarios: { warmup: scenario(WARMUP, 0, "warmup"), main: scenario(DURATION, WARMUP + 5, "main") },
  summaryTrendStats: ["avg", "min", "med", "p(95)", "p(99)", "max"],
  // 값을 거르려는 기준이 아니라, 요약에 phase:main 만의 값을 따로 남기려고 둔다.
  thresholds: {
    "http_req_duration{phase:main}": ["max>=0"],
    "http_reqs{phase:main}": ["count>=0"],
    "http_req_failed{phase:main}": ["rate>=0"],
  },
};

function b64url(value) {
  return encoding.b64encode(value, "rawurl");
}

// 서버(JwtTokenService)와 같은 HS256 토큰. 가상 사용자마다 다른 시드 회원을 쓴다.
function token(userId) {
  const now = Math.floor(Date.now() / 1000);
  const head = b64url(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const claims = b64url(JSON.stringify({
    iss: "yeogirok", sub: String(userId), iat: now, exp: now + 1800, jti: `${userId}-${now}-${Math.random()}`, role: "USER",
  }));
  const signature = crypto.hmac("sha256", encoding.b64decode(__ENV.JWT_SECRET, "std"), `${head}.${claims}`, "base64rawurl");
  return `${head}.${claims}.${signature}`;
}

let headers;

function capturedAt() {
  const seoul = new Date(Date.now() + 9 * 3600 * 1000).toISOString();
  return `${seoul.slice(0, 10)} ${seoul.slice(11, 19)}+09`;
}

// 서울 시청 주변 ±0.1도 안의 임의 위치(시드가 몰려 있는 곳).
function near(center) {
  return center + (Math.random() - 0.5) * 0.2;
}

const SCENARIOS = {
  // 지도 화면 하나(0.3 x 0.2도)를 서울 안에서 조금씩 옮겨 가며 조회
  "stats-city": () => {
    const west = near(126.85), south = near(37.45);
    return http.get(`${BASE}/api/v1/visit-statistics?bbox=${west.toFixed(4)},${south.toFixed(4)},${(west + 0.3).toFixed(4)},${(south + 0.2).toFixed(4)}&zoom=13`, { headers });
  },
  "stats-nation": () => http.get(`${BASE}/api/v1/visit-statistics?bbox=124,33,132,39&zoom=7`, { headers }),
  "tips-search": () => http.post(`${BASE}/api/v1/tips/search`, JSON.stringify({
    location: { latitude: Number(near(37.5665).toFixed(7)), longitude: Number(near(126.978).toFixed(7)), accuracyMeters: 5, capturedAt: capturedAt() },
  }), { headers }),
  "my-footprints": () => http.get(`${BASE}/api/v1/users/me/footprints`, { headers }),
  "my-tips": () => http.get(`${BASE}/api/v1/users/me/tips`, { headers }),
};

export default function () {
  if (!headers) {
    headers = { Authorization: `Bearer ${token(1 + (__VU % USERS))}`, "Content-Type": "application/json" };
  }
  const response = SCENARIOS[__ENV.SCENARIO]();
  check(response, { "200": (r) => r.status === 200 });
}
