// Dashboard checks with playwright (node tests/test_site.js [outdir]).
// Renders jghedge/site/template.html the way jghedge/site/__init__.py does, against three slates:
//   fixture  tests/fixtures/latest_sample.json (full data contract: policy, night, pickem, fliff, lines)
//   real     docs/data/latest.json (whatever the nightly job last produced)
//   empty    {} (no slate at all)
// and asserts: no console/page errors, no horizontal overflow at 390px on every tab, no bankroll input
// (tonight's amount is the only stake input), the slip hit-count distribution computed in the browser
// from pickem.sims.bits equals the slips' p_dist, one pick per player, how tonight's amount is split
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
function render(slate) {
  const data = { slate, ledger: readJson(path.join(ROOT, "docs/data/ledger.json")), backtest: readJson(path.join(ROOT, "docs/data/backtest.json")),
    replay: readJson(path.join(ROOT, "docs/data/replay_open.json")), results: {}, shared: null, repo: "https://github.com/example/repo", pages_url: "", raw_url: "" };
  const blob = JSON.stringify(data).replace(/<\//g, "<\\/");
  return "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body>" + template.replace('"__JG_DATA__"', blob) + "</body></html>";
}
let failures = 0, checks = 0;
function assert(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL:", msg); } else console.log("ok:", msg); }
const TABS = ["tonight", "games", "props", "record", "method"];

(async () => {
  const fixture = readJson(path.join(ROOT, "tests/fixtures/latest_sample.json"));
  const real = readJson(path.join(ROOT, "docs/data/latest.json"));
  const pages = { fixture: render(fixture), real: render(real), empty: render({}) };
  for (const k of Object.keys(pages)) fs.writeFileSync(path.join(OUT, k + ".html"), pages[k]);
  const browser = await chromium.launch();
  for (const name of Object.keys(pages)) {
    const file = "file://" + path.join(OUT, name + ".html");
    const errs = [];
    const phone = await browser.newPage({ viewport: { width: 390, height: 844 }, colorScheme: "dark" });
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
      const p = await browser.newPage({ viewport: { width: 1280, height: 900 }, colorScheme: scheme });
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
  const p = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const perr = []; p.on("pageerror", (e) => perr.push(String(e)));
  await p.goto("file://" + path.join(OUT, "fixture.html") + "#tonight"); await p.waitForTimeout(400);
  assert(await p.evaluate(() => document.getElementById("answer").dataset.verdict) === String(fixture.night.verdict).toUpperCase(), "answer carries the night's call (" + fixture.night.verdict + ")");
  // bit matrix: browser distribution == fixture p_dist for every slip (raw), and sums to 1
  for (const s of fixture.pickem.slips) {
    const d = await p.evaluate((ids) => window.JG_DEBUG.hitDist(ids, false), s.legs);
    const maxDiff = Math.max(...d.map((x, i) => Math.abs(x - s.p_dist[i])));
    assert(maxDiff < 1e-3, "slip distribution from bits matches " + s.type + " p_dist (max diff " + maxDiff.toFixed(5) + ")");
    assert(Math.abs(d.reduce((a, b) => a + b, 0) - 1) < 1e-9, s.type + " distribution sums to 1");
  }
  { const s = fixture.pickem.slips[0]; const raw = await p.evaluate((ids) => window.JG_DEBUG.hitDist(ids, false), s.legs), shr = await p.evaluate((ids) => window.JG_DEBUG.hitDist(ids, true), s.legs);
    assert(shr[shr.length - 1] <= raw[raw.length - 1] + 1e-9, "after the cut, P(all hit) <= raw P(all hit)"); }
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
  // the edge map lists every game with a posted total
  const nTotals = new Set(fixture.bets.filter((b) => b.kind === "total").map((b) => b.game_id)).size;
  assert(await p.evaluate(() => document.querySelectorAll("#mapGames .erow").length) === nTotals, "edge map has one row per game with a total (" + nTotals + ")");
  const statuses = await p.evaluate(() => Array.from(document.querySelectorAll("#mapGames .erow")).map((r) => r.className));
  assert(statuses.every((c) => /s-(edge|close|none)/.test(c)), "every game row is marked edge, close or none");
  // games tab: details open the numbers
  await p.goto("file://" + path.join(OUT, "fixture.html") + "#games"); await p.waitForTimeout(300);
  await p.click("#gameRows [data-why]"); await p.waitForTimeout(100);
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
    const q = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    await q.goto("file://" + path.join(OUT, "passedge.html") + "#tonight"); await q.waitForTimeout(300);
    const t = await q.evaluate(() => ({ sub: document.getElementById("tSub").textContent, rows: window.JG_DEBUG.plan(50).rows.length,
      edge: Array.from(document.querySelectorAll("#mapGames .erow.s-edge")).map((r) => r.querySelector(".act").textContent) }));
    assert(t.rows === 0, "PASS night with an edge: no game bets staked");
    assert(/passes on games/.test(t.sub) && !/None of tonight's/.test(t.sub), "PASS night with an edge: the summary names the edge instead of saying there is none");
    assert(t.edge.length === 1 && /desk passes on the night/.test(t.edge[0]) && !/^Bet /.test(t.edge[0]), "PASS night with an edge: the row is tagged Edge and explains the pass");
    await q.close();
  }
  // ---- the real slate: a PASS night stakes no game bets at any amount
  const r = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await r.goto("file://" + path.join(OUT, "real.html") + "#tonight"); await r.waitForTimeout(300);
  if (real.night && String(real.night.verdict).toUpperCase() === "PASS") assert(await r.evaluate(() => [10, 100, 1000].every((a) => window.JG_DEBUG.plan(a).rows.length === 0)), "real slate (PASS): no game bets at any amount");
  await r.close();
  await browser.close();
  console.log("\n" + (checks - failures) + "/" + checks + " checks passed; screenshots in " + OUT);
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
