// Dashboard checks with playwright (node tests/test_site.js [outdir]).
// Renders jghedge/site/template.html the way jghedge/site/__init__.py does, against three slates:
//   fixture  tests/fixtures/latest_sample.json (full data contract: policy, night, pickem, fliff, lines)
//   real     docs/data/latest.json (whatever the nightly job last produced)
//   empty    {} (no slate at all)
// and asserts: no console/page errors, no horizontal overflow at 390px on every tab, no bankroll input
// (tonight's amount is the only stake input), the page's exact slip value equals the engine's for every
// published slip (including the void rule), one pick per player, how tonight's amount is split
// (PrizePicks minimum only when it is at most half the amount, game bets by the night's call, nothing
// moved past our number or info-only), line re-pricing, the Tonight / Tomorrow switch, the opening-line
// targets of games with no line yet, and the old #desk / #pickem links.
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
    await up.focus("#amount"); await up.evaluate(() => { window.JG_DEBUG.checkForUpdate(); }); await up.waitForTimeout(500);
    assert(await up.evaluate((g) => !document.getElementById("upd").hidden && /newer model run/.test(document.getElementById("upd").textContent) && document.getElementById("jg-data").textContent.includes(g), real.generated), "a newer build while a box is in use is offered, not forced");
    await up.click("#updGo"); await up.waitForTimeout(900);
    assert(await up.evaluate((g) => document.getElementById("jg-data").textContent.includes(g) && !!window.JG_DEBUG && document.getElementById("tHead").textContent.length > 0, later), "the newer build is loaded in place and runs");
    await up.close();
  }
  // ---- tomorrow's slate rides along: a switch shows it, the choice holds for the tab, and tonight is the default while it has a game to bet
  {
    const nextSlate = JSON.parse(JSON.stringify(fixture)), dayMs = 24 * 3600 * 1000, shift = (iso) => new Date(Date.parse(iso) + dayMs).toISOString().replace(".000Z", "Z");
    nextSlate.date = "2026-11-08"; nextSlate.run_label = "opener"; nextSlate.generated = "2026-11-07T19:29:50+00:00";
    nextSlate.games.forEach((g) => { g.tip = shift(g.tip); }); nextSlate.bets.forEach((b) => { if (b.tip) b.tip = shift(b.tip); }); nextSlate.pickem.legs.forEach((l) => { l.tip = shift(l.tip); });
    nextSlate.games = nextSlate.games.slice(0, 3);
    fs.writeFileSync(path.join(OUT, "twoday.html"), render(fixture, { next: nextSlate, build: nextSlate.generated }));
    const file = "file://" + path.join(OUT, "twoday.html");
    const d = await openAt(browser, { viewport: { width: 1280, height: 900 } }, fixture);
    const derr = []; d.on("pageerror", (e) => derr.push(String(e)));
    await d.goto(file + "#tonight"); await d.waitForTimeout(300);
    const a0 = await d.evaluate(() => ({ view: window.JG_DEBUG.VIEWDAY, date: window.JG_DEBUG.date, hidden: document.getElementById("daySwitch").hidden, btns: Array.from(document.querySelectorAll("#daySwitch button")).map((b) => [b.dataset.day, b.getAttribute("aria-pressed")]) }));
    assert(a0.view === "main" && a0.date === fixture.date && !a0.hidden && a0.btns.length === 2 && a0.btns[0][1] === "true" && a0.btns[1][0] === "2026-11-08", "with tomorrow priced, a switch offers both days and tonight is shown first");
    await d.fill("#amount", "20");
    // a PrizePicks line typed for tonight is kept for tonight only
    const legT = fixture.pickem.legs.find((l) => (l.q || []).length > 2);
    await d.evaluate((a) => { window.JG_DEBUG.pk.edited[a.id] = a.line + 1; window.JG_DEBUG.savePk(); }, { id: legT.id, line: legT.line });
    await d.click("#daySwitch button[data-day='2026-11-08']"); await d.waitForTimeout(500);
    const a1 = await d.evaluate(() => ({ view: window.JG_DEBUG.VIEWDAY, date: window.JG_DEBUG.date, games: document.querySelectorAll("#mapGames .erow").length, kicker: document.getElementById("tDate").textContent, label: document.getElementById("amountLabel").textContent, pressed: document.querySelector("#daySwitch button[aria-pressed='true']").dataset.day,
      started: Array.from(document.querySelectorAll("#mapGames .erow .tag")).filter((x) => x.textContent === "Started").length, amount: document.getElementById("amount").value, edited: Object.keys(window.JG_DEBUG.pk.edited).length }));
    assert(a1.view === "next" && a1.date === "2026-11-08" && a1.games === 3 && a1.pressed === "2026-11-08" && a1.started === 0 && /Nov 8/.test(a1.kicker), "the switch loads tomorrow's slate: its date, its 3 games, none started (" + a1.kicker.slice(0, 40) + ")");
    assert(!/tonight\?/.test(a1.label) && a1.amount === "20" && a1.edited === 0, "tomorrow's view asks about that day's games, keeps the amount, and starts without tonight's typed lines");
    await d.goto(file + "#games"); await d.waitForTimeout(300);
    assert(await d.evaluate(() => window.JG_DEBUG.VIEWDAY === "next" && getComputedStyle(document.getElementById("daySwitch")).display !== "none"), "the choice holds across tabs and reloads, and the switch is on the Games tab too");
    await d.goto(file + "#tonight"); await d.waitForTimeout(300);
    await d.click("#daySwitch button[data-day='" + fixture.date + "']"); await d.waitForTimeout(500);
    const a2 = await d.evaluate((id) => ({ view: window.JG_DEBUG.VIEWDAY, date: window.JG_DEBUG.date, kept: window.JG_DEBUG.pk.edited[id] }), legT.id);
    assert(a2.view === "main" && a2.date === fixture.date && a2.kept === legT.line + 1, "switching back shows tonight, with the PrizePicks line typed earlier still there");
    await d.goto(file + "#method"); await d.waitForTimeout(200);
    assert(await d.evaluate(() => getComputedStyle(document.getElementById("daySwitch")).display === "none"), "the switch is hidden on tabs that are not about a slate");
    assert(derr.length === 0, "no page errors with two slates: " + JSON.stringify(derr));
    await d.close();
    // once every game tonight has tipped, a fresh tab opens on tomorrow
    const late = await openAt(browser, { viewport: { width: 1280, height: 900 } }, fixture, 9);
    await late.goto(file + "#tonight"); await late.waitForTimeout(300);
    assert(await late.evaluate(() => window.JG_DEBUG.VIEWDAY === "next" && document.querySelectorAll("#mapGames .erow").length === 3), "after tonight's last tip the page opens on tomorrow's slate");
    await late.close();
    // tomorrow before its lines are posted: the switch is still there, and each game shows the opening line that would be a bet
    if (real.live_pricing) {
      const bare = JSON.parse(JSON.stringify(nextSlate));
      Object.assign(bare, { bets: [], parlays: [], portfolio: { ids: [], mu: [], cov: [] }, live_pricing: real.live_pricing, picks: {} });
      bare.pickem.legs = []; bare.pickem.slips = []; bare.allocations.reference.items = []; bare.night = Object.assign({}, bare.night, { verdict: "PASS", score: 0, would_change: [] });
      fs.writeFileSync(path.join(OUT, "bare.html"), render(fixture, { next: bare, build: bare.generated }));
      const w = await openAt(browser, { viewport: { width: 1280, height: 900 } }, fixture);
      const werr = []; w.on("pageerror", (e) => werr.push(String(e)));
      await w.goto("file://" + path.join(OUT, "bare.html") + "#tonight"); await w.waitForTimeout(300);
      assert(/lines not posted yet/.test(await w.evaluate(() => document.getElementById("daySwitch").textContent)), "tomorrow without lines is still offered, and the switch says its lines are not posted");
      await w.click("#daySwitch button[data-day='2026-11-08']"); await w.waitForTimeout(500);
      const wr = await w.evaluate(() => { const D = window.JG_DEBUG, dec = 1 + 100 / 110, LPm = JSON.parse(document.getElementById("jg-data").textContent).next.live_pricing;
        const rows = Array.from(document.querySelectorAll("#mapGames [data-wait]")).map((el) => ({ id: el.dataset.wait, txt: el.textContent }));
        const g = D.GAMES[rows[0].id], p = D.preLine(g);
        const ev = (side, L) => { const a = D.liveProbs(p.w * p.tm + (1 - p.w) * L + (g.model.total_context || 0), L), pw = side === "over" ? a.over : a.under; return pw * (dec - 1) - (1 - pw - a.push); };
        return { rows, p, head: document.getElementById("tHead").textContent, sub: document.getElementById("tSub").textContent, floor: LPm.floor / LPm.s, minEv: LPm.min_ev,
          atOver: ev("over", p.over), pastOver: ev("over", p.over + 1), atUnder: ev("under", p.under), pastUnder: ev("under", p.under - 1) }; });
      assert(wr.rows.length === 3 && wr.rows.every((r) => /line not posted yet/.test(r.txt) && /Over at [\d.]+ or lower/.test(r.txt) && /Under at [\d.]+ or higher/.test(r.txt)), "every game waiting for a line shows the opening lines that would give it an edge");
      const bar = Math.max(wr.floor, wr.minEv);
      assert(wr.p.over < wr.p.tm && wr.p.tm < wr.p.under && wr.atOver >= bar - 1e-9 && wr.pastOver < bar && wr.atUnder >= bar - 1e-9 && wr.pastUnder < bar, "those lines are the last ones that clear the desk's bar (Over " + wr.p.over + ", our total " + wr.p.tm + ", Under " + wr.p.under + ")");
      assert(/No bets for/.test(wr.head) && /opening line that would give it an edge/.test(wr.sub), "the headline says there is nothing to bet yet and points to the opening lines (" + wr.head + ")");
      assert(werr.length === 0, "no page errors on a slate without lines: " + JSON.stringify(werr));
      await w.close();
    }
    // one slate only: no switch
    const one = await openAt(browser, { viewport: { width: 1280, height: 900 } }, fixture);
    await one.goto("file://" + path.join(OUT, "fixture.html") + "#tonight"); await one.waitForTimeout(200);
    assert(await one.evaluate(() => document.getElementById("daySwitch").hidden && window.JG_DEBUG.VIEWDAY === "main"), "with one slate there is no switch");
    await one.close();
  }
  // ---- tracking: say which of the plan's bets we placed, add a bet by hand, remove one, log the slate
  {
    const tr = await openAt(browser, { viewport: { width: 1280, height: 900 } }, fixture);
    const terr = []; tr.on("pageerror", (e) => terr.push(String(e)));
    const file = "file://" + path.join(OUT, "fixture.html");
    await tr.goto(file + "#tonight"); await tr.waitForTimeout(300);
    await tr.fill("#amount", "40"); await tr.waitForTimeout(200);
    const planned = await tr.evaluate(() => { const x = window.JG_DEBUG.plan(40); return x.rows.length + (x.pp && !x.pp.skip ? 1 : 0); });
    const boxes = await tr.evaluate(() => Array.from(document.querySelectorAll("#plan input[data-log]")).map((c) => [c.dataset.log, c.checked]));
    assert(boxes.length === planned && planned >= 1 && boxes.every((b) => b[1]), "every planned bet has a ticked 'We placed this' box (" + boxes.length + ")");
    await tr.uncheck("#plan input[data-log='" + boxes[0][0] + "']");
    await tr.click("#placed"); await tr.waitForTimeout(250);
    const n1 = await tr.evaluate(() => window.JG_DEBUG.loadPlaced().length);
    assert(n1 === boxes.length - 1, "only the ticked bets are logged (" + n1 + " of " + boxes.length + ")");
    await tr.check("#plan input[data-log='" + boxes[0][0] + "']");
    await tr.click("#placed"); await tr.waitForTimeout(250);
    const n2 = await tr.evaluate(() => window.JG_DEBUG.loadPlaced().length);
    assert(n2 === boxes.length, "ticking it again and logging adds it without duplicating the others (" + n2 + ")");
    // add a game total by hand on the Record tab, at the number we actually got
    await tr.goto(file + "#record"); await tr.waitForTimeout(250);
    await tr.click("#addBet summary");
    const g0 = fixture.games[0], under = fixture.bets.find((b) => b.id === g0.id + "-to-u");
    await tr.selectOption("#abType", "total"); await tr.selectOption("#abWhat", g0.id); await tr.selectOption("#abSide", "under");
    const pre = await tr.evaluate(() => ({ line: Number(document.getElementById("abLine").value), odds: Number(document.getElementById("abOdds").value) }));
    assert(pre.line === under.line && pre.odds === under.price, "the entry starts from the sportsbook's current line and odds (" + pre.line + ", " + pre.odds + ")");
    await tr.fill("#abOdds", "50"); await tr.fill("#abStake", "2"); await tr.click("#abAdd"); await tr.waitForTimeout(100);
    const refused = await tr.evaluate(() => ({ msg: document.getElementById("abMsg").textContent, n: window.JG_DEBUG.loadPlaced().length }));
    assert(/odds/.test(refused.msg) && refused.n === n2, "odds that are not American odds are refused");
    await tr.fill("#abLine", String(under.line + 1)); await tr.fill("#abOdds", "-105"); await tr.fill("#abStake", "2.5"); await tr.click("#abAdd"); await tr.waitForTimeout(250);
    const st = await tr.evaluate(() => { const a = window.JG_DEBUG.loadPlaced(), e = a[a.length - 1]; return { n: a.length, e, rows: document.querySelectorAll("#ourTbl tbody tr").length, msg: document.getElementById("abMsg").textContent }; });
    assert(st.n === n2 + 1 && st.e.manual === true && st.e.type === "single" && st.e.stake === 2.5 && st.e.price === -105 && st.e.legs[0].kind === "total" && st.e.legs[0].side === "under" && st.e.legs[0].line === under.line + 1 && st.e.legs[0].game_id === g0.id && st.rows === st.n && /Added/.test(st.msg),
      "a bet typed by hand is stored with the line, odds and stake we got, and listed");
    const gr = await tr.evaluate((gid) => { const D = window.JG_DEBUG, a = D.loadPlaced(), e = a[a.length - 1], L = e.legs[0].line, day = (h, w) => ({ games: { [gid]: { hs: h, as: w } }, players: {} });
      return [D.settleBet(e, day(Math.floor(L / 2) - 5, Math.floor(L / 2) - 5)), D.settleBet(e, day(Math.ceil(L / 2) + 5, Math.ceil(L / 2) + 5))]; }, g0.id);
    assert(gr[0].result === "win" && Math.abs(gr[0].pnl - 2.5 * (100 / 105)) < 1e-9 && gr[1].result === "loss" && gr[1].pnl === -2.5, "a hand-entered Under grades as a win below the line and a loss above it");
    // a single player prop
    await tr.selectOption("#abType", "prop"); await tr.waitForTimeout(100);
    const propKey = await tr.evaluate(() => document.getElementById("abWhat").value);
    if (propKey) {
      await tr.selectOption("#abSide", "over"); await tr.fill("#abOdds", "-112"); await tr.fill("#abStake", "1"); await tr.click("#abAdd"); await tr.waitForTimeout(250);
      const pe = await tr.evaluate(() => { const a = window.JG_DEBUG.loadPlaced(); return a[a.length - 1]; });
      assert(pe.manual && pe.legs[0].kind === "prop" && pe.legs[0].side === "over" && pe.legs[0].player_id === propKey.split("|")[0] && pe.legs[0].stat === propKey.split("|")[1] && pe.legs[0].line > 0, "a single player prop can be logged by hand (" + pe.label + ")");
    }
    const nManual = await tr.evaluate(() => window.JG_DEBUG.loadPlaced().filter((e) => e.manual).length);
    // the shared-record link carries every bet logged for the slate
    const link = await tr.evaluate(() => { const a = document.getElementById("logOurs"); return { hidden: a.hidden, href: a.href }; });
    const body = decodeURIComponent((link.href.split("body=")[1] || ""));
    const payload = JSON.parse((body.split("```json\n")[1] || "{}").split("\n```")[0] || "{}");
    assert(!link.hidden && /Bets%202026-11-07/.test(link.href) && payload.date === fixture.date && payload.bets.length === (await tr.evaluate(() => window.JG_DEBUG.loadPlaced().length)) && payload.bets.every((b) => b.stake > 0 && b.legs.length >= 1),
      "the shared-record link carries every bet logged for the slate (" + (payload.bets || []).length + ")");
    // logging the plan again never wipes a bet typed by hand
    await tr.goto(file + "#tonight"); await tr.waitForTimeout(250);
    await tr.click("#placed"); await tr.waitForTimeout(250);
    assert(await tr.evaluate((n) => window.JG_DEBUG.loadPlaced().filter((e) => e.manual).length === n, nManual), "logging the plan again keeps the bets typed by hand (" + nManual + ")");
    // removing a logged bet takes two presses
    await tr.goto(file + "#record"); await tr.waitForTimeout(250);
    const before = await tr.evaluate(() => window.JG_DEBUG.loadPlaced().length);
    await tr.click("#ourTbl [data-rm]"); await tr.waitForTimeout(100);
    const mid = await tr.evaluate(() => ({ n: window.JG_DEBUG.loadPlaced().length, txt: document.querySelector("#ourTbl [data-rm]").textContent }));
    await tr.click("#ourTbl [data-rm]"); await tr.waitForTimeout(150);
    const after = await tr.evaluate(() => ({ n: window.JG_DEBUG.loadPlaced().length, rows: document.querySelectorAll("#ourTbl tbody tr").length }));
    assert(mid.n === before && /Remove\?/.test(mid.txt) && after.n === before - 1 && after.rows === after.n, "a logged bet is removed on the second press (" + before + " → " + after.n + ")");
    assert(terr.length === 0, "no page errors while tracking bets: " + JSON.stringify(terr));
    await tr.close();
    // phone: the entry form does not overflow
    const ph = await openAt(browser, { viewport: { width: 390, height: 844 } }, fixture);
    await ph.goto(file + "#record"); await ph.waitForTimeout(250); await ph.click("#addBet summary"); await ph.waitForTimeout(150);
    assert(await ph.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), "the bet-entry form fits a phone screen");
    await ph.close();
  }
  // ---- PrizePicks' own board, read by the browser: fetch (or paste), match to the published price tables, re-price picks and slips
  if (((real.pickem || {}).pool || []).length > 40) {
    const PPNAME = { pts: "Points", reb: "Rebounds", ast: "Assists", fg3m: "3-PT Made", pra: "Pts+Rebs+Asts", pr: "Pts+Rebs", pa: "Pts+Asts", ra: "Rebs+Asts", stl: "Steals", blk: "Blocked Shots", tov: "Turnovers" };
    const pool = real.pickem.pool, lineOk = (r, L) => r.q.some((e) => Math.abs(e[0] - L) < 1e-9);
    // a board of 60 standard lines (every fifth one a point above DraftKings'), plus a demon, an unpriced stat, an unknown player and a line too far away
    const picks = pool.filter((r, i) => PPNAME[r.stat] && i % 3 === 0).slice(0, 60);
    const mk = (i, name, statType, line, odds) => ({ type: "projection", id: "p" + i, attributes: { line_score: line, stat_type: statType, odds_type: odds || "standard", status: "pre_game" }, relationships: { new_player: { data: { type: "new_player", id: "n" + i } } }, _name: name });
    const data = picks.map((r, i) => mk(i, r.player, PPNAME[r.stat], i % 5 === 0 && lineOk(r, r.line + 1) ? r.line + 1 : r.line));
    data.push(mk(900, picks[0].player, PPNAME[picks[0].stat], picks[0].line + 2, "demon"));
    data.push(mk(901, picks[1].player, "Fantasy Score", 31.5));
    data.push(mk(902, "Nobody Known", "Points", 12.5));
    const farRow = pool.find((r) => PPNAME[r.stat] && !picks.includes(r));
    data.push(mk(903, farRow.player, PPNAME[farRow.stat], farRow.line + 20));
    const board = { data: data.map((d) => ({ type: d.type, id: d.id, attributes: d.attributes, relationships: d.relationships })), included: data.map((d) => ({ type: "new_player", id: d.relationships.new_player.data.id, attributes: { display_name: d._name } })), links: {} };
    const expectMatched = picks.length, expectDiffer = picks.filter((r, i) => i % 5 === 0 && lineOk(r, r.line + 1)).length;
    const file = "file://" + path.join(OUT, "real.html");
    const bp = await openAt(browser, { viewport: { width: 1280, height: 900 } }, real);
    const berr = []; bp.on("pageerror", (e) => berr.push(String(e)));
    let served = 0;
    await bp.route("https://api.prizepicks.com/**", (route) => { served++; route.fulfill({ status: 200, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(board) }); });
    await bp.goto(file + "#props"); await bp.waitForTimeout(300);
    const before = await bp.evaluate(() => ({ legs: window.JG_DEBUG.legs().length, bar: document.getElementById("pkBanner").textContent, btn: !!document.querySelector("#pkBanner [data-pp='load']"), src: window.JG_DEBUG.legs()[0].line_source }));
    assert(before.btn && /DraftKings' lines, standing in/.test(before.bar) && before.src === "dk", "before loading, the bar says the lines are DraftKings' stand-ins and offers to load PrizePicks' board");
    await bp.click("#pkBanner [data-pp='load']"); await bp.waitForTimeout(500);
    const got = await bp.evaluate(() => { const D = window.JG_DEBUG, b = D.board(), legs = D.legs(); return { b, n: legs.length, srcs: Array.from(new Set(legs.map((l) => l.line_source))), bar: document.getElementById("pkBanner").textContent,
      slips: D.buildSlips().map((s) => ({ type: s.type, verdict: s.verdict, players: s.legs.map((id) => legs.find((l) => l.id === id).player_id), teams: s.legs.map((id) => legs.find((l) => l.id === id).team), ev: s.ev_shrunk })),
      cards: document.querySelectorAll("#pkSlips .card").length, stored: !!localStorage.getItem("jg.ppboard." + D.date) }; });
    assert(served >= 1 && got.b && got.b.matched === expectMatched && got.n === expectMatched && got.b.differ === expectDiffer && got.b.alt === 1 && got.b.far === 1 && got.b.unknown === 1 && got.srcs.join() === "prizepicks",
      "the board is read and matched: " + got.b.matched + " picks at PrizePicks' line (" + got.b.differ + " differ from DraftKings'), the demon left out, the far line and the unknown player skipped");
    assert(/PrizePicks' lines loaded/.test(got.bar) && got.stored, "the bar says PrizePicks' lines are loaded, and the board is kept on this device");
    const shared = await bp.evaluate(() => window.JG_DEBUG.shareText());
    assert(shared.startsWith("JGPP1 " + real.date + " ") && shared.split(",").length === expectMatched && shared.length < 40 * expectMatched && !!(await bp.$("#pkBanner [data-pp='share']")), "a loaded board can be copied for a partner as one short line of text (" + shared.length + " characters)");
    // a pick at a line one point above DraftKings' is priced from its table at that line: Less gets easier
    const moved = picks.find((r, i) => i % 5 === 0 && lineOk(r, r.line + 1));
    const chk = await bp.evaluate((a) => { const l = window.JG_DEBUG.legs().find((x) => x.id === a.id); return { line: l.line, pm: l.p_more, dk: l.dk_line }; }, { id: moved.id });
    const qAt = (r, L) => r.q.find((e) => Math.abs(e[0] - L) < 1e-9)[1];
    assert(chk.line === moved.line + 1 && chk.dk === moved.line && Math.abs(chk.pm - qAt(moved, moved.line + 1)) < 1e-9 && chk.pm < qAt(moved, moved.line), "a PrizePicks line a point above DraftKings' is priced from the table at that line (More " + (chk.pm * 100).toFixed(1) + "% against " + (qAt(moved, moved.line) * 100).toFixed(1) + "%)");
    // slips are rebuilt from these picks: one per player, at least two teams, never "check the lines"
    assert(got.slips.length >= 3 && got.slips.every((s) => new Set(s.players).size === s.players.length && new Set(s.teams).size >= 2 && s.verdict !== "check_lines") && got.cards >= 1, "slips are rebuilt from PrizePicks' lines: one pick per player, two teams or more, " + got.slips.length + " sizes priced");
    await bp.goto(file + "#tonight"); await bp.waitForTimeout(300);
    const ton = await bp.evaluate(() => ({ head: document.getElementById("tHead").textContent, bar: document.getElementById("ppBarT").hidden ? "" : document.getElementById("ppBarT").textContent, plan: window.JG_DEBUG.plan(40).pp ? window.JG_DEBUG.plan(40).pp.slip.verdict : null }));
    assert(!/check the lines/.test(ton.head) && /PrizePicks' lines loaded/.test(ton.bar) && ton.plan !== "check_lines", "Tonight uses the loaded lines too: no 'check the lines' (" + ton.head + ")");
    // the board survives a reload, and can be dropped
    await bp.reload(); await bp.waitForTimeout(400);
    assert(await bp.evaluate((n) => { const D = window.JG_DEBUG; return !!D.board() && D.legs().length === n && D.legs()[0].line_source === "prizepicks"; }, expectMatched), "after a reload the loaded board is still applied");
    await bp.click("#ppBarT [data-pp='forget']"); await bp.waitForTimeout(300);
    assert(await bp.evaluate((n) => { const D = window.JG_DEBUG; return !D.board() && D.legs().length === n && D.legs()[0].line_source === "dk" && !localStorage.getItem("jg.ppboard." + D.date); }, before.legs), "'Back to DraftKings' lines' restores the model run's picks");
    assert(berr.length === 0, "no page errors loading the board: " + JSON.stringify(berr));
    await bp.close();
    // PrizePicks refuses the browser: the page asks for a paste instead, and the pasted board works the same
    const bq = await openAt(browser, { viewport: { width: 390, height: 844 } }, real);
    const qerr = []; bq.on("pageerror", (e) => qerr.push(String(e)));
    await bq.route("https://api.prizepicks.com/**", (route) => route.abort());
    await bq.goto(file + "#props"); await bq.waitForTimeout(300);
    await bq.click("#pkBanner [data-pp='load']"); await bq.waitForTimeout(500);
    const blocked = await bq.evaluate(() => ({ state: window.JG_DEBUG.ppui().state, ta: !!document.querySelector("#pkBanner textarea[data-pp='text']"), txt: document.getElementById("pkBanner").textContent, wide: document.documentElement.scrollWidth <= document.documentElement.clientWidth }));
    assert(blocked.state === "blocked" && blocked.ta && /not allowed to read/.test(blocked.txt) && blocked.wide, "when the browser is refused, the page explains the copy-and-paste route (and it fits a phone)");
    await bq.fill("#pkBanner textarea[data-pp='text']", "this is not a board"); await bq.click("#pkBanner [data-pp='paste']"); await bq.waitForTimeout(200);
    assert(/did not read as PrizePicks' board/.test(await bq.evaluate(() => document.getElementById("pkBanner").textContent)), "text that is not a board is refused with a clear message");
    await bq.fill("#pkBanner textarea[data-pp='text']", JSON.stringify(board)); await bq.click("#pkBanner [data-pp='paste']"); await bq.waitForTimeout(400);
    assert(await bq.evaluate((n) => { const b = window.JG_DEBUG.board(); return !!b && b.matched === n && window.JG_DEBUG.ppui().state === "loaded"; }, expectMatched), "a pasted board is matched and applied like a fetched one");
    assert(qerr.length === 0, "no page errors on the paste route: " + JSON.stringify(qerr));
    await bq.close();
    // PrizePicks' own refusal is shown as it is, with no paste box that could not work
    const geo = await openAt(browser, { viewport: { width: 1280, height: 900 } }, real);
    await geo.route("https://api.prizepicks.com/**", (route) => route.fulfill({ status: 403, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify({ error: "PrizePicks is currently unavailable in your location." }) }));
    await geo.goto(file + "#props"); await geo.waitForTimeout(300);
    await geo.click("#pkBanner [data-pp='load']"); await geo.waitForTimeout(500);
    const gs = await geo.evaluate(() => ({ txt: document.getElementById("pkBanner").textContent, ta: !!document.querySelector("#pkBanner textarea"), legs: window.JG_DEBUG.legs()[0].line_source, board: window.JG_DEBUG.board() }));
    assert(/unavailable in your location/.test(gs.txt) && /cannot be loaded here/.test(gs.txt) && /paste the lines they copied/.test(gs.txt) && gs.ta && gs.legs === "dk" && !gs.board, "PrizePicks' own refusal is shown as it is, with a box for lines a partner loaded; the stand-in lines stay");
    // lines for another day are refused with the reason
    await geo.fill("#pkBanner textarea[data-pp='text']", shared.replace("JGPP1 " + real.date, "JGPP1 2020-01-01")); await geo.click("#pkBanner [data-pp='paste']"); await geo.waitForTimeout(250);
    const wrongDay = await geo.evaluate(() => ({ txt: document.getElementById("pkBanner").textContent, board: window.JG_DEBUG.board() }));
    assert(!wrongDay.board && /Those lines are for/.test(wrongDay.txt), "lines copied for another day are refused, and the page says why");
    // the partner's lines give this device the same picks, at the same lines
    await geo.fill("#pkBanner textarea[data-pp='text']", shared); await geo.click("#pkBanner [data-pp='paste']"); await geo.waitForTimeout(400);
    const handed = await geo.evaluate((a) => { const D = window.JG_DEBUG, b = D.board(), l = D.legs().find((x) => x.id === a.id); return { matched: b && b.matched, partner: !!(b && b.from && b.from.partner), alt: b && b.alt, line: l && l.line, src: l && l.line_source, txt: document.getElementById("pkBanner").textContent,
      slip: (D.plan(40).pp || {}).slip ? D.plan(40).pp.slip.verdict : null, again: D.shareText() }; }, { id: moved.id });
    assert(handed.matched === expectMatched && handed.partner && handed.alt === 1 && handed.line === moved.line + 1 && handed.src === "prizepicks" && /from your partner/.test(handed.txt) && handed.slip !== "check_lines",
      "pasting the partner's lines gives this device the same " + expectMatched + " picks at PrizePicks' lines");
    assert(handed.again.split(" ")[4] === shared.split(" ")[4], "the lines can be passed on again unchanged");
    await geo.reload(); await geo.waitForTimeout(400);
    assert(await geo.evaluate((n) => { const b = window.JG_DEBUG.board(); return !!b && b.matched === n && b.from && b.from.partner === true; }, expectMatched), "the partner's lines are still applied after a reload");
    await geo.close();
    // without any refusal, the paste box is one press away
    const idle = await openAt(browser, { viewport: { width: 390, height: 844 } }, real);
    await idle.goto(file + "#props"); await idle.waitForTimeout(300);
    await idle.click("#pkBanner [data-pp='askpaste']"); await idle.waitForTimeout(150);
    assert(await idle.evaluate(() => !!document.querySelector("#pkBanner textarea[data-pp='text']") && /Paste the lines your partner copied/.test(document.getElementById("pkBanner").textContent) && document.documentElement.scrollWidth <= document.documentElement.clientWidth), "“Paste lines from a partner” opens the paste box (and it fits a phone)");
    await idle.fill("#pkBanner textarea[data-pp='text']", "hello"); await idle.click("#pkBanner [data-pp='paste']"); await idle.waitForTimeout(200);
    assert(await idle.evaluate(() => !window.JG_DEBUG.board()), "text that is neither a hand-off nor a board changes nothing");
    await idle.close();
    // names join across sites
    const nm = await openAt(browser, { viewport: { width: 1280, height: 900 } }, real);
    await nm.goto(file + "#method"); await nm.waitForTimeout(300);
    assert(await nm.evaluate(() => { const f = window.JG_DEBUG.normName; return f("Nikola Jokić") === "nikola jokic" && f("Jaren Jackson Jr.") === "jaren jackson" && f("Shai Gilgeous-Alexander") === "shai gilgeousalexander" && f("  P.J.  Washington ") === "pj washington"; }), "player names are matched without accents, dots, hyphens or suffixes");
    // the payout table: the page's break-evens equal the engine's, and change with what the app pays
    const be0 = await nm.evaluate(() => window.JG_DEBUG.be());
    const worstBe = Math.max(...Object.keys(real.pickem.breakeven).map((k) => Math.abs((be0[k]) - real.pickem.breakeven[k])));
    assert(worstBe < 2e-4, "the page's break-evens match the engine's for every slip size (max gap " + worstBe.toFixed(5) + ")");
    assert(await nm.evaluate(() => Math.abs(window.JG_DEBUG.breakEven("power3") - Math.pow(6, -1 / 3)) < 1e-6 && Math.abs(window.JG_DEBUG.breakEven("power2") - Math.pow(3, -1 / 2)) < 1e-6), "a Power slip's break-even is the root of its payout (2-pick 57.7%, 3-pick 55.0% at 6x)");
    await nm.fill("#payGrid input[data-pay='power|3|0']", "5"); await nm.dispatchEvent("#payGrid input[data-pay='power|3|0']", "change"); await nm.waitForTimeout(300);
    const pe = await nm.evaluate(() => ({ be: window.JG_DEBUG.be().power3, pay: window.JG_DEBUG.pay().power["3"], reset: !document.getElementById("payReset").hidden, stored: !!localStorage.getItem("jg.pay"), engine: (window.JG_DEBUG.plan(40).slip || {}).engine === true || window.JG_DEBUG.buildSlips().length > 0, note: document.getElementById("payNote").textContent }));
    assert(Math.abs(pe.be - Math.pow(5, -1 / 3)) < 1e-6 && pe.pay === 5 && pe.reset && pe.stored && /You changed this table/.test(pe.note), "typing 5x for a 3-pick Power moves its break-even to 58.5% and is remembered");
    await nm.reload(); await nm.waitForTimeout(300);
    assert(await nm.evaluate(() => Math.abs(window.JG_DEBUG.be().power3 - Math.pow(5, -1 / 3)) < 1e-6), "the typed payout is still in force after a reload");
    await nm.click("#payReset"); await nm.waitForTimeout(300);
    assert(await nm.evaluate(() => Math.abs(window.JG_DEBUG.be().power3 - Math.pow(6, -1 / 3)) < 1e-6 && !localStorage.getItem("jg.pay") && document.getElementById("payReset").hidden), "'Back to the default table' restores the assumed payouts");
    await nm.close();
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
