#!/usr/bin/env node
// Checks the report page embedded in dupefinder.py (REPORT_TEMPLATE) against
// dev/sample_data.json in headless Chromium.
//   node dev/check_report.js [screenshot-dir]
// Injects the sample for mode "server" (API mocked with page.route) and mode "static"
// (opened from file://), runs the report's flows, fails on any console error, uncaught
// exception or request to a non-local address, and writes screenshots.
'use strict';
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const os = require('os');

const DEV = __dirname;
const SHOTS = path.resolve(process.argv[2] || process.env.DF_SHOTS ||
  path.join(os.tmpdir(), 'dupefinder-report-shots'));
const SCRIPT = fs.readFileSync(path.join(DEV, '..', 'dupefinder.py'), 'utf8');
const TEMPLATE = SCRIPT.split('REPORT_TEMPLATE = r"""')[1].split('"""')[0];
const SAMPLE = JSON.parse(fs.readFileSync(path.join(DEV, 'sample_data.json'), 'utf8'));
const PORT = 8765, ORIGIN = `http://127.0.0.1:${PORT}`, TOKEN = 'tok-3f9c1a7e';
const EVIL = '<img src=x onerror="window.__pwned=1">.jpg';
const GONE = 'DupeFinder isn\u2019t running any more \u2014 actions are unavailable.';
fs.mkdirSync(SHOTS, { recursive: true });

/* ---------- helpers mirroring the page ---------- */
const nf0 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const fmt = (n) => nf0.format(n);
const plural = (n, one) => fmt(n) + ' ' + (n === 1 ? one : one + 's');
function human(b) {
  if (b < 1024) return fmt(b) + ' B';
  const u = ['KB', 'MB', 'GB', 'TB', 'PB']; let i = -1;
  do { b /= 1024; i++; } while (b >= 1023.95 && i < u.length - 1);
  return (b >= 99.95 ? nf0 : nf1).format(b) + ' ' + u[i];
}
const barText = (n, b) => `Move ${plural(n, 'file')} (${human(b)}) to Recycle Bin`;
const initial = (m) => (!m.locked && m.suggest === 'trash' ? 'trash' : 'keep');
function suggested(pred = () => true) {
  let n = 0, b = 0;
  for (const g of SAMPLE.groups) if (pred(g)) for (const m of g.members) if (initial(m) === 'trash') { n++; b += m.size; }
  return { n, b };
}
const G = SAMPLE.groups;
const countGroups = (pred) => G.filter(pred).length;

function inject(data) {
  const parts = TEMPLATE.split('__DF_DATA__');
  if (parts.length !== 2) throw new Error(`placeholder __DF_DATA__ must appear exactly once (found ${parts.length - 1})`);
  return parts[0] + JSON.stringify(data).replace(/</g, '\\u003c') + parts[1];
}

let passed = [];
function ok(cond, msg) {
  if (!cond) throw new Error('CHECK FAILED: ' + msg);
  passed.push(msg);
}
const shot = (page, name, opts = {}) => page.screenshot({ path: path.join(SHOTS, name + '.png'), ...opts });

function watch(page, label, errors, allow = () => false) {
  page.on('console', (m) => { if (m.type() === 'error' && !allow(m)) errors.push(`[${label}] console error: ${m.text()} @ ${m.location().url}`); });
  page.on('pageerror', (e) => errors.push(`[${label}] uncaught: ${e.message}`));
  page.on('dialog', (d) => { errors.push(`[${label}] unexpected native dialog: ${d.message()}`); d.dismiss(); });
}

/* ---------- fake images ---------- */
function photoSvg(fid, w, h) {
  const hue = (Number(fid) * 47) % 360;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">` +
    `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl(${hue},55%,66%)"/><stop offset="1" stop-color="hsl(${(hue + 50) % 360},45%,34%)"/></linearGradient></defs>` +
    `<rect width="100%" height="100%" fill="url(#g)"/><circle cx="${w * 0.7}" cy="${h * 0.3}" r="${Math.min(w, h) * 0.11}" fill="hsl(${(hue + 180) % 360},80%,86%)"/>` +
    `<path d="M0 ${h * 0.82} L${w * 0.32} ${h * 0.5} L${w * 0.58} ${h * 0.74} L${w * 0.8} ${h * 0.56} L${w} ${h * 0.7} V${h} H0Z" fill="rgba(0,0,0,.3)"/></svg>`;
}
function pdfSvg(w, h) {
  let lines = '';
  for (let i = 0; i < 9; i++) lines += `<rect x="${w * 0.2}" y="${h * (0.22 + i * 0.07)}" width="${w * (i % 3 === 2 ? 0.4 : 0.6)}" height="${h * 0.025}" fill="#9aa3ad"/>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}"><rect width="100%" height="100%" fill="#e9ecef"/><rect x="${w * 0.12}" y="${h * 0.06}" width="${w * 0.76}" height="${h * 0.88}" fill="#fff" stroke="#c9cfd6"/><rect x="${w * 0.2}" y="${h * 0.12}" width="${w * 0.45}" height="${h * 0.05}" fill="#56606b"/>${lines}</svg>`;
}
const byFid = new Map();
for (const g of G) for (const m of g.members) byFid.set(String(m.fid), m);

