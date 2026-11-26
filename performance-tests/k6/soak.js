// 장시간 안정성(TEST 10)용. 조회 4종과 발자취 저장을 정해진 속도(열린 모델)로 섞어 보낸다.
// 환경 변수: BASE_URL, JWT_SECRET(측정용, Base64), DURATION(초), USERS(시드 회원 수), SCALE(유입률 배수, 기본 1)
import crypto from "k6/crypto";
import encoding from "k6/encoding";
import http from "k6/http";
import { check } from "k6";

const BASE = __ENV.BASE_URL;
const DURATION = `${__ENV.DURATION || 600}s`;
const USERS = Number(__ENV.USERS || 5000);
const SCALE = Number(__ENV.SCALE || 1);
// 초당 요청 수. 합계 40.
const RATES = { statsCity: 10, tipsSearch: 10, myFootprints: 8, myTips: 8, saveFootprint: 4 };

export const options = {
  scenarios: Object.fromEntries(Object.entries(RATES).map(([name, rate]) => [name, {
    executor: "constant-arrival-rate", exec: name, rate: Math.round(rate * SCALE), timeUnit: "1s", duration: DURATION,
    preAllocatedVUs: 20, maxVUs: 200, gracefulStop: "10s",
  }])),
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)", "max"],
  thresholds: Object.fromEntries(Object.keys(RATES).flatMap((name) => [
    [`http_req_duration{scenario:${name}}`, ["max>=0"]],
    [`http_req_failed{scenario:${name}}`, ["rate>=0"]],
    [`http_reqs{scenario:${name}}`, ["count>=0"]],
  ])),
};

function b64url(value) {
  return encoding.b64encode(value, "rawurl");
}

function headers() {
  const userId = 1 + Math.floor(Math.random() * USERS);
  const now = Math.floor(Date.now() / 1000);
  const head = b64url(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const claims = b64url(JSON.stringify({
    iss: "yeogirok", sub: String(userId), iat: now, exp: now + 1800, jti: `${userId}-${now}-${Math.random()}`, role: "USER",
  }));
  const signature = crypto.hmac("sha256", encoding.b64decode(__ENV.JWT_SECRET, "std"), `${head}.${claims}`, "base64rawurl");
  return { Authorization: `Bearer ${head}.${claims}.${signature}`, "Content-Type": "application/json" };
}

function capturedAt() {
  const seoul = new Date(Date.now() + 9 * 3600 * 1000).toISOString();
  return `${seoul.slice(0, 10)} ${seoul.slice(11, 19)}+09`;
}

function near(center) {
  return center + (Math.random() - 0.5) * 0.2;
}

function expect(response, status) {
  check(response, { [status]: (r) => r.status === status });
}

export function statsCity() {
  const west = near(126.85), south = near(37.45);
  expect(http.get(`${BASE}/api/v1/visit-statistics?bbox=${west.toFixed(4)},${south.toFixed(4)},${(west + 0.3).toFixed(4)},${(south + 0.2).toFixed(4)}&zoom=13`, { headers: headers() }), 200);
}

export function tipsSearch() {
  expect(http.post(`${BASE}/api/v1/tips/search`, JSON.stringify({
    location: { latitude: Number(near(37.5665).toFixed(7)), longitude: Number(near(126.978).toFixed(7)), accuracyMeters: 5, capturedAt: capturedAt() },
  }), { headers: headers() }), 200);
}

export function myFootprints() {
  expect(http.get(`${BASE}/api/v1/users/me/footprints`, { headers: headers() }), 200);
}

export function myTips() {
  expect(http.get(`${BASE}/api/v1/users/me/tips`, { headers: headers() }), 200);
}

// 같은 회원이 같은 날 500m 안에 또 쓰면 409(F003)라, 넓은 범위(위도 34~38, 경도 126.5~129)에 흩어 쓴다.
export function saveFootprint() {
  const response = http.post(`${BASE}/api/v1/footprints`, JSON.stringify({
    location: { latitude: Number((34 + Math.random() * 4).toFixed(7)), longitude: Number((126.5 + Math.random() * 2.5).toFixed(7)), accuracyMeters: 5, capturedAt: capturedAt() },
    categoryCodes: ["CAFE"], body: "장시간 측정",
  }), { headers: { ...headers(), "Idempotency-Key": `soak-${Date.now()}-${Math.random()}` }, responseCallback: http.expectedStatuses(201, 409) });
  expect(response, 201);
}
