// E.9 load baseline (M13): signed LINE webhook events and staff pages through the edge (Caddy),
// the way LINE and staff reach a host. Run by `faoj load-test smoke|design` on staging or a
// rehearsal with synthetic subjects from `manage.py prepare_load_test`; never production.
//
// design: 2 events/s for 30 min, then a burst of 10 events/s for 5 min, while 100 staff
// sessions open the queue and a case every 5-15 s. Pass/fail follows E.9: webhook p95 ≤ 1 s
// and p99 ≤ 2 s, queue/case pages p95 ≤ 2 s, under 1% failed requests.
import { check, sleep } from "k6";
import crypto from "k6/crypto";
import http from "k6/http";

const BASE = __ENV.BASE_URL;
const SECRET = __ENV.LINE_SECRET;
const SUBJECTS = JSON.parse(open("/out/subjects.json"));
const PROFILES = {
  smoke: { steady: "1m", burst: "30s", staff: 10, think: [1, 3] },
  design: { steady: "30m", burst: "5m", staff: 100, think: [5, 15] },
};
const P = PROFILES[__ENV.PROFILE || "smoke"];
const UNKNOWN_USERS = 200; // people who are not registered yet: the registration flow
const TEXTS = [
  "สวัสดีครับ",
  "ขอเบิกมิเตอร์ 2 เครื่อง",
  "เรื่องของผมถึงไหนแล้ว",
  "ขอเบอร์ติดต่อหน่อย",
  "หม้อแปลงหน้าซอยมีเสียงดัง",
  "ผู้ทดสอบ โหลดระบบ",
  "1",
];
const POSTBACKS = ["m=new", "m=mine", "m=track"];

function seconds(text) {
  const n = parseInt(text, 10);
  return text.endsWith("m") ? n * 60 : n;
}

export const options = {
  insecureSkipTLSVerify: __ENV.INSECURE === "1", // rehearsal: Caddy's own CA
  noCookiesReset: true, // a staff session signs in once, like a person keeping the tab open
  hosts: __ENV.EDGE_HOST ? { [__ENV.EDGE_HOST]: "127.0.0.1" } : {}, // k6 shares Caddy's network
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)", "max"],
  scenarios: {
    webhook_steady: {
      executor: "constant-arrival-rate",
      exec: "webhook",
      rate: 2,
      timeUnit: "1s",
      duration: P.steady,
      preAllocatedVUs: 10,
      maxVUs: 50,
    },
    webhook_burst: {
      executor: "constant-arrival-rate",
      exec: "webhook",
      rate: 10,
      timeUnit: "1s",
      duration: P.burst,
      startTime: P.steady,
      preAllocatedVUs: 30,
      maxVUs: 150,
    },
    staff_pages: {
      executor: "constant-vus",
      exec: "staff",
      vus: P.staff,
      duration: `${seconds(P.steady) + seconds(P.burst)}s`,
    },
  },
  thresholds: {
    "http_req_duration{scenario:webhook_steady}": ["p(95)<1000", "p(99)<2000"],
    "http_req_duration{scenario:webhook_burst}": ["p(95)<1000", "p(99)<2000"],
    "http_req_duration{page:queue}": ["p(95)<2000"],
    "http_req_duration{page:case}": ["p(95)<2000"],
    http_req_failed: ["rate<0.01"],
    "checks{check:webhook stored}": ["rate>0.99"],
    "checks{check:queue page}": ["rate>0.99"],
    "checks{check:case page}": ["rate>0.99"],
  },
};

function hex(bytes) {
  return Array.from(new Uint8Array(crypto.randomBytes(bytes)))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function pick(list) {
  return list[Math.floor(Math.random() * list.length)];
}

function someone() {
  if (Math.random() < 0.7) return pick(SUBJECTS.registered);
  const n = Math.floor(Math.random() * UNKNOWN_USERS);
  return "U" + crypto.sha256(`faoj-load-unknown-${n}`, "hex").slice(0, 32);
}

export function webhook() {
  const now = Date.now();
  const event = {
    type: "message",
    mode: "active",
    timestamp: now,
    webhookEventId: `load${__VU}x${__ITER}x${now}x${hex(4)}`,
    deliveryContext: { isRedelivery: false },
    replyToken: hex(16),
    source: { type: "user", userId: someone() },
  };
  if (Math.random() < 0.3) {
    event.type = "postback";
    event.postback = { data: pick(POSTBACKS) };
  } else {
    event.message = { type: "text", id: `${now}${__VU}${__ITER}`, text: pick(TEXTS) };
  }
  const body = JSON.stringify({ destination: "Uload", events: [event] });
  const res = http.post(`${BASE}/webhooks/line`, body, {
    headers: {
      "Content-Type": "application/json",
      "X-Line-Signature": crypto.hmac("sha256", SECRET, body, "base64"),
    },
    tags: { page: "webhook" },
  });
  check(res, { "webhook stored": (r) => r.status === 200 });
}

let queueUrl = "";

function signIn() {
  const form = http.get(`${BASE}/accounts/login/`, { tags: { page: "login" } });
  const token = (form.body.match(/name="csrfmiddlewaretoken" value="([^"]+)"/) || [])[1];
  const res = http.post(
    `${BASE}/accounts/login/`,
    { username: SUBJECTS.staff, password: __ENV.LOAD_PASSWORD, csrfmiddlewaretoken: token },
    {
      headers: { Referer: `${BASE}/accounts/login/`, Origin: BASE },
      tags: { page: "login" },
    },
  );
  if (check(res, { "staff signed in": (r) => r.url.includes("/divisions/") })) {
    queueUrl = res.url;
  }
}

export function staff() {
  if (!queueUrl) {
    signIn();
    if (!queueUrl) {
      sleep(5);
      return;
    }
  }
  const queue = http.get(queueUrl, { tags: { page: "queue" }, redirects: 0 });
  if (!check(queue, { "queue page": (r) => r.status === 200 })) {
    queueUrl = ""; // signed out: sign in again next time
    return;
  }
  const numbers = [...queue.body.matchAll(/href="\/cases\/([^/"?]+)\/"/g)].map((m) => m[1]);
  if (numbers.length) {
    const detail = http.get(`${BASE}/cases/${pick(numbers)}/`, {
      tags: { page: "case" },
      redirects: 0,
    });
    check(detail, { "case page": (r) => r.status === 200 });
  }
  sleep(P.think[0] + Math.random() * (P.think[1] - P.think[0]));
}

export function handleSummary(data) {
  const summary = (name) => {
    const metric = data.metrics[name];
    if (!metric) return null;
    const thresholds = {};
    for (const [rule, result] of Object.entries(metric.thresholds || {})) {
      thresholds[rule] = result.ok;
    }
    return { values: metric.values, thresholds };
  };
  const names = Object.keys(data.metrics).filter(
    (name) => name.startsWith("http_req_duration{") || name.startsWith("checks{"),
  );
  const report = {
    profile: __ENV.PROFILE || "smoke",
    metrics: Object.fromEntries(
      ["http_reqs", "http_req_failed", "http_req_duration", "checks", "dropped_iterations", ...names]
        .map((name) => [name, summary(name)])
        .filter(([, value]) => value),
    ),
  };
  report.passed = Object.values(report.metrics).every((m) =>
    Object.values(m.thresholds).every(Boolean),
  );
  return {
    "/out/k6-summary.json": JSON.stringify(report, null, 2),
    stdout: `k6 ${report.profile}: ${report.passed ? "PASS" : "FAIL"}\n`,
  };
}