/* ---------- server mode ---------- */
async function serverPage(browser, { width, height, scheme, errors, label }) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: scheme, locale: 'en-US', deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  const html = inject({ ...SAMPLE, mode: 'server', token: TOKEN });
  const st = { moves: [], opens: [], quits: 0, external: [], badToken: [], moveDelay: 0, openFail: false,
    responder: (ids) => ids.map((fid) => ({ fid, ok: true, action: 'recycle', dest: '' })) };
  // The only console error we accept is the browser's own log line for the server-gone simulation.
  watch(page, label, errors, (m) => st.openFail && /ERR_CONNECTION_REFUSED/.test(m.text()) && m.location().url.endsWith('/api/open'));
  await ctx.route('**/*', async (route) => {
    const req = route.request();
    const u = new URL(req.url());
    if (u.origin !== ORIGIN) { st.external.push(req.url()); return route.abort(); }
    const p = u.pathname;
    if (req.method() === 'GET') {
      if (p === '/') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
      if (u.searchParams.get('token') !== TOKEN) { st.badToken.push(req.url()); return route.fulfill({ status: 403, body: 'bad token' }); }
      let mm;
      if ((mm = /^\/(thumb|view)\/([^/]+)$/.exec(p))) {
        const m = byFid.get(decodeURIComponent(mm[2]));
        if (!m) return route.fulfill({ status: 404, body: '' });
        const big = mm[1] === 'view';
        const body = m.kind === 'pdf' ? pdfSvg(big ? 850 : 200, big ? 1100 : 256) : photoSvg(m.fid, big ? 1600 : 256, big ? 1200 : 192);
        return route.fulfill({ contentType: 'image/svg+xml', body });
      }
      if ((mm = /^\/crop\/([^/]+)\/(sharp|blur)$/.exec(p))) {
        const body = `<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><rect width="300" height="300" fill="${mm[2] === 'sharp' ? '#2f6f4f' : '#8a7d6b'}"/>` +
          Array.from({ length: 12 }, (_, i) => `<rect x="${i * 25}" y="0" width="12" height="300" fill="rgba(255,255,255,${mm[2] === 'sharp' ? 0.5 : 0.12})"/>`).join('') + '</svg>';
        return route.fulfill({ contentType: 'image/svg+xml', body });
      }
      return route.fulfill({ status: 404, body: '' });
    }
    if (req.method() === 'POST') {
      const h = req.headers();
      if (h['x-token'] !== TOKEN || !String(h['content-type']).startsWith('application/json')) { st.badToken.push(req.url()); return route.fulfill({ status: 403, body: '{}' }); }
      const body = req.postDataJSON() || {};
      if (p === '/api/move') {
        st.moves.push(body);
        if (st.moveDelay) await new Promise((r) => setTimeout(r, st.moveDelay));
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ results: st.responder(body.ids) }) });
      }
      if (p === '/api/open') {
        if (st.openFail) return route.abort('connectionrefused');
        st.opens.push(body);
        return route.fulfill({ contentType: 'application/json', body: '{"ok":true}' });
      }
      if (p === '/api/quit') { st.quits++; return route.fulfill({ contentType: 'application/json', body: '{"ok":true}' }); }
    }
    return route.fulfill({ status: 404, body: '' });
  });
  await page.goto(`${ORIGIN}/?token=${TOKEN}`);
  await page.waitForSelector('.card');
  return { page, ctx, st };
}

