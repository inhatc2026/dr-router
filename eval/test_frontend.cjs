const { test } = require('node:test');
const assert = require('node:assert/strict');
const api = require('../frontend/assets/app.js');

test('unknown hours stay distinct from closed and only true matches open filter', () => {
  assert.equal(api.openStatus(null).text, '확인 필요');
  assert.equal(api.openStatus(undefined).text, '확인 필요');
  assert.equal(api.openStatus(false).text, '진료중 아님');
  assert.equal(api.openStatus(true, '응급실').text, '24시간');
  assert.equal(api.openStatus(null, '응급실').text, '확인 필요');
  assert.equal(api.openStatus(false, '응급실').text, '진료중 아님');
  assert.equal(api.openStatus(true, '병원').text, '진료중');
  const input = [true, false, null].map(is_open => ({ kind: '약국', is_open }));
  assert.deepEqual(api.filterHospitals(input, '약국', true), [input[0]]);
});
test('distance takes priority across kinds without mutating the response', () => {
  const input = [{ name: 'ER', kind: '응급실', distance_m: 20 }, { name: 'far', kind: '병원', distance_m: 35000 },
    { name: 'drug', kind: '약국', distance_m: 10 }, { name: 'near', kind: '병원', distance_m: 200 }];
  assert.deepEqual(api.filterHospitals(input).map(h => h.name), ['drug', 'ER', 'near', 'far']);
  assert.deepEqual(api.filterHospitals(input, '응급실').map(h => h.name), ['ER']);
  assert.equal(input[0].name, 'ER');
});
test('rural distances are neither capped nor relabeled as nearby', () => {
  assert.equal(api.formatDistance(35000), '35 km');
  assert.equal(api.formatDistance(1250), '1.3 km');
  assert.equal(api.formatDistance(0), '0 m');
  assert.equal(api.formatDistance(null), '거리 정보 없음');
  assert.equal(api.formatDistance(-1), '거리 정보 없음');
});
test('map coordinates do not turn missing or invalid positions into zero', () => {
  assert.equal(api.mapLinks({ lat: null, lng: 126 }), null);
  assert.equal(api.mapLinks({ lat: 95, lng: 126 }), null);
  assert.equal(api.mapLinks({ lat: '37', lng: '126' }), null);
  const map = api.mapLinks({ lat: 37.5, lng: 126.6 });
  assert.equal(new URL(map.embed).searchParams.get('marker'), '37.5,126.6');
  const directions = new URL(map.directions);
  assert.equal(directions.origin + directions.pathname, 'https://www.google.com/maps/dir/');
  assert.equal(directions.searchParams.get('api'), '1');
  assert.equal(directions.searchParams.get('destination'), '37.5,126.6');
  assert.equal(directions.searchParams.has('origin'), false);
  assert.ok(api.mapLinks({ lat: 0, lng: 0 }));
});
test('telephone links reject non-phone URLs', () => {
  assert.equal(api.phoneLink('032-123-4567'), 'tel:0321234567');
  assert.equal(api.phoneLink('javascript:alert(1)'), null);
  assert.equal(api.phoneLink(null), null);
});
test('sendChat carries the new region contract and reports HTTP failures', async () => {
  const original = global.fetch;
  const response = { reply: '확인', hospitals: [], departments: [] };
  try {
    global.fetch = async (url, options) => {
      assert.equal(url, '/api/chat');
      const body = JSON.parse(options.body);
      assert.equal(body.region, '인천 미추홀구');
      assert.equal(body.lat, null);
      assert.deepEqual(body.history, [{ role: 'user', content: '이전 입력' }]);
      return { ok: true, json: async () => response };
    };
    assert.deepEqual(await api.sendChat('증상', { region: '인천 미추홀구', history: [{ role: 'user', content: '이전 입력' }] }), response);
    global.fetch = async () => ({ ok: false });
    await assert.rejects(api.sendChat('증상'), /다시 전송/);
    global.fetch = async () => ({ ok: true, json: async () => ({ reply: '잘못된 응답' }) });
    await assert.rejects(api.sendChat('증상'), /응답 형식/);
    global.fetch = async () => ({ ok: true, json: async () => { throw new SyntaxError('invalid JSON'); } });
    await assert.rejects(api.sendChat('증상'), /응답 형식/);
    global.fetch = async () => { throw new DOMException('request timed out', 'AbortError'); };
    await assert.rejects(api.sendChat('증상'), /응답이 지연/);
  } finally { global.fetch = original; }
});
test('result storage expires and does not retain raw conversation or reasons', () => {
  const original = global.sessionStorage;
  const store = new Map();
  global.sessionStorage = { getItem: k => store.get(k), setItem: (k, v) => store.set(k, v), removeItem: k => store.delete(k) };
  try {
    assert.equal(api.saveResults({ hospitals: [], departments: [{ name: '치과', reason: '증상 근거' }], reply: '대화 내용' }), true);
    assert.deepEqual(api.loadResults().departments, [{ name: '치과' }]);
    const [key, value] = [...store][0];
    assert.equal(value.includes('대화 내용'), false);
    assert.equal(value.includes('증상 근거'), false);
    const expired = JSON.parse(value); expired.savedAt -= 31 * 60 * 1000;
    store.set(key, JSON.stringify(expired));
    assert.equal(api.loadResults(), null);
    store.set(key, '{broken');
    assert.equal(api.loadResults(), null);
  } finally { global.sessionStorage = original; }
});

