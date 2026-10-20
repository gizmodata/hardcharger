import { webkit } from '@playwright/test';
const text = 'SELECT id,\n    customer\n  FROM sales.orders\n    WHERE id < 3;';
for (const headless of process.env.PROBE_HEADLESS_ONLY ? [true] : [true, false]) {
  for (const removeSource of [true, false]) {
    const browser = await webkit.launch({ headless });
    const context = await browser.newContext({ userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 Version/18.4 Safari/605.1.15' });
    const page = await context.newPage();
    await page.route('http://localhost:43219/', route => route.fulfill({ contentType: 'text/html', body: '<textarea id="target"></textarea>' }));
    await page.goto('http://localhost:43219/');
    await page.evaluate(t => {
      window.clipEvents = [];
      document.addEventListener('copy', e => window.clipEvents.push({ type: e.type, trusted: e.isTrusted }));
      document.addEventListener('paste', e => window.clipEvents.push({ type: e.type, trusted: e.isTrusted, text: e.clipboardData?.getData('text/plain') }));
      const source = document.createElement('textarea'); source.id = 'source'; source.value = t;
      document.body.appendChild(source); source.focus(); source.select();
    }, text);
    await page.keyboard.press('Control+C');
    if (removeSource) await page.locator('#source').evaluate(el => el.remove());
    await page.locator('#target').click();
    await page.keyboard.press('Control+V');
    await page.waitForTimeout(250);
    console.log(JSON.stringify({ display: process.env.DISPLAY ?? null, headless, removeSource, matched: (await page.locator('#target').inputValue()) === text,
      details: await page.evaluate(() => ({ events: window.clipEvents, clipboard: !!navigator.clipboard, platform: navigator.platform })) }));
    await browser.close();
  }
}