const rowSel = (fid) => `.row[data-fid="${fid}"]`;
const btn = (fid, c) => `${rowSel(fid)} button[data-choice="${c}"]`;
const pressed = (page, fid, c) => page.getAttribute(btn(fid, c), 'aria-pressed');
const barNow = (page) => page.textContent('#move');
const noHScroll = (page) => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
async function ensureCard(page, gid) {
  for (let i = 0; i < 20; i++) {
    if (await page.$(`.card[data-gid="${gid}"]`)) return page.locator(`.card[data-gid="${gid}"]`);
    await page.evaluate(() => document.getElementById('sentinel').scrollIntoView());
    await page.waitForTimeout(80);
  }
  throw new Error('group ' + gid + ' never rendered');
}
async function setFolder(page, text) {
  await page.fill('#f-folder', text);
  await page.waitForTimeout(260);
}
const showing = (page) => page.textContent('#showing');
const expShowing = (n) => `Showing ${fmt(n)} of ${plural(G.length, 'group')}`;

async function serverChecks(browser, errors) {
  const { page, ctx, st } = await serverPage(browser, { width: 1400, height: 900, scheme: 'light', errors, label: 'server-desktop' });
  // initial render
  ok(await page.locator('.card').count() === 30, 'server: first chunk renders exactly 30 group cards');
  ok(await showing(page) === expShowing(G.length), 'server: "Showing X of Y groups" initial');
  const sug = suggested();
  ok(await barNow(page) === barText(sug.n, sug.b), `server: action bar starts from suggestions (${barText(sug.n, sug.b)})`);
  ok(await page.locator('.tg').count() > 0 && await page.isVisible('#quit'), 'server: toggles and Quit button present');
  // hostile text
  const evilG = G.find((g) => g.members.some((m) => m.name === EVIL));
  await ensureCard(page, evilG.id);
  const evilFid = String(evilG.members[0].fid), otherFid = String(evilG.members[1].fid);
  ok(await page.textContent(`${rowSel(evilFid)} .name`) === EVIL, 'hostile file name renders as literal text');
  ok((await page.textContent(`${rowSel(evilFid)} .folder`)).includes('<script>'), 'hostile folder renders as literal text');
  ok(await page.evaluate(() => document.querySelectorAll('img[src="x"]').length === 0 && document.scripts.length === 1), 'no injected <img src=x> or extra <script> elements');
  await page.click('#skip-sum');
  ok((await page.textContent('#skipbox')).includes('"><svg onload=window.__pwned=4>.jpg'), 'hostile path in Skipped panel renders as text');
  await shot(page, 'server-desktop-light-top');
  await page.click('#skip-sum');
  ok(await page.evaluate(() => window.__pwned === undefined), 'no hostile handler executed (window.__pwned unset)');

  // toggles and keep-one rule
  ok(await pressed(page, evilFid, 'keep') === 'true' && await pressed(page, otherFid, 'trash') === 'true', 'initial choices follow suggestions');
  await page.click(btn(evilFid, 'trash'));
  ok(await pressed(page, evilFid, 'keep') === 'true', 'keep-one rule blocks trashing the last kept copy');
  ok((await page.textContent(`${rowSel(evilFid)} .note`)).includes('Keep at least one copy in each group'), 'keep-one rule shows inline message');
  await page.click(btn(otherFid, 'keep'));
  ok(await pressed(page, otherFid, 'keep') === 'true', 'Keep toggle works');
  ok(await barNow(page) === barText(sug.n - 1, sug.b - evilG.members[1].size), 'bar count/size update after toggle');
  await page.click(btn(evilFid, 'trash'));
  ok(await pressed(page, evilFid, 'trash') === 'true', 'Trash allowed once another copy is kept');
  ok((await page.textContent(`.card[data-gid="${evilG.id}"] .gspace`)).includes(human(evilG.members[0].size)), 'group space recomputed live');
  await page.click(btn(otherFid, 'trash'));
  ok(await pressed(page, otherFid, 'keep') === 'true', 'keep-one rule blocks the other direction too');
  await page.click(btn(evilFid, 'keep')); await page.click(btn(otherFid, 'trash'));
  ok(await barNow(page) === barText(sug.n, sug.b), 'choices restored');
  // locked member
  const lockedM = G.flatMap((g) => g.members).find((m) => m.locked);
  const lockedG = G.find((g) => g.members.includes(lockedM));
  await ensureCard(page, lockedG.id);
  ok(await page.isDisabled(btn(lockedM.fid, 'trash')) && (await page.textContent(`${rowSel(lockedM.fid)} .lock`)).includes(lockedM.lock_reason), 'locked member: Trash disabled, reason shown');

  // bulk actions
  await page.click('[data-bulk="clear"]');
  ok(await barNow(page) === barText(0, 0) && await page.isDisabled('#move'), 'Clear all choices -> 0 files, button disabled');
  await page.click('[data-bulk="exact"]');
  const ex = suggested((g) => g.type === 'identical' || g.type === 'same');
  ok(await barNow(page) === barText(ex.n, ex.b), `Accept exact duplicates -> ${barText(ex.n, ex.b)}`);
  await page.click('[data-bulk="all"]');
  ok(await barNow(page) === barText(sug.n, sug.b), 'Accept all suggestions -> all suggestions');

  // filters and sort
  await page.click('#f-kind [data-k="pdf"]');
  ok(await showing(page) === expShowing(countGroups((g) => g.members.some((m) => m.kind === 'pdf'))), 'kind filter: PDFs (mixed groups included)');
  await page.click('#f-kind [data-k="image"]');
  ok(await showing(page) === expShowing(countGroups((g) => g.members.some((m) => m.kind === 'image'))), 'kind filter: Photos');
  await page.click('#f-kind [data-k="all"]');
  await page.click('.chip[data-t="burst"]');
  ok(await showing(page) === expShowing(countGroups((g) => g.type === 'burst')), 'type chip: Burst');
  await page.click('.chip[data-t="related"]');
  ok(await showing(page) === expShowing(countGroups((g) => g.type === 'burst' || g.type === 'related')), 'type chips multi-select: Burst + Related');
  ok(await page.$$eval('.card', (cs) => cs.every((c) => c.dataset.type === 'burst' || c.dataset.type === 'related')), 'only chosen types rendered');
  await page.click('.chip[data-t="burst"]'); await page.click('.chip[data-t="related"]');
  await page.fill('#f-min', '95');
  ok(await showing(page) === expShowing(countGroups((g) => g.min_score == null || g.min_score >= 95)), 'minimum score filter (null never hidden)');
  ok(await page.textContent('#barinfo').then((t) => /hidden by the filters/.test(t)), 'bar notes chosen files hidden by filters');
  await page.fill('#f-min', '');
  await setFolder(page, 'ACME');
  ok(await showing(page) === expShowing(countGroups((g) => g.members.some((m) => m.folder.toLowerCase().includes('acme')))), 'folder filter, case-insensitive');
  await setFolder(page, 'no such folder zzz');
  ok(await page.isVisible('#empty') && await showing(page) === expShowing(0), 'empty state when nothing matches');
  await page.click('#reset');
  ok(await showing(page) === expShowing(G.length), 'Reset filters');
  await page.selectOption('#f-sort', 'count');
  const big = [...G].sort((a, b) => b.members.length - a.members.length)[0];
  ok(await page.getAttribute('.card >> nth=0', 'data-gid') === String(big.id), 'sort Most files puts the largest group first');
  const bigCard = page.locator(`.card[data-gid="${big.id}"]`);
  ok(await bigCard.locator('.row').count() === 50, 'large group renders 50 rows first');
  ok((await bigCard.locator('button.more').textContent()) === `Show ${big.members.length - 50} more`, '"Show N more" label');
  await bigCard.locator('button.more').click();
  ok(await bigCard.locator('.row').count() === big.members.length && !(await bigCard.locator('button.more').isVisible()), '"Show N more" renders the rest');
  await page.selectOption('#f-sort', 'id');
  ok((await page.textContent('.card >> nth=0 >> .gnum')) === 'Group 1', 'sort Group number');
  await page.selectOption('#f-sort', 'space');
  const firstBySpace = [...G].sort((a, b) => b.space - a.space || a.id - b.id)[0];
  ok(await page.getAttribute('.card >> nth=0', 'data-gid') === String(firstBySpace.id), 'default sort: space to free, descending');

  // lazy loading
  ok(await page.locator('.card').count() === 30, 'filter change resets to one chunk');
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await page.waitForFunction(() => document.querySelectorAll('.card').length > 30);
  ok(true, 'scrolling to the end loads more groups (IntersectionObserver)');
  for (let i = 0; i < 10 && (await page.locator('.card').count()) < G.length; i++) {
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight)); await page.waitForTimeout(100);
  }
  ok(await page.locator('.card').count() === G.length && await page.isHidden('#sentinel'), 'all groups load eventually; sentinel hidden at the end');
  await page.evaluate(() => window.scrollTo(0, 0));

  // lightbox
  await page.click('.chip[data-t="burst"]');
  const burstGid = Number(await page.getAttribute('.card >> nth=0', 'data-gid'));
  const burst = G.find((g) => g.id === burstGid);
  await page.click('.card >> nth=0 >> .row >> nth=0 >> .thumb');
  await page.waitForSelector('#lb[open]');
  ok((await page.getAttribute('#lb-img', 'src')).startsWith(`/view/${burst.members[0].fid}?token=${TOKEN}`), 'lightbox shows /view/<fid> with token');
  await page.waitForFunction(() => document.getElementById('lb-img').complete && document.getElementById('lb-img').naturalWidth > 0);
  ok(await page.isVisible('#lb-crops') && (await page.getAttribute('#lb-sharp', 'src')).includes(`/crop/${burst.members[0].fid}/sharp`), 'lightbox shows sharpest/blurriest crops');
  ok((await page.textContent('#lb-crops')).includes('Sharpest area (100%)') && (await page.textContent('#lb-crops')).includes('Blurriest area (100%)'), 'crop captions');
  await page.waitForTimeout(150);
  await shot(page, 'server-desktop-light-lightbox');
  for (let i = 0; i < 8; i++) await page.keyboard.press('Tab');
  ok(await page.evaluate(() => document.getElementById('lb').contains(document.activeElement)), 'focus stays trapped in the lightbox');
  await page.keyboard.press('ArrowRight');
  ok((await page.textContent('#lb-c')) === `2 of ${burst.members.length}`, 'arrow key steps through the group');
  await page.keyboard.press('Escape');
  ok(await page.$('#lb[open]') === null, 'Esc closes the lightbox');
  await page.click('.card >> nth=0 >> .row >> nth=1 >> .thumb');
  await page.waitForSelector('#lb[open]');
  await page.mouse.click(5, 5);
  ok(await page.$('#lb[open]') === null, 'click outside closes the lightbox');
  await page.click('.chip[data-t="burst"]');
  await page.evaluate(() => window.scrollTo(0, 0));

  // open / show in folder
  await page.click(`${rowSel(otherFid)} button:text-is("Open")`);
  await page.click(`${rowSel(otherFid)} button:text-is("Show in folder")`);
  await page.waitForTimeout(150);
  ok(st.opens.length === 2 && st.opens[0].fid === Number(otherFid) && st.opens[0].reveal === false && st.opens[1].reveal === true, 'Open / Show in folder POST /api/open {fid, reveal}');

  // move flow
  await page.click('[data-bulk="clear"]');
  const agm = G.find((g) => g.members.some((m) => m.name === 'AGM2023.pdf'));
  const poorG = G.find((g) => g.type === 'poor');
  const [a1, a2] = [agm.members[1], agm.members[2]];
  const p0 = poorG.members[0];
  await ensureCard(page, agm.id);
  await page.click(btn(otherFid, 'trash'));
  await page.click(btn(a1.fid, 'trash'));
  await page.click(btn(a2.fid, 'trash'));
  await ensureCard(page, poorG.id);
  await page.click(btn(p0.fid, 'trash'));
  const chosen = [evilG.members[1], a1, a2, p0];
  const bytes = chosen.reduce((s, m) => s + m.size, 0);
  ok(await barNow(page) === barText(4, bytes), 'bar reflects manual choices before moving');
  const dest = 'E:\\_duplicates_review\\USB stick backup\\Docs\\Board\\AGM2023.pdf';
  st.responder = (ids) => ids.map((fid) => (fid === a1.fid ? { fid, ok: false, reason: 'The file changed after the scan.' }
    : fid === a2.fid ? { fid, ok: true, action: 'review', dest } : { fid, ok: true, action: 'recycle', dest: '' }));
  st.moveDelay = 600;
  await page.click('#move');
  await page.waitForSelector('#ask[open]');
  const dlg = await page.textContent('#ask');
  ok(dlg.includes('4 files') && dlg.includes(human(bytes)), 'confirm dialog shows count and total size');
  ok(dlg.includes('Files on USB or network drives go to a _duplicates_review folder on that drive instead of the Recycle Bin.'), 'confirm dialog shows USB/network note');
  ok(dlg.includes('1 of these has no other copy.'), 'confirm dialog warns about poor shots with no other copy');
  await shot(page, 'server-desktop-light-confirm');
  await page.click('#ask-ok');
  await page.waitForFunction(() => /^Moving/.test(document.getElementById('move').textContent));
  ok(await page.isDisabled('#move'), 'move button disabled and shows progress while moving');
  await page.waitForFunction(() => !/^Moving/.test(document.getElementById('move').textContent));
  const lastToast = () => page.textContent('.toast >> nth=-1');
  ok(st.moves.length === 1 && JSON.stringify([...st.moves[0].ids].sort()) === JSON.stringify(chosen.map((m) => m.fid).sort()), 'POST /api/move sends the chosen ids');
  ok((await page.textContent(`${rowSel(otherFid)} .note`)) === 'Moved to Recycle Bin' && await page.$eval(rowSel(otherFid), (r) => r.classList.contains('moved')), 'ok/recycle row greyed "Moved to Recycle Bin"');
  ok((await page.textContent(`${rowSel(a2.fid)} .note`)) === 'Moved to ' + dest, 'ok/review row shows "Moved to <dest>"');
  ok((await page.textContent(`${rowSel(a1.fid)} .note`)).includes('The file changed after the scan.') && await pressed(page, a1.fid, 'trash') === 'true', 'refused row shows reason, choice unchanged');
  ok(await barNow(page) === barText(1, a1.size), 'counts exclude moved files');
  ok((await lastToast()).includes('Moved 2 files to the Recycle Bin') && (await lastToast()).includes('1 file to a _duplicates_review folder') && (await lastToast()).includes('1 file not moved'), 'toast summary');
  ok(await page.isDisabled(btn(a2.fid, 'keep')), 'moved rows have no active toggles');
  await page.click(btn(evilFid, 'trash'));
  ok(await pressed(page, evilFid, 'keep') === 'true', 'keep-one rule counts only unmoved members');
  await page.locator(`.card[data-gid="${agm.id}"]`).scrollIntoViewIfNeeded();
  await shot(page, 'server-desktop-light-after-move');
  ok(await page.evaluate(() => window.__pwned === undefined), 'still no hostile handler executed');

  // server gone
  st.openFail = true;
  await page.click(`${rowSel(a1.fid)} button:text-is("Open")`);
  await page.waitForFunction((t) => [...document.querySelectorAll('.toast')].some((x) => x.textContent.includes(t)), GONE);
  ok(await page.isDisabled('#move'), 'failed request -> "isn\'t running" toast, move disabled');
  ok(st.external.length === 0 && st.badToken.length === 0, 'server: no external requests; every request carried the token');
  await ctx.close();

  // quit
  const q = await serverPage(browser, { width: 1400, height: 900, scheme: 'light', errors, label: 'server-quit' });
  await q.page.click('#quit');
  await q.page.waitForSelector('#ask[open]');
  await q.page.click('#ask-ok');
  await q.page.waitForSelector('#stopped:not([hidden])');
  ok(q.st.quits === 1 && (await q.page.textContent('#stopped')).includes('DupeFinder has stopped. You can close this tab.'), 'Quit -> POST /api/quit, stopped overlay');
  await shot(q.page, 'server-quit-overlay');
  await q.ctx.close();
}