// 2026-11-23 19:50 한국 시간 = 10:50 UTC
const BED_AT = '2026-11-23T19:50';
const BED_MS = Date.UTC(2026, 10, 23, 10, 50);
const minutes = m => m * 60 * 1000;

test('emergency bed badge shows count or full and ignores missing or non-ER values', () => {
  const er = beds => ({ kind: '응급실', er_beds: beds, er_beds_at: BED_AT });
  const now = BED_MS + minutes(5);
  const five = api.bedStatus(er(5), now);
  assert.deepEqual([five.text, five.className, five.time], ['병상 5', 'free', '19:50']);
  assert.equal(api.bedStatus(er(0), now).text, '병상 포화');
  assert.equal(api.bedStatus(er(-3), now).className, 'full');
  assert.match(api.bedStatus(er(2), now).detail, /19:50 기관 입력 기준/);
  assert.match(api.bedStatus(er(2), now).notice, /실제와 다를 수.*전화로 확인/);
  for (const item of [{ kind: '응급실' }, er(null), er('3'), er(NaN), { kind: '약국', er_beds: 3 }, null,
    { kind: '응급실', er_beds: 4 }, { kind: '응급실', er_beds: 4, er_beds_at: '19:50' }]) {
    assert.equal(api.bedStatus(item, now), null);
  }
});

test('bed counts expire after 60 minutes, including results reopened later', () => {
  const item = { kind: '응급실', er_beds: 3, er_beds_at: BED_AT };
  assert.ok(api.bedStatus(item, BED_MS + minutes(60)));
  assert.equal(api.bedStatus(item, BED_MS + minutes(70)), null);   // 저장된 결과를 70분 뒤에 다시 연 경우
  assert.equal(api.bedStatus(item, BED_MS - minutes(10)), null);   // 기기 시계보다 한참 미래
  assert.ok(api.bedStatus(item, BED_MS - minutes(3)));             // 약간의 시계 차이는 허용
});

test('emergency-only lists keep full ERs behind, mixed lists stay in distance order', () => {
  const now = BED_MS + minutes(5);
  const er = (name, distance_m, er_beds, er_beds_at = BED_AT) => ({ name, kind: '응급실', distance_m, er_beds, er_beds_at });
  const names = list => list.map(item => item.name);
  const full1 = er('포화1km', 1000, 0), free2 = er('여유2km', 2000, 4), unknown3 = { name: '모름3km', kind: '응급실', distance_m: 3000 };
  assert.deepEqual(names(api.filterHospitals([full1, free2, unknown3], '전체', false, now)), ['여유2km', '모름3km', '포화1km']);
  assert.deepEqual(names(api.filterHospitals([unknown3, full1, free2], '응급실', false, now)), ['여유2km', '모름3km', '포화1km']);
  // 가장 가까운 곳보다 10 km 넘게 먼 곳은 앞당기지 않는다 (서버와 같은 규칙)
  assert.deepEqual(names(api.filterHospitals([er('15km여유', 16000, 9), full1], '응급실', false, now)), ['포화1km', '15km여유']);
  // 만료된 포화 표시는 포화로 치지 않는다
  assert.deepEqual(names(api.filterHospitals([free2, er('옛포화1km', 1000, 0, '2026-11-23T17:00')], '응급실', false, now)), ['옛포화1km', '여유2km']);
  // 여러 종류가 섞인 목록은 거리순(FR-20)
  const drug = { name: '약국500m', kind: '약국', distance_m: 500 };
  assert.deepEqual(names(api.filterHospitals([free2, full1, drug], '전체', false, now)), ['약국500m', '포화1km', '여유2km']);
});

test('foreground and background combinations meet 4.5:1 text contrast', () => {
  const luminance = hex => {
    const rgb = hex.match(/\w\w/g).map(value => parseInt(value, 16) / 255)
      .map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
    return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
  };
  for (const [foreground, background] of [['53657b','f6f9fc'], ['53657b','edf0f3'], ['ffffff','1a6fb5'],
    ['187047','e6f6ee'], ['566473','edf0f3'], ['845700','fff3d5'], ['962f28','fff0ed'],
    ['155a94','eaf2fb'], ['ffffff','155a94'], ['ffffff','14633d'], ['ffffff','962f28'], ['ffffff','62339b']]) {
    const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
    assert.ok((values[0] + .05) / (values[1] + .05) >= 4.5, `${foreground} on ${background}`);
  }
});
