// 실제 uvicorn/LangGraph/SQLite/UI 통합. 데이터만 합성 fixture이며 외부 API는 사용하지 않는다.
const assert = require('node:assert/strict');
const { spawn, execFileSync } = require('node:child_process');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { chromium } = require('playwright');

(async () => {
  const cwd = path.resolve(__dirname, '..');
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'dr-router-live-'));
  const database = path.join(temporary, 'fixture.sqlite');
  const python = process.env.TEST_PYTHON || 'python';
  let server, closed, browser;
  let log = '';
  try {
    execFileSync(python, ['-X', 'utf8', '-m', 'eval.graph_fixture', database], { cwd, stdio: 'pipe' });
    const probe = net.createServer();
    await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
    const port = probe.address().port;
    await new Promise(resolve => probe.close(resolve));
    const base = `http://127.0.0.1:${port}`;
    server = spawn(python, ['-X', 'utf8', '-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', String(port)], {
      cwd, windowsHide: true, env: { ...process.env, HOSPITAL_DB: database, HIRA_API_KEY: '', EGEN_API_KEY: '', LLM_API_KEY: '' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    closed = new Promise(resolve => server.once('close', resolve));
    server.stdout.on('data', chunk => { log = (log + chunk).slice(-6000); });
    server.stderr.on('data', chunk => { log = (log + chunk).slice(-6000); });
    let ready = false;
    for (let attempt = 0; attempt < 75; attempt++) {
      try { if ((await fetch(`${base}/api/health`)).ok) { ready = true; break; } } catch {}
      if (server.exitCode !== null) break;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    assert.ok(ready, log);
    browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) });
    const context = await browser.newContext({ viewport: { width: 360, height: 800 }, permissions: ['geolocation'], geolocation: { latitude: 37.47, longitude: 126.62 } });
    await context.route('**/*', route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`${base}/chat.html`);
    await page.locator('#get-location').click();
    await page.waitForFunction(() => document.querySelector('#location-status').textContent === '현재 위치를 기준으로 찾습니다.');
    await page.locator('#region').fill('중구');
    await page.locator('#message').fill('이가 시려요');
    await page.locator('#send').click();
    await page.waitForFunction(() => document.querySelector('#chat-status').textContent === '추가 질문에 답해 주세요.');
    assert.match(await page.locator('#conversation').innerText(), /인천광역시 중구/);
    assert.equal(await page.locator('#results-link').isVisible(), false);
    await page.locator('#message').fill('네 인천 중구 맞아요');
    await page.locator('#send').click();
    await page.locator('#results-link').waitFor({ state: 'visible' });
    assert.match(await page.locator('#recommendations').innerText(), /의료기관을 방문해 상담/);
    assert.equal(await page.locator('.department').count(), 0); // 실제 분류 스텁을 가리지 않는다.
    await page.locator('#results-link').click();
    await page.waitForURL('**/hospitals.html');
    assert.deepEqual(await page.locator('.hospital-card h2').allTextContents(), ['테스트 인천 약국', '테스트 인천 응급실']);
    assert.match(await page.locator('#search-context').innerText(), /요청 지역: 중구/);
    assert.equal(await page.locator('.place-marker.me').count(), 1);

    await page.goto(`${base}/chat.html`);
    await page.locator('#get-location').click();
    await page.waitForFunction(() => document.querySelector('#location-status').textContent === '현재 위치를 기준으로 찾습니다.');
    await page.locator('#message').fill('이가 아프고 목까지 부었어요');
    await page.locator('#send').click();
    await page.locator('#emergency-places .hospital-card').waitFor();
    assert.deepEqual(await page.locator('#emergency-places h2').allTextContents(), ['테스트 인천 응급실']);
    assert.equal(await page.locator('#recommendations').isVisible(), false);
    await page.locator('#message').fill('이가 시려요');
    await page.locator('#send').click();
    await page.locator('#recommendations').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#emergency-result').isVisible(), false);
    assert.equal(await page.locator('#conversation .msg').count(), 2);
    assert.deepEqual(errors, []);
    console.log('PASS real uvicorn + LangGraph + fixture SQLite + browser: region confirmation, list, dental emergency, restart');
  } finally {
    if (browser) await browser.close();
    if (server) { server.kill(); await closed; }
    await fs.rm(database, { force: true });
    await fs.rmdir(temporary);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