async function themedShots(browser, errors) {
  for (const scheme of ['light', 'dark']) {
    for (const [w, h, tag] of [[1400, 900, 'desktop'], [390, 844, 'phone']]) {
      const { page, ctx, st } = await serverPage(browser, { width: w, height: h, scheme, errors, label: `server-${tag}-${scheme}` });
      if (tag === 'phone') {
        await page.click('#skip-sum');
        ok(await noHScroll(page), `server ${tag} ${scheme}: no horizontal scroll (skipped panel open)`);
        await page.click('#skip-sum');
      }
      if (scheme === 'dark' || tag === 'phone') await shot(page, `server-${tag}-${scheme}-top`);
      // a burst group and the big group further down
      await page.click('.chip[data-t="burst"]');
      await page.evaluate(() => document.querySelector('.card').scrollIntoView());
      await page.waitForTimeout(250);
      await shot(page, `server-${tag}-${scheme}-burst`);
      await page.click('.chip[data-t="burst"]');
      await page.click('.chip[data-t="related"]');
      await page.evaluate(() => document.querySelector('.card').scrollIntoView());
      await page.waitForTimeout(200);
      if (scheme === 'light') await shot(page, `server-${tag}-${scheme}-related`);
      await page.click('.chip[data-t="related"]');
      await page.selectOption('#f-sort', 'count');
      await page.locator('.card >> nth=0 >> button.more').click();
      for (let i = 0; i < 6; i++) { await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight)); await page.waitForTimeout(60); }
      ok(await noHScroll(page), `server ${tag} ${scheme}: no horizontal scroll with all groups and rows rendered`);
      if (tag === 'phone') {
        const evilG = G.find((g) => g.members.some((m) => m.name === EVIL));
        await page.locator(`.card[data-gid="${evilG.id}"]`).scrollIntoViewIfNeeded();
        await page.evaluate(() => window.scrollBy(0, -60));
        await shot(page, `server-${tag}-${scheme}-hostile`);
        const longG = G.find((g) => g.members.some((m) => m.name === 'deep_copy.jpg'));
        const card = page.locator(`.card[data-gid="${longG.id}"]`);
        ok(await card.evaluate((c) => c.scrollWidth <= c.clientWidth + 1), `server ${tag} ${scheme}: long path truncates inside the card`);
        await page.locator(`.card[data-gid="${G.find((g) => g.type === 'burst').id}"] .row >> nth=0 >> .thumb`).click();
        await page.waitForSelector('#lb[open]');
        await page.waitForTimeout(200);
        await shot(page, `server-${tag}-${scheme}-lightbox`);
        await page.keyboard.press('Escape');
      } else if (scheme === 'dark') {
        await page.selectOption('#f-sort', 'space');
        await page.click(`.row >> nth=1 >> button[data-choice="keep"]`);
        await page.click('#move');
        await page.waitForSelector('#ask[open]');
        await shot(page, `server-${tag}-${scheme}-confirm`);
        await page.click('#ask button[value="cancel"]');
        await page.locator('.card >> nth=0 >> .row >> nth=0 >> .thumb').click();
        await page.waitForSelector('#lb[open]');
        await page.waitForTimeout(200);
        await shot(page, `server-${tag}-${scheme}-lightbox`);
        await page.keyboard.press('Escape');
      }
      ok(st.external.length === 0, `server ${tag} ${scheme}: no external requests`);
      await ctx.close();
    }
  }
}

