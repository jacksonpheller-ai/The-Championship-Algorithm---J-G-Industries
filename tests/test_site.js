// Dashboard checks with playwright (node tests/test_site.js [outdir]).
// Renders jghedge/site/template.html the way jghedge/site/__init__.py does, against three slates:
//   fixture  tests/fixtures/latest_sample.json (full data contract: policy, night, pickem, fliff, lines)
//   real     docs/data/latest.json (whatever the nightly job last produced)
//   empty    {} (no slate at all)
// and asserts: no console/page errors, no horizontal overflow at 390px on every tab, no bankroll input
// (tonight's amount is the only stake input), the page's exact slip value equals the engine's for every
// published slip (including the void rule), one pick per player, how tonight's amount is split
// (PrizePicks minimum only when it is at most half the amount, game bets by the night's call, nothing
// moved past our number or info-only), line re-pricing, and the old #desk / #pickem links.
// Writes light/dark screenshots of Tonight and Player props at 1280px into outdir.
const fs = require("fs"), path = require("path");
const { chromium } = require("playwright");
const ROOT = path.resolve(__dirname, "..");
const OUT = process.argv[2] || path.join(__dirname, "..", "dist", "shots");
fs.mkdirSync(OUT, { recursive: true });
const readJson = (p) => (fs.existsSync(p) ? JSON.parse(fs.readFileSync(p, "utf8")) : {});
const template = fs.readFileSync(path.join(ROOT, "jghedge/site/template.html"), "utf8");
// live: false keeps a test page off the network; the live-line and self-update tests pass { live: true } and mock the two hosts
function render(slate, extra) {
  const data = Object.assign({ slate, ledger: readJson(path.join(ROOT, "docs/data/ledger.json")), backtest: readJson(path.join(ROOT, "docs/data/backtest.json")),
    replay: readJson(path.join(ROOT, "docs/data/replay_open.json")), results: {}, shared: null, repo: "https://github.com/example/repo", pages_url: "", raw_url: "", live: false }, extra || {});
  const blob = JSON.stringify(data).replace(/<\//g, "<\\/");
  return "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body>" + template.replace('"__JG_DATA__"', blob) + "</body></html>";
}
let failures = 0, checks = 0;
function assert(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL:", msg); } else console.log("ok:", msg); }
const TABS = ["tonight", "games", "props", "record", "method"];
// The page uses the device clock (games that have tipped are dropped, old lines are flagged), so each page is
// opened at a fixed time: by default the moment its slate was priced.
async function openAt(browser, opts, slate, offsetHours) {
  const page = await browser.newPage(opts);
  if (slate && slate.generated) await page.clock.setFixedTime(new Date(new Date(slate.generated).getTime() + (offsetHours || 0) * 3600 * 1000));
  return page;
}

(async () => {
  const fixture = readJson(path.join(ROOT, "tests/fixtures/latest_sample.json"));
  const real = readJson(path.join(ROOT, "docs/data/latest.json"));
  const pages = { fixture: render(fixture), real: render(real), empty: render({}) };
  const slates = { fixture, real, empty: {} };
  for (const k of Object.keys(pages)) fs.writeFileSync(path.join(OUT, k + ".html"), pages[k]);
  const browser = await chromium.launch();
  for (const name of Object.keys(pages)) {
    const file = "file://" + path.join(OUT, name + ".html");
    const errs = [];
    const phone = await openAt(browser, { viewport: { width: 390, height: 844 }, colorScheme: "dark" }, slates[name]);
    phone.on("pageerror", (e) => errs.push(String(e))); phone.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
    for (const v of TABS) {
      await phone.goto(file + "#" + v); await phone.waitForTimeout(250);
      const r = await phone.evaluate(() => {
        const W = document.documentElement.clientWidth, out = [];
        document.querySelectorAll("body *").forEach((el) => { const rc = el.getBoundingClientRect();
          if (rc.right > W + 1 && el.offsetParent !== null) { let a = el, inScroll = false; while ((a = a.parentElement)) { const s = getComputedStyle(a); if (s.overflowX === "auto" || s.overflowX === "scroll" || s.overflowX === "hidden") { inScroll = true; break; } }
            if (!inScroll) out.push(el.tagName + "." + el.className + " " + Math.round(rc.right)); } });
        return { sw: document.documentElement.scrollWidth, cw: W, out: out.slice(0, 6) };
      });
      assert(r.sw <= r.cw, name + " #" + v + " no horizontal overflow at 390px (scrollWidth " + r.sw + ", offenders " + JSON.stringify(r.out) + ")");
    }
    const ui = await phone.evaluate(() => ({ bankroll: !!document.getElementById("bankroll") || /bankroll/i.test((document.querySelector("label[for=amount]") || {}).textContent || ""),
      amount: !!document.getElementById("amount"), undef: /undefined|NaN/.test(document.getElementById("view-tonight").textContent) }));
    assert(!ui.bankroll && ui.amount, name + ": tonight's amount is the only stake input (no bankroll box)");
    assert(!ui.undef, name + ": Tonight has no 'undefined' or 'NaN' text");
    assert(errs.length === 0, name + " no console/page errors: " + JSON.stringify(errs));
    await phone.close();
    for (const scheme of ["light", "dark"]) {
      const p = await openAt(browser, { viewport: { width: 1280, height: 900 }, colorScheme: scheme }, slates[name]);
      if (name !== "empty") {
        await p.goto(file + "#tonight"); await p.waitForTimeout(500);
        await p.screenshot({ path: path.join(OUT, name + "_tonight_" + scheme + ".png"), fullPage: true });
        await p.goto(file + "#props"); await p.waitForTimeout(400);
        await p.screenshot({ path: path.join(OUT, name + "_props_" + scheme + ".png"), fullPage: true });
      }
      await p.close();
    }
  }
  // ---- functional checks against the fixture
  const p = await openAt(browser, { viewport: { width: 1280, height: 900 } }, fixture);
  const perr = []; p.on("pageerror", (e) => perr.push(String(e)));
  await p.goto("file://" + path.join(OUT, "fixture.html") + "#tonight"); await p.waitForTimeout(400);
  assert(await p.evaluate(() => document.getElementById("answer").dataset.verdict) === String(fixture.night.verdict).toUpperCase(), "answer carries the night's call (" + fixture.night.verdict + ")");
  // how tonight's amount is split
  const byId = {}; fixture.bets.forEach((b) => { byId[b.id] = b; });
  const moved = new Set((fixture.lines.moved || []).filter((m) => m.still_ok === false).map((m) => m.bet_id));
  const factor = { BET: 1, LIGHT: 0.5, PASS: 0 }[String(fixture.night.verdict).toUpperCase()];
  for (const A of [3, 8, 10, 40, 250]) {
    const pl = await p.evaluate((a) => { const x = window.JG_DEBUG.plan(a); return { total: x.total, games: x.games, pp: x.pp ? x.pp.stake : 0, rows: x.rows.map((r) => ({ id: r.id, stake: r.stake, legs: r.legs.map((l) => l.id) })), slip: !!x.slip }; }, A);
    assert(pl.total <= A + 1e-9, "$" + A + ": plan stakes no more than the amount (" + pl.total + ")");
    assert(pl.pp === 0 || (pl.pp === 5 && 5 <= A / 2 + 1e-9), "$" + A + ": PrizePicks gets the $5 minimum only when it is at most half the amount (pp " + pl.pp + ")");
    if (pl.slip && A >= 10) assert(pl.pp === 5, "$" + A + ": a slip with an edge gets the minimum entry");
    assert(pl.games <= (A - pl.pp) * factor + 1e-9, "$" + A + ": game bets use at most " + factor + " of what is left (" + pl.games + ")");
    assert(pl.rows.every((r) => Math.abs(r.stake / 0.25 - Math.round(r.stake / 0.25)) < 1e-9), "$" + A + ": game stakes are whole Fliff-minimum steps");
    assert(pl.rows.every((r) => r.legs.every((id) => byId[id] && byId[id].kind === "total" && !byId[id].info_only && !moved.has(id))), "$" + A + ": only totals, nothing info-only or moved past our number");
  }
  assert(await p.evaluate(() => window.JG_DEBUG.plan(0).total === 0), "$0: nothing staked");
  await p.fill("#amount", "0"); await p.waitForTimeout(100);
  assert(/Enter tonight's amount/.test(await p.evaluate(() => document.getElementById("tHead").textContent)), "amount 0 asks for an amount");
  await p.fill("#amount", "20"); await p.waitForTimeout(100);
  assert(await p.evaluate(() => localStorage.getItem("jg.amount")) === "20", "tonight's amount is remembered on this device");
  const planCards = await p.evaluate(() => document.querySelectorAll("#plan .bet").length);
  const expect = await p.evaluate(() => { const x = window.JG_DEBUG.plan(20); return x.rows.length + (x.pp ? 1 : 0); });
  assert(planCards === expect, "Tonight shows one card per planned bet (" + planCards + ")");
  // the slip on Tonight is checked against the app in place: a much harder line turns it into a skip, reset restores it
  {
    const slipIds = await p.evaluate(() => { const x = window.JG_DEBUG.plan(20); return x.pp ? x.pp.slip.legs : []; });
    if (slipIds.length) {
      assert(await p.evaluate(() => document.querySelectorAll("#plan .bet.pp input[data-slipline]").length) === slipIds.length, "the slip card has a line box for every pick (" + slipIds.length + ")");
      const leg = fixture.pickem.legs.find((l) => l.id === slipIds[0]), worse = leg.side === "more" ? leg.line + 3 : leg.line - 3;
      const sel = "#plan input[data-slipline='" + leg.id + "']";
      await p.fill(sel, String(worse)); await p.dispatchEvent(sel, "change"); await p.waitForTimeout(150);
      const after = await p.evaluate(() => { const x = window.JG_DEBUG.plan(20); return { skip: x.pp.skip, stake: x.pp.stake, total: x.total, off: !!document.querySelector("#plan .bet.pp.off"), acts: document.getElementById("planActs").hidden, txt: document.querySelector("#plan .bet.pp").textContent }; });
      const calc = await p.evaluate(() => window.JG_DEBUG.slipNow(window.JG_DEBUG.plan(20).pp.slip));
      assert(calc.edited && (calc.good ? !after.skip : after.skip && after.stake === 0 && after.off), "a typed line re-prices the slip; a slip without an edge is marked Skip and stakes nothing (" + (calc.good ? "still good" : "skip") + ")");
      await p.click("#slipReset"); await p.waitForTimeout(150);
      const back = await p.evaluate(() => { const x = window.JG_DEBUG.plan(20); return { skip: x.pp.skip, edited: x.pp.calc.edited, stake: x.pp.stake }; });
      assert(!back.edited && !back.skip && back.stake === 5, "Reset lines restores the suggested slip");
    }
  }
  // the edge map lists every game with a posted total
  const nTotals = new Set(fixture.bets.filter((b) => b.kind === "total").map((b) => b.game_id)).size;
  assert(await p.evaluate(() => document.querySelectorAll("#mapGames .erow").length) === nTotals, "edge map has one row per game with a total (" + nTotals + ")");
  const statuses = await p.evaluate(() => Array.from(document.querySelectorAll("#mapGames .erow")).map((r) => r.className));
  assert(statuses.every((c) => /s-(edge|close|none)/.test(c)), "every game row is marked edge, close or none");
  // games tab: details open the numbers
  await p.goto("file://" + path.join(OUT, "fixture.html") + "#games"); await p.waitForTimeout(300);
  await p.click("#gameRows .whybtn[data-why]"); await p.waitForTimeout(100);
  assert(/How we got our total/.test(await p.evaluate(() => document.getElementById("gameRows").textContent)), "game Details shows how we got our total");
  // props: tick a suggested slip's legs into the builder
  await p.goto("file://" + path.join(OUT, "fixture.html") + "#props"); await p.waitForTimeout(300);
  await p.click("#pkMore").catch(() => {});
  const legIds = fixture.pickem.slips[0].legs;
  for (const id of legIds) await p.check("input[data-pk='" + id + "']");
  const outTxt = await p.evaluate(() => document.getElementById("pkCustomOut").textContent);
  assert(/Back per \$1/.test(outTxt) && /All \d hit/.test(outTxt), "slip builder shows the chance all hit and the return per $1");
  const legs = fixture.pickem.legs, dupPlayer = legs.find((l) => legs.filter((x) => x.player_id === l.player_id).length > 1);
  if (dupPlayer) {
    await p.click("#pkClear");
    await p.check("input[data-pk='" + dupPlayer.id + "']");
    const other = legs.find((l) => l.player_id === dupPlayer.player_id && l.id !== dupPlayer.id);
    assert(await p.evaluate((id) => document.querySelector("input[data-pk='" + id + "']").disabled, other.id), "one pick per player: a second pick for " + dupPlayer.player + " is disabled");
  }
  // typing PrizePicks' line re-prices the pick from its table
  const qLeg = legs.find((l) => (l.q || []).length > 2);
  if (qLeg) {
    const before = await p.evaluate((id) => document.getElementById("p-" + id).querySelector(".cm .lab").textContent, qLeg.id);
    const easier = qLeg.side === "more" ? qLeg.line - 1 : qLeg.line + 1;
    await p.fill("input[data-pkline='" + qLeg.id + "']", String(easier)); await p.dispatchEvent("input[data-pkline='" + qLeg.id + "']", "change"); await p.waitForTimeout(100);
    const after = await p.evaluate((id) => document.getElementById("p-" + id).querySelector(".cm .lab").textContent, qLeg.id);
    assert(parseFloat(after) > parseFloat(before), "an easier PrizePicks line raises the pick's chance (" + before + " → " + after + ")");
  }
  // old links still land somewhere sensible
  await p.goto("file://" + path.join(OUT, "fixture.html") + "#desk"); await p.waitForTimeout(150);
  assert(await p.evaluate(() => !document.getElementById("view-tonight").hidden), "#desk opens Tonight");
  await p.goto("file://" + path.join(OUT, "fixture.html") + "#pickem"); await p.waitForTimeout(150);
  assert(await p.evaluate(() => !document.getElementById("view-props").hidden), "#pickem opens Player props");
  assert(perr.length === 0, "no page errors during interaction: " + JSON.stringify(perr));
  await p.close();
  // ---- a total with an edge on a PASS night: named as an edge, not staked, and the page does not claim there is none
  {
    const pe = JSON.parse(JSON.stringify(fixture)); pe.night.verdict = "PASS"; pe.lines.moved = [];
    pe.bets.forEach((x) => { if (x.id === "401909940-to-u") Object.assign(x, { eligible: true, shrunk_ev: 0.028, p_shrunk: 0.54, ev: 0.056 }); if (x.id === "401909940-to-o") Object.assign(x, { eligible: false, shrunk_ev: -0.12, p_shrunk: 0.46 }); });
    fs.writeFileSync(path.join(OUT, "passedge.html"), render(pe));
    const q = await openAt(browser, { viewport: { width: 1280, height: 900 } }, pe);
    await q.goto("file://" + path.join(OUT, "passedge.html") + "#tonight"); await q.waitForTimeout(300);
    const t = await q.evaluate(() => ({ sub: document.getElementById("tSub").textContent, rows: window.JG_DEBUG.plan(50).rows.length,
      edge: Array.from(document.querySelectorAll("#mapGames .erow.s-edge")).map((r) => r.querySelector(".act").textContent) }));
    assert(t.rows === 0, "PASS night with an edge: no game bets staked");
    assert(/passes on games/.test(t.sub) && !/None of tonight's/.test(t.sub), "PASS night with an edge: the summary names the edge instead of saying there is none");
    assert(t.edge.length === 1 && /desk passes on the night/.test(t.edge[0]) && !/^Bet /.test(t.edge[0]), "PASS night with an edge: the row is tagged Edge and explains the pass");
    await q.close();
  }
  // ---- the clock: lines priced hours ago are flagged; once games tip nothing on them is offered
  {
    const late = await openAt(browser, { viewport: { width: 1280, height: 900 } }, fixture, 5);
    await late.goto("file://" + path.join(OUT, "fixture.html") + "#tonight"); await late.waitForTimeout(300);
    const firstTip = Math.min(...fixture.games.map((g) => Date.parse(g.tip))), fiveH = Date.parse(fixture.generated) + 5 * 3600 * 1000;
    const st = await late.evaluate(() => ({ banner: document.getElementById("fresh").hidden ? "" : document.getElementById("fresh").textContent, kicker: document.getElementById("tDate").textContent,
      started: document.querySelectorAll("#mapGames .erow .tag").length && Array.from(document.querySelectorAll("#mapGames .erow .tag")).filter((t) => t.textContent === "Started").length }));
    assert(/priced 5 h ago/.test(st.kicker), "five hours on, the page says the lines were priced 5 h ago (" + st.kicker.slice(-60) + ")");
    const nStarted = fixture.games.filter((g) => Date.parse(g.tip) <= fiveH).length;
    assert(st.started === nStarted, "games that have tipped are marked Started (" + st.started + " of " + fixture.games.length + ")");
    if (fiveH < Math.max(...fixture.games.map((g) => Date.parse(g.tip)))) assert(/priced 5 h ago/.test(st.banner), "old lines get a warning banner while games remain");
    await late.close();
    const over = await openAt(browser, { viewport: { width: 1280, height: 900 } }, fixture, 72);
    await over.goto("file://" + path.join(OUT, "fixture.html") + "#tonight"); await over.waitForTimeout(300);
    const done = await over.evaluate(() => ({ head: document.getElementById("tHead").textContent, cards: document.querySelectorAll("#plan .bet").length, total: window.JG_DEBUG.plan(100).total,
      picks: document.querySelectorAll("#mapProps .erow").length }));
    assert(done.cards === 0 && done.total === 0 && done.picks === 0 && /have started/.test(done.head), "after every game has tipped nothing is offered (" + done.head + ")");
    await over.close();
  }
  // ---- engine and page price a slip the same way: the page's exact value from the published legs equals the engine's
  {
    const rr = await openAt(browser, { viewport: { width: 1280, height: 900 } }, real);
    await rr.goto("file://" + path.join(OUT, "real.html") + "#props"); await rr.waitForTimeout(300);
    const slips = ((real.pickem || {}).slips || []).filter((x) => x.ev_shrunk != null);
    let worst = 0, worstAll = 0;
    for (const x of slips) {
      const v = await rr.evaluate((sl) => { const D = window.JG_DEBUG, legs = sl.legs.map((id) => D.PKBY[id]), be = D.beFor(sl.type);
        return D.slipValue(legs.map((l) => D.legP(l, true, be)), legs.map((l) => D.legVoid(l)), D.slipKind(sl.type)); }, x);
      worst = Math.max(worst, Math.abs(v.ev - x.ev_shrunk)); worstAll = Math.max(worstAll, Math.abs(v.dist[v.dist.length - 1] - x.p_dist_shrunk[x.p_dist_shrunk.length - 1]));
    }
    if (slips.length) assert(worst < 3e-3 && worstAll < 1e-3, "page and engine agree on the value of all " + slips.length + " published slips (max EV diff " + worst.toFixed(5) + ")");
    // a pick whose player may sit: the void rule, by hand (two picks at 0.6, one 10% to sit -> 0.9 x (3 x 0.36 - 1))
    const hand = await rr.evaluate(() => window.JG_DEBUG.slipValue([0.6, 0.6], [0.1, 0], "power").ev);
    assert(Math.abs(hand - 0.9 * (3 * 0.36 - 1)) < 1e-12, "void rule in the page: a 2-pick with one pick 10% to sit is worth 0.9 x (3 x 0.36 - 1)");
    await rr.close();
  }
  // ---- live lines: the page reads ESPN's totals and re-prices each game the way the engine did
  if (real.live_pricing && (real.games || []).length) {
    const UPD = "https://jg-update.test/docs/";
    fs.writeFileSync(path.join(OUT, "live.html"), render(real, { live: true, update_base: UPD }));
    const scoreboard = (edit) => ({ events: real.games.map((g) => { const m = Object.assign({ total: g.market.total, over_price: g.market.over_price, under_price: g.market.under_price, state: "pre" }, (edit || {})[g.id] || {});
      const od = (x) => (x > 0 ? "+" + x : String(x));
      return { id: g.id, date: g.tip, competitions: [{ status: { type: { state: m.state } }, odds: [{ provider: { name: "DraftKings" }, overUnder: m.total, total: { over: { close: { line: "o" + m.total, odds: od(m.over_price) } }, under: { close: { line: "u" + m.total, odds: od(m.under_price) } } } }] }] }; }) });
    let board = scoreboard(), hits = 0;
    // six hours before the first tip the model carries weight, so a moved line matters
    const firstTip = Math.min(...real.games.map((g) => Date.parse(g.tip)));
    const lp = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const lerr = []; lp.on("pageerror", (e) => lerr.push(String(e)));
    await lp.clock.setFixedTime(new Date(firstTip - 6 * 3600 * 1000));
    await lp.route("https://site.api.espn.com/**", (route) => { hits++; route.fulfill({ status: 200, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(board) }); });
    await lp.route(UPD + "**", (route) => route.fulfill({ status: 404, body: "" }));
    await lp.goto("file://" + path.join(OUT, "live.html") + "#tonight"); await lp.waitForTimeout(700);
    const st0 = await lp.evaluate(() => ({ ok: window.JG_DEBUG.LIVE.ok, n: window.JG_DEBUG.LIVE.n, moved: window.JG_DEBUG.LIVE.moved, kicker: document.getElementById("tDate").textContent }));
    assert(hits >= 1 && st0.ok && st0.n === real.games.length && st0.moved === 0 && /lines live/.test(st0.kicker), "the page reads the live feed and re-prices all " + real.games.length + " games (" + st0.kicker.slice(-70) + ")");
    // maths: the formula agrees with the engine's simulation at the engine's own centre and line, and is sane
    const m0 = await lp.evaluate(() => { const D = window.JG_DEBUG, out = { worst: 0, half: D.liveProbs(228.5, 228.5), whole: D.liveProbs(224, 224), up: D.liveProbs(230.5, 228.5), ppf: D.normPpf(0.975) };
      Object.keys(D.SRV).forEach((id) => { const g = D.GAMES[id.split("-to-")[0]], s0 = D.SRV[id]; if (!g || g.model.t_final == null || s0.line == null) return; const a = D.liveProbs(g.model.t_final, s0.line), js = (id.endsWith("-o") ? a.over : a.under) + 0.5 * a.push; out.worst = Math.max(out.worst, Math.abs(js - s0.p_final)); });
      return out; });
    assert(m0.worst < 0.013, "the page's formula matches the engine's simulated chance for every total (max gap " + m0.worst.toFixed(4) + ")");
    assert(Math.abs(m0.half.over - 0.5) < 0.002 && m0.half.push === 0 && m0.whole.push > 0.01 && Math.abs(m0.whole.over - m0.whole.under) < 0.004 && m0.up.over > 0.53 && Math.abs(m0.ppf - 1.959964) < 1e-5, "an even line is 50/50, a whole-number line can push, a higher centre favours the Over");
    // move one game's total three points up: the Over gets worse, the Under better, and the page says the line moved
    const g0 = real.games[0], before = await lp.evaluate((id) => ({ o: window.JG_DEBUG.BETS[id + "-to-o"].p_final, u: window.JG_DEBUG.BETS[id + "-to-u"].p_final }), g0.id);
    board = scoreboard({ [g0.id]: { total: g0.market.total + 3 } });
    await lp.evaluate(() => { window.JG_DEBUG.LIVE.busy = false; document.dispatchEvent(new Event("visibilitychange")); }); await lp.waitForTimeout(500);
    const after = await lp.evaluate((id) => ({ o: window.JG_DEBUG.BETS[id + "-to-o"], u: window.JG_DEBUG.BETS[id + "-to-u"], moved: window.JG_DEBUG.LIVE.moved, kicker: document.getElementById("tDate").textContent,
      row: Array.from(document.querySelectorAll("#mapGames .erow")).map((r) => r.textContent).find((t) => t.includes("at the model run")) || "" }), g0.id);
    assert(after.o.line === g0.market.total + 3 && after.o.p_final < before.o - 0.01 && after.o.p_final > before.o - 0.04 && after.u.p_final > before.u + 0.01 && after.moved === 1 && /1 moved/.test(after.kicker) && /was /.test(after.row), "a line moved 3 points re-prices both sides (our blend follows the market for 70% of the move, so about 2 points of chance) and is shown as moved (Over " + before.o.toFixed(3) + " -> " + after.o.p_final.toFixed(3) + ")");
    // a live line can take a bet off the plan, never put one on: make the model love an Over, then move the line away
    const off = await lp.evaluate((id) => { const D = window.JG_DEBUG, b = D.BETS[id + "-to-o"]; D.SRV[b.id].eligible = true; b.eligible = false; b.live = true;
      return { off: D.gameStatus(b).tag, plan: D.plan(100).rows.filter((r) => r.id === b.id).length }; }, g0.id);
    assert(off.off === "Moved" && off.plan === 0, "a bet that no longer qualifies at the live line is marked Moved and left out of the plan");
    // a started game is not re-priced
    board = scoreboard({ [g0.id]: { total: g0.market.total - 9, state: "in" } });
    const lineBefore = await lp.evaluate((id) => window.JG_DEBUG.BETS[id + "-to-o"].line, g0.id);
    await lp.evaluate(() => { window.JG_DEBUG.LIVE.busy = false; document.dispatchEvent(new Event("visibilitychange")); }); await lp.waitForTimeout(500);
    assert(await lp.evaluate((id) => window.JG_DEBUG.BETS[id + "-to-o"].line, g0.id) === lineBefore, "a game ESPN reports as under way keeps its last pre-game price");
    assert(lerr.length === 0, "no page errors with live lines: " + JSON.stringify(lerr));
    await lp.close();

    // ---- a newer build: the open page finds it through a few bytes and loads it in place, even from a file
    const later = new Date(Date.parse(real.generated) + 3 * 3600 * 1000).toISOString().replace(".000Z", "+00:00");
    const newer = render(Object.assign({}, real, { generated: later }), { live: true, update_base: UPD });
    const up = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    await up.clock.setFixedTime(new Date(firstTip - 6 * 3600 * 1000));
    await up.route("https://site.api.espn.com/**", (route) => route.fulfill({ status: 200, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(scoreboard()) }));
    let served = { v: real.generated };
    await up.route(UPD + "data/version.json", (route) => route.fulfill({ status: 200, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify({ generated: served.v }) }));
    await up.route(UPD + "index.html", (route) => route.fulfill({ status: 200, contentType: "text/plain", headers: { "access-control-allow-origin": "*" }, body: newer }));
    await up.goto("file://" + path.join(OUT, "live.html") + "#tonight"); await up.waitForTimeout(400);
    await up.evaluate(() => window.JG_DEBUG.checkForUpdate()); await up.waitForTimeout(400);
    assert(await up.evaluate((g) => document.getElementById("jg-data").textContent.includes(g), real.generated), "no newer build: the page stays as it is");
    // while a line is typed the page asks instead of reloading
    served.v = later;
    await up.evaluate(() => { window.JG_DEBUG.pk.edited["x"] = 1; window.JG_DEBUG.checkForUpdate(); }); await up.waitForTimeout(500);
    assert(await up.evaluate((g) => !document.getElementById("upd").hidden && /newer model run/.test(document.getElementById("upd").textContent) && document.getElementById("jg-data").textContent.includes(g), real.generated), "a newer build while a line is typed is offered, not forced");
    await up.click("#updGo"); await up.waitForTimeout(900);
    assert(await up.evaluate((g) => document.getElementById("jg-data").textContent.includes(g) && !!window.JG_DEBUG && document.getElementById("tHead").textContent.length > 0, later), "the newer build is loaded in place and runs");
    await up.close();
  }
  // ---- what a percentage means: every bar opens an explanation, and the explainer does the arithmetic
  {
    const ex = await openAt(browser, { viewport: { width: 1280, height: 900 } }, real);
    await ex.goto("file://" + path.join(OUT, "real.html") + "#tonight"); await ex.waitForTimeout(300);
    if (await ex.$("#mapGames button.cm")) {
      await ex.click("#mapGames button.cm"); await ex.waitForTimeout(100);
      assert(/is our chance this bet wins/.test(await ex.evaluate(() => document.getElementById("mapGames").textContent)), "tapping a game's bar explains its percentage in wins and dollars");
    }
    if (await ex.$("#mapProps button.cm")) {
      await ex.click("#mapProps button.cm"); await ex.waitForTimeout(100);
      assert(/is our chance this pick hits/.test(await ex.evaluate(() => document.getElementById("mapProps").textContent)), "tapping a pick's bar explains what it needs in each slip");
    }
    await ex.goto("file://" + path.join(OUT, "real.html") + "#method/percent"); await ex.waitForTimeout(300);
    const at60 = await ex.evaluate(() => document.getElementById("pctOut").textContent);
    assert(/60 wins in 100/.test(at60) && /1\.15×/.test(at60) && /\+\$14\.55/.test(at60) && /1\.08×/.test(at60), "the explainer at 60%: a -110 total returns 1.15x (+$14.55 per 100 bets), a 2-pick Power 1.08x");
    await ex.fill("#pctSlider", "52"); await ex.dispatchEvent("#pctSlider", "input"); await ex.waitForTimeout(100);
    assert(/−\$0\.73/.test(await ex.evaluate(() => document.getElementById("pctOut").textContent)), "the explainer at 52%: a -110 total loses $0.73 per 100 bets");
    await ex.close();
  }
  // ---- the real slate: a PASS night stakes no game bets at any amount
  const r = await openAt(browser, { viewport: { width: 1280, height: 900 } }, real);
  await r.goto("file://" + path.join(OUT, "real.html") + "#tonight"); await r.waitForTimeout(300);
  if (real.night && String(real.night.verdict).toUpperCase() === "PASS") assert(await r.evaluate(() => [10, 100, 1000].every((a) => window.JG_DEBUG.plan(a).rows.length === 0)), "real slate (PASS): no game bets at any amount");
  await r.close();
  await browser.close();
  console.log("\n" + (checks - failures) + "/" + checks + " checks passed; screenshots in " + OUT);
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
