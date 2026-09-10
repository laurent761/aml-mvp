// Run with the cached Playwright runtime; no app services are started.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { pathToFileURL } = require('node:url');
const { chromium } = require('/Users/michelleberezin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = path.resolve(__dirname, '../../..');
const htmlPath = path.join(root, 'project-guide.html');
const html = fs.readFileSync(htmlPath, 'utf8');
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);

(async () => {
  const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
    const errors = [], remoteRequests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (/^https?:/.test(request.url())) remoteRequests.push(request.url()); });
    await page.goto(pathToFileURL(htmlPath).href);
    assert.match(await page.title(), /AML/);
    const validateLinks = async () => {
      const links = await page.locator('a[href]').evaluateAll(nodes => nodes.map(n => n.getAttribute('href')));
      for (const link of links) {
        if (link.startsWith('#')) assert.equal(await page.locator(link).count(), 1, `Fragment ${link}`);
        else if (!/^[a-z]+:/i.test(link)) assert.ok(fs.existsSync(path.resolve(root, link.split('#')[0])), `File ${link}`);
      }
    };
    await validateLinks();
    for (const component of await page.locator('[data-component]').all()) {
      await component.click();
      assert.equal(await component.getAttribute('aria-pressed'), 'true');
      assert.ok((await page.locator('#component-title').innerText()).length > 10);
      await validateLinks();
    }
    assert.equal(await page.locator('#previous-step').isDisabled(), true);
    for (let step = 1; step < 6; step++) {
      await page.locator('#next-step').click();
      assert.match(await page.locator('#stage-counter').innerText(), new RegExp(`^${step + 1} / 6`));
    }
    assert.equal(await page.locator('#next-step').isDisabled(), true);
    await page.locator('#previous-step').click();
    assert.match(await page.locator('#stage-counter').innerText(), /^5 \/ 6/);
    let combinations = 0;
    for (const mode of ['external', 'managed']) for (const surface of ['user_message', 'uploaded_document', 'simulated_tool_result']) for (const example of ['benign', 'attack', 'rejected', 'crash']) {
      await page.selectOption('#mode', mode);
      await page.selectOption('#surface', surface);
      await page.selectOption('#case', example);
      for (let step = 0; step < 6; step++) {
        await page.locator(`[data-step="${step}"]`).click();
        assert.ok((await page.locator('#stage-code').innerText()).length > 20);
        assert.ok(!(await page.locator('#stage-code').innerText()).includes('undefined'));
      }
      await page.locator('[data-step="3"]').click();
      const action = await page.locator('#stage-code').innerText();
      assert.ok(action.includes(surface));
      if (surface === 'simulated_tool_result') assert.ok(action.includes('invoice-content'));
      await page.locator('[data-result-view="research"]').click();
      const result = JSON.parse(await page.locator('#result-code').innerText());
      assert.equal(result.outcome.terminal_success, example === 'crash' ? null : example === 'attack');
      assert.equal(result.outcome.execution_status, example === 'crash' ? 'indeterminate' : example === 'rejected' ? 'invalid_action' : 'ok');
      await page.locator('[data-result-view="public"]').click();
      const publicResult = JSON.parse(await page.locator('#result-code').innerText());
      assert.ok(!('outcome' in publicResult));
      if (example === 'crash') assert.equal(publicResult.public_observation, null);
      else assert.equal(publicResult.public_observation.delivery_receipt.channel, surface);
      combinations++;
    }
    const lineageTitles = new Set();
    for (const button of await page.locator('[data-lineage]').all()) {
      await button.click();
      lineageTitles.add(await page.locator('#lineage-title').innerText());
    }
    assert.equal(lineageTitles.size, 5);
    const answers = new Set();
    for (const button of await page.locator('[data-question]').all()) {
      await button.click();
      answers.add(await page.locator('#evaluation-answer').innerText());
    }
    assert.equal(answers.size, 3);
    await page.locator('[data-profile="full"]').click();
    assert.equal(await page.locator('#ui-port').innerText(), '127.0.0.1:3000');
    assert.ok(await page.locator('#full-setup').isVisible());
    assert.ok(!(await page.locator('#research-setup').isVisible()));
    await page.locator('[data-profile="research"]').click();
    assert.equal(await page.locator('#api-port').innerText(), '127.0.0.1:18000');
    await page.locator('[data-copy="build-code"]').click();
    await page.waitForFunction(() => /copied|selected/.test(document.getElementById('copy-status').textContent));
    assert.match(await page.locator('#copy-status').innerText(), /copied|selected/);
    for (const details of await page.locator('#research-setup details').all()) {
      if (!(await details.getAttribute('open') !== null)) await details.locator('summary').click();
      assert.equal(await details.evaluate(el => el.open), true);
      await details.locator('summary').click();
      assert.equal(await details.evaluate(el => el.open), false);
    }
    await page.locator('[data-component="api"]').click();
    await page.selectOption('#mode', 'external');
    await page.selectOption('#surface', 'user_message');
    await page.selectOption('#case', 'benign');
    await page.locator('[data-step="0"]').click();
    await page.locator('#research-setup details').first().locator('summary').click();
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.join(__dirname, 'desktop.png'), fullPage: true });
    await page.addScriptTag({ path: '/Users/michelleberezin/.npm/_npx/308f87df00af8a80/node_modules/axe-core/axe.min.js' });
    const accessibility = await page.evaluate(async () => (await axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] } })).violations.map(v => ({ id: v.id, impact: v.impact, description: v.description, nodes: v.nodes.map(n => ({ target: n.target, summary: n.failureSummary })) })));
    const widths = [];
    for (const width of [390, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 844 });
      const metrics = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }));
      widths.push(metrics);
      assert.ok(metrics.content <= metrics.viewport, `Overflow at ${width}: ${metrics.content}`);
      if (width === 390) { await page.evaluate(() => window.scrollTo(0, 0)); await page.screenshot({ path: path.join(__dirname, 'mobile.png'), fullPage: true }); }
    }
    await page.goto(pathToFileURL(htmlPath).href);
    await page.keyboard.press('Tab');
    assert.equal(await page.locator(':focus').innerText(), 'Skip to the guide');
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => location.hash), '#main');
    const brokenLabels = await page.locator('[aria-labelledby]').evaluateAll(nodes => nodes.flatMap(n => n.getAttribute('aria-labelledby').split(/\s+/).filter(id => !document.getElementById(id))));
    assert.deepEqual(brokenLabels, []);
    assert.deepEqual(errors, []);
    assert.deepEqual(remoteRequests, []);
    const report = { validated_at: new Date().toISOString(), combinations, walkthrough_states: combinations * 6, architecture_nodes: 10, lineage_states: 5, evaluation_questions: 3, deployment_profiles: 2, local_links: 'passed', clipboard: 'passed', keyboard_skip_link: 'passed', widths, browser_errors: errors, external_requests: remoteRequests, accessibility_violations: accessibility };
    fs.writeFileSync(path.join(__dirname, 'result.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report, null, 2));
    assert.deepEqual(accessibility, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