/* ---------- static mode ---------- */
async function staticChecks(browser, errors) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'df-static-'));
  const files = path.join(dir, 'files');
  fs.mkdirSync(files);
  const toUrl = (p) => 'file://' + p.split(path.sep).map(encodeURIComponent).join('/');
  const data = JSON.parse(JSON.stringify({ ...SAMPLE, mode: 'static' }));
  delete data.token;
  // Point thumbnails and photos at real local files (the sample's C:\ URLs don't exist here).
  // HEIC photos get undecodable bytes, so the "can't display" note is exercised.
  for (const g of data.groups) for (const m of g.members) {
    if (m.thumb) { const f = path.join(files, `t${m.fid}.svg`); fs.writeFileSync(f, m.kind === 'pdf' ? pdfSvg(200, 256) : photoSvg(m.fid, 256, 192)); m.thumb = toUrl(f); }
    if (m.kind === 'image') {
      const heic = m.ext === 'heic';
      const f = path.join(files, `v${m.fid}.${heic ? 'heic' : 'svg'}`);
      fs.writeFileSync(f, heic ? Buffer.from('ftypheic\0\0not really a heic') : photoSvg(m.fid, 1600, 1200));
      m.file_url = toUrl(f);
    }
  }
  const file = path.join(dir, 'report.html');
  fs.writeFileSync(file, inject(data));
  for (const [w, h, scheme, tag] of [[1400, 900, 'light', 'desktop'], [1400, 900, 'dark', 'desktop'], [390, 844, 'light', 'phone'], [390, 844, 'dark', 'phone']]) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h }, colorScheme: scheme, locale: 'en-US' });
    const page = await ctx.newPage();
    const ext = [];
    await ctx.route(/^https?:/, (r) => { ext.push(r.request().url()); r.abort(); });
    watch(page, `static-${tag}-${scheme}`, errors);
    await page.goto(toUrl(file));
    await page.waitForSelector('.card');
    if (tag === 'desktop' && scheme === 'light') {
      ok(await page.locator('.tg, .seg.tg button').count() === 0, 'static: no Keep/Trash toggles');
      ok(await page.isHidden('#bar') && await page.isHidden('#move') && await page.isHidden('#bulk') && await page.isHidden('#quit'), 'static: no move button, bulk actions or Quit');
      ok(await page.locator('.sugg-k').count() > 0 && await page.locator('.sugg-t').count() > 0 && (await page.textContent('.sugg >> nth=0')).startsWith('Suggested: '), 'static: read-only "Suggested: Keep/Trash" badges');
      ok(await page.isVisible('#ro-note'), 'static: read-only note shown');
      const href = await page.getAttribute('.row >> nth=0 >> a.btn:text-is("Open")', 'href');
      ok(/^file:\/\//.test(href || ''), 'static: Open is a file:// link');
      const evilG = G.find((g) => g.members.some((m) => m.name === EVIL));
      await ensureCard(page, evilG.id);
      ok(await page.textContent(`${rowSel(evilG.members[0].fid)} .name`) === EVIL && await page.evaluate(() => window.__pwned === undefined), 'static: hostile name is text, nothing executed');
      ok(await page.locator('.card').count() === 30, 'static: lazy chunk of 30');
      await page.waitForFunction(() => [...document.querySelectorAll('.thumb img')].slice(0, 4).every((i) => i.complete && i.naturalWidth > 0));
      ok(true, 'static: thumbnails load from file:// URLs');
      await page.selectOption('#f-sort', 'id');
      const anyBurst = data.groups.find((g) => g.type === 'burst');
      await page.click('.chip[data-t="burst"]');
      await page.click(`.card[data-gid="${anyBurst.id}"] .row >> nth=0 >> .thumb`);
      await page.waitForSelector('#lb[open]');
      await page.waitForFunction(() => document.getElementById('lb-img').complete && document.getElementById('lb-img').naturalWidth > 0);
      ok((await page.getAttribute('#lb-img', 'src')).startsWith('file://') && await page.isHidden('#lb-crops'), 'static: lightbox shows file_url, no crops');
      await page.keyboard.press('Escape');
      await page.click('.chip[data-t="burst"]');
      const heicG = data.groups.find((g) => g.members.some((m) => m.ext === 'heic'));
      const heicM = heicG.members.find((m) => m.ext === 'heic');
      await setFolder(page, heicM.folder);
      await ensureCard(page, heicG.id);
      await page.click(`${rowSel(heicM.fid)} .thumb`);
      await page.waitForSelector('#lb-note:not([hidden])');
      ok((await page.textContent('#lb-note')).includes('HEIC'), 'static: lightbox note when the photo can\'t display (HEIC)');
      await page.waitForTimeout(100);
      await shot(page, 'static-desktop-light-heic-note');
      await page.keyboard.press('Escape');
      await page.click('#f-reset');
      ok(await page.isHidden('#f-reset') && await page.textContent('#showing') === expShowing(G.length), 'filter-bar Reset button clears filters and hides itself');
      await page.evaluate(() => window.scrollTo(0, 0));
    }
    ok(await noHScroll(page), `static ${tag} ${scheme}: no horizontal scroll`);
    await shot(page, `static-${tag}-${scheme}-top`);
    await page.evaluate(() => document.querySelector('.card').scrollIntoView());
    await page.waitForTimeout(150);
    await shot(page, `static-${tag}-${scheme}-groups`);
    ok(ext.length === 0, `static ${tag} ${scheme}: no network requests`);
    await ctx.close();
  }
  fs.rmSync(dir, { recursive: true, force: true });
}

