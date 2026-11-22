// API/지도 응답을 재현하는 브라우저 회귀 검사. 실제 의료기관 API는 호출하지 않는다.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '../frontend');
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
const server = http.createServer(async (req, res) => {
  const file = path.resolve(root, '.' + decodeURIComponent(new URL(req.url, 'http://localhost').pathname));
  if (!file.startsWith(root + path.sep)) { res.writeHead(404).end(); return; }
  try { res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' }).end(await fs.readFile(file)); }
  catch { res.writeHead(404).end(); }
});
const places = [
  { name: '검증응급실', kind: '응급실', dept: '응급의학과', distance_m: 2000, is_open: true, phone: '032-222-3333', address: '인천 미추홀구 응급로 3', lat: 37.46, lng: 126.66 },
  { name: '먼거리의원', kind: '병원', dept: '내과', distance_m: 42500, is_open: null, phone: null, address: '인천 강화군 예시로 42', lat: 37.74, lng: 126.49 },
  { name: '검증약국', kind: '약국', dept: '약국', distance_m: 100, is_open: false, phone: '032-123-4567', address: null, lat: null, lng: null },
  { name: '가까운의원', kind: '병원', dept: '내과', distance_m: 350, is_open: true, phone: '032-555-6666', address: '인천 미추홀구 예시로 1', lat: 37.45, lng: 126.65 },
];
const reply = { reply: '소화기내과 진료를 안내합니다.', emergency: false, need_more_info: false,
  departments: [{ name: '소화기내과', score: .8, reason: '증상을 확인할 수 있는 진료과입니다.' }], hospitals: places };

async function checkAccessibility(page) {
  const problems = await page.evaluate(() => {
    const issues = [];
    const visible = node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
    for (const node of document.querySelectorAll('p,span,a,button,label,summary,h1,h2,h3,small,input,textarea')) {
      if (!visible(node) || node.closest('svg') || node.disabled) continue;
      const hasText = [...node.childNodes].some(child => child.nodeType === Node.TEXT_NODE && child.textContent.trim());
      if (hasText && parseFloat(getComputedStyle(node).fontSize) < 16) issues.push(`small text: ${node.textContent.slice(0, 30)}`);
    }
    for (const node of document.querySelectorAll('button:not(:disabled),a[href],summary,.open-filter,input:not([type=checkbox]),textarea')) {
      if (!visible(node)) continue;
      const rect = node.getBoundingClientRect();
      if (rect.height < 44 || rect.width < 44) issues.push(`small target: ${node.textContent.slice(0, 30)} (${rect.width} × ${rect.height})`);
    }
    return issues;
  });
  assert.deepEqual(problems, []);
}

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.route('**/*', route => {
      const url = route.request().url();
      if (url.startsWith(base)) return route.continue();
      if (url.startsWith('https://www.openstreetmap.org/export/embed.html')) return route.fulfill({ contentType: 'text/html', body: '<p>Map embed fixture</p>' });
      return route.abort();
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const requests = [];
    let mode = 'normal';
    await page.route('**/api/chat', async route => {
      requests.push(route.request().postDataJSON());
      if (mode === 'failure') return route.fulfill({ status: 503, body: 'unavailable' });
      const data = mode === 'followup' ? { ...reply, reply: '언제부터 불편하셨나요?', need_more_info: true }
        : mode === 'emergency' ? { reply: '즉시 응급 안내를 확인하세요.', emergency: true, need_more_info: false, hospitals: [], departments: [] }
        : mode === 'emergency-places' ? { ...reply, emergency: true, need_more_info: true }
        : mode === 'sources' ? { ...reply, departments: [{ name: '정형외과', score: .8, reason: '문서 범위와 입력을 비교했습니다.', sources: ['정형외과.md', '<img src=x onerror="window.injected=true">'] }] }
        : mode === 'sources-low' ? { ...reply, departments: [{ name: '정형외과', score: .3, reason: '정보 부족', sources: ['정형외과.md'] }] }
        : mode === 'low-score' ? { ...reply, departments: [{ name: '외과', score: .2, reason: '정보 부족' }, { name: '내과', score: .4, reason: '정보 부족' }, { name: '치과', score: .1, reason: '정보 부족' }, { name: '피부과', score: .05, reason: '정보 부족' }] }
        : mode === 'unsafe-text' ? { ...reply, reply: '<img src=x onerror="window.injected=true">', hospitals: [{ ...places[3], name: '<script>window.injected=true</script>' }] }
        : reply;
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });

    await page.goto(`${base}/hospitals.html`);
    assert.match(await page.locator('#hospital-list').innerText(), /AI 문진을 먼저 진행해 주세요/);
    assert.equal(await page.locator('#map-placeholder').innerText(), 'AI 문진을 먼저 진행해 주세요.');
    assert.equal(await page.locator('.filters').isVisible(), false);
    assert.equal(await page.locator('#hospital-list a').getAttribute('href'), 'chat.html');
    // 결과 만료와 실제 조회 0건도 목록/지도에서 같은 상태를 안내한다.
    await page.evaluate(() => sessionStorage.setItem('dr-router.results.v1', JSON.stringify({
      hospitals: [], departments: [], savedAt: Date.now() - 31 * 60 * 1000,
    })));
    await page.reload();
    assert.equal(await page.locator('#map-placeholder').innerText(), 'AI 문진을 먼저 진행해 주세요.');
    for (const position of [null, { lat: 37.5, lng: 126.6 }]) {
      await page.evaluate(position => DrRouter.saveResults({ hospitals: [], departments: [] }, '', position), position);
      await page.reload();
      assert.match(await page.locator('#hospital-list').innerText(), /이번 응답에 의료기관이 없습니다/);
      assert.equal(await page.locator('#map-placeholder').innerText(), await page.locator('#hospital-list p').first().innerText());
      assert.equal(await page.locator('#places-map').isVisible(), false);
      assert.equal(await page.locator('#hospital-list a').innerText(), 'AI 문진 이어가기');
    }
    await page.evaluate(() => DrRouter.clearResults());
    console.log('PASS no consultation, expired results and zero facilities with/without GPS');
    await page.goto(`${base}/chat.html`);
    await page.locator('#send').click();
    assert.match(await page.locator('#chat-status').innerText(), /입력해/);
    await page.locator('#message').fill('가'.repeat(501));
    await page.locator('#send').click();
    assert.match(await page.locator('#chat-status').innerText(), /500자/);
    assert.equal(requests.length, 0);
    await page.evaluate(() => Object.defineProperty(navigator, 'geolocation', { configurable: true, value: { getCurrentPosition: (_, fail) => fail({ code: 1 }) } }));
    await page.locator('#get-location').click();
    assert.match(await page.locator('#location-status').innerText(), /시·군·구/);
    await page.locator('#region').fill('인천 미추홀구');
    mode = 'followup';
    await page.locator('#message').fill('속이 쓰려요');
    await page.locator('#send').click();
    await page.waitForFunction(() => document.querySelector('#chat-status').textContent === '추가 질문에 답해 주세요.');
    assert.equal(requests[0].region, '인천 미추홀구');
    assert.equal(requests[0].lat, null);
    assert.deepEqual(requests[0].history, []);
    assert.equal(await page.locator('#recommendations').isVisible(), false);
    assert.equal(await page.locator('#results-link').isVisible(), false);
    assert.equal(await page.evaluate(() => DrRouter.loadResults()), null);
    mode = 'normal';
    await page.locator('#message').fill('어제부터요');
    await page.locator('#send').click();
    await page.locator('#recommendations').waitFor({ state: 'visible' });
    assert.equal(requests[1].history.length, 2);
    assert.equal(requests[1].history[0].content, '속이 쓰려요');
    assert.match(await page.locator('#recommendations').innerText(), /소화기내과/);
    await page.locator('#results-link').click();
    await page.waitForURL('**/hospitals.html');
    assert.deepEqual(await page.locator('.hospital-card h2').allTextContents(), ['검증약국', '가까운의원', '검증응급실', '먼거리의원']);
    assert.match(await page.locator('.hospital-card').nth(3).innerText(), /확인 필요/);
    assert.match(await page.locator('.hospital-card').nth(3).innerText(), /42.5 km/);
    assert.equal(await page.locator('.hospital-card').nth(2).locator('.tag').innerText(), '24시간');
    assert.equal(await page.locator('.hospital-card').nth(1).locator('.tag').innerText(), '진료중');
    const directionsLink = page.getByRole('link', { name: '가까운의원 길찾기 (구글맵, 새 창)', exact: true });
    const directionsUrl = new URL(await directionsLink.getAttribute('href'));
    assert.equal(directionsUrl.searchParams.get('destination'), '37.45,126.65');
    assert.equal(await directionsLink.getAttribute('target'), '_blank');
    assert.equal(await directionsLink.getAttribute('rel'), 'noopener noreferrer');
    assert.equal(await page.locator('.hospital-card').first().getByRole('link', { name: /길찾기/ }).count(), 0);
    assert.match(await page.locator('#search-context').innerText(), /소화기내과/);
    assert.equal(await page.locator('.hospital-card .dept').first().innerText(), '내과');
    assert.equal(await page.locator('.hospital-card a').first().getAttribute('href'), 'tel:0321234567');
    await page.locator('summary').first().click();
    assert.equal(await page.locator('details').first().getAttribute('open'), '');
    assert.equal(await page.locator('.place-marker').count(), 3);
    await page.getByRole('button', { name: '지도 보기', exact: true }).first().click();
    assert.match(await page.locator('#external-map').getAttribute('href'), /mlat=37.45&mlon=126.65/);
    assert.equal(await page.locator('#map-name').innerText(), '가까운의원');
    await page.locator('[data-kind="약국"]').click();
    assert.equal(await page.locator('.hospital-card').count(), 1);
    assert.equal(await page.getByRole('button', { name: '좌표 정보 없음' }).isDisabled(), true);
    assert.equal(await page.locator('#places-map').isVisible(), false);
    assert.match(await page.locator('#map-placeholder').innerText(), /주소와 전화번호/);
    await page.locator('#only-open').check();
    assert.match(await page.locator('#hospital-list').innerText(), /조건/);
    assert.equal(await page.locator('#map-placeholder').innerText(), await page.locator('#hospital-list p').first().innerText());
    assert.equal(await page.locator('.filters').isVisible(), true);
    await page.locator('[data-kind="전체"]').click();
    assert.deepEqual(await page.locator('.hospital-card h2').allTextContents(), ['가까운의원', '검증응급실']);
    await page.locator('#only-open').uncheck();
    await fs.mkdir(path.resolve(__dirname, '../test-results'), { recursive: true });
    await page.screenshot({ path: path.resolve(__dirname, '../test-results/hospitals-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 360, height: 800 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await checkAccessibility(page);
    await page.screenshot({ path: path.resolve(__dirname, '../test-results/hospitals-mobile.png'), fullPage: true });
    console.log('PASS chat → follow-up → hospital list, filters, status, distance, phone, map, mobile');

    await page.goto(`${base}/chat.html`);
    mode = 'sources';
    await page.locator('#message').fill('무릎이 시큰거려요');
    await page.locator('#send').click();
    await page.locator('#recommendations').waitFor({ state: 'visible' });
    assert.match(await page.locator('#recommendations').innerText(), /근거 문서: 정형외과.md/);
    assert.doesNotMatch(await page.locator('#recommendations').innerText(), /근거 문서를 확인할 수 없어/);
    assert.equal(await page.locator('#recommendations img').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    assert.deepEqual(await page.evaluate(() => DrRouter.loadResults().departments), [{ name: '정형외과' }]);
    mode = 'sources-low';
    await page.locator('#message').fill('아직 불편해요');
    await page.locator('#send').click();
    await page.waitForFunction(() => document.querySelector('#recommendations').textContent.includes('분류 확신이 낮아'));
    assert.match(await page.locator('#recommendations').innerText(), /정형외과.md/);
    assert.match(await page.locator('#recommendations').innerText(), /방문해 상담/);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    console.log('PASS source document rendering, safe text, low confidence and storage minimization');

    await page.goto(`${base}/chat.html`);
    mode = 'failure';
    await page.locator('#message').fill('재시도할 입력');
    await page.locator('#send').click();
    await page.waitForFunction(() => document.querySelector('#chat-status').textContent.includes('다시 전송'));
    assert.equal(await page.locator('#message').inputValue(), '재시도할 입력');
    assert.equal(await page.locator('#conversation .msg.me').count(), 0);
    assert.equal(await page.locator('#send').isDisabled(), false);
    assert.equal(await page.locator('#results-link').isVisible(), false);
    assert.equal(await page.evaluate(() => DrRouter.loadResults()), null);
    mode = 'unsafe-text';
    await page.locator('#send').click();
    await page.locator('#results-link').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#conversation .msg.me').count(), 1);
    assert.equal(await page.locator('#conversation img').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    await page.locator('#results-link').click();
    await page.waitForURL('**/hospitals.html');
    assert.equal(await page.locator('.hospital-card script').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    console.log('PASS failed request retry, stale-result clearing, safe text rendering');

    await page.goto(`${base}/chat.html`);
    mode = 'emergency';
    await page.locator('#message').fill('응급 예시');
    await page.locator('#send').click();
    await page.locator('#emergency-result').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#recommendations').isVisible(), false);
    await page.locator('#results-link').click();
    await page.waitForURL('**/hospitals.html');
    assert.equal(await page.locator('#emergency-result').isVisible(), true);
    assert.match(await page.locator('#hospital-list').innerText(), /응급실 정보가 없/);
    assert.match(await page.locator('#map-placeholder').innerText(), /119에 연락/);
    assert.equal(await page.locator('#hospital-list a').count(), 0);
    await page.goto(`${base}/chat.html`);
    await page.locator('#new-chat').click();
    assert.equal(await page.evaluate(() => DrRouter.loadResults()), null);
    assert.equal(await page.locator('#conversation .msg').count(), 0);
    assert.deepEqual(errors, []);
    console.log('PASS emergency without facilities and reset');

    mode = 'emergency-places';
    await page.locator('#message').fill('응급 목록 확인');
    await page.locator('#send').click();
    await page.locator('#emergency-places .hospital-card').waitFor();
    assert.deepEqual(await page.locator('#emergency-places h2').allTextContents(), ['검증응급실']);
    assert.equal(await page.locator('#emergency-places .tag').innerText(), '24시간');
    assert.equal(await page.locator('#emergency-places').getByRole('link', { name: /길찾기/ }).count(), 1);
    assert.equal(await page.locator('#recommendations').isVisible(), false);
    mode = 'low-score';
    await page.locator('#message').fill('새 문진');
    await page.locator('#send').click();
    await page.locator('#recommendations').waitFor({ state: 'visible' });
    assert.deepEqual(requests.at(-1).history, []);
    assert.equal(await page.locator('#conversation .msg').count(), 2);
    assert.equal(await page.locator('#emergency-result').isVisible(), false);
    assert.deepEqual(await page.locator('.department h3').allTextContents(), ['내과', '외과', '치과']);
    assert.match(await page.locator('#recommendations').innerText(), /분류 확신이 낮/);
    assert.match(await page.locator('#recommendations').innerText(), /0.40/);
    assert.match(await page.locator('#recommendations').innerText(), /근거 문서: 확인 필요/);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.resolve(__dirname, '../test-results/chat-mobile.png'), fullPage: true });
    await checkAccessibility(page);
    await page.goto(`${base}/index.html`);
    assert.equal(await page.locator('.menu').isVisible(), true);
    await checkAccessibility(page);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    console.log('PASS 500-character guard, follow-up result suppression, emergency restart, top-3 scores, mobile home');

    const geoContext = await browser.newContext({ permissions: ['geolocation'], geolocation: { latitude: 37.5, longitude: 126.6 } });
    await geoContext.route('**/*', route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    const geoPage = await geoContext.newPage();
    let geoRequest;
    await geoPage.route('**/api/chat', route => { geoRequest = route.request().postDataJSON(); return route.fulfill({ contentType: 'application/json', body: JSON.stringify(reply) }); });
    await geoPage.goto(`${base}/chat.html`);
    await geoPage.locator('#get-location').click();
    await geoPage.waitForFunction(() => document.querySelector('#location-status').textContent === '현재 위치를 기준으로 찾습니다.');
    await geoPage.locator('#message').fill('위치 포함 입력');
    await geoPage.locator('#send').click();
    await geoPage.locator('#results-link').waitFor({ state: 'visible' });
    assert.equal(geoRequest.lat, 37.5);
    assert.equal(geoRequest.lng, 126.6);
    assert.equal(geoRequest.region, null);
    await geoPage.locator('#results-link').click();
    await geoPage.waitForURL('**/hospitals.html');
    assert.equal(await geoPage.locator('.place-marker.me').count(), 1);
    assert.equal(await geoPage.locator('.place-marker.hospital').count(), 2);
    assert.equal(await geoPage.locator('.place-marker.er').count(), 1);
    assert.equal(await geoPage.locator('.place-marker.pharmacy').count(), 0); // 좌표 없는 기관을 추측하지 않는다.
    assert.deepEqual(errors, []);
    console.log('PASS location permission and coordinate request');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