(async () => {
  const errors = [];
  const browser = await chromium.launch();
  let failed = null;
  try {
    ok(TEMPLATE.split('__DF_DATA__').length === 2 && TEMPLATE.includes('window.DF_DATA = __DF_DATA__;'), 'template has exactly one window.DF_DATA = __DF_DATA__;');
    ok(!/(src|href)\s*=\s*["']?(https?:)?\/\//i.test(TEMPLATE) && !/@import|url\(\s*["']?https?:/i.test(TEMPLATE), 'template references nothing on the internet');
    ok(!/\.innerHTML|outerHTML|insertAdjacentHTML|document\.write/.test(TEMPLATE), 'template never uses innerHTML/insertAdjacentHTML/document.write');
    const types = new Set(G.map((g) => g.type));
    ok(['identical', 'same', 'similar', 'burst', 'related', 'poor'].every((t) => types.has(t)) && G.length >= 120 && G.some((g) => g.members.length >= 60), 'sample covers every group type, 120+ groups and a 60+ member group');
    await serverChecks(browser, errors);
    await themedShots(browser, errors);
    await staticChecks(browser, errors);
    ok(errors.length === 0, 'no console errors or uncaught exceptions in any run');
  } catch (e) {
    failed = e;
  }
  await browser.close();
  for (const p of passed) console.log('  ok  ' + p);
  if (errors.length) console.log(errors.map((e) => '  !!  ' + e).join('\n'));
  if (failed) { console.error('\n' + (failed.stack || failed)); process.exit(1); }
  console.log(`\nAll ${passed.length} checks passed. Screenshots: ${SHOTS}`);
})();
