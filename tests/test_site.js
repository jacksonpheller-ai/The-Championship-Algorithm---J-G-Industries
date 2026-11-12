// Dashboard checks with playwright (node tests/test_site.js [outdir]).
// Renders jghedge/site/template.html the way jghedge/site/__init__.py does, against three slates:
//   fixture  tests/fixtures/latest_sample.json (full data contract: policy, night, props, fliff, lines)
//   real     docs/data/latest.json (whatever the nightly job last produced)
//   empty    {} (no slate at all)
// and asserts: no console/page errors, no horizontal overflow at 390px on every tab, no bankroll input
// (tonight's amount is the only stake input), how tonight's amount is split (everything on Fliff: props
// first at a quarter of Kelly, at most half and one per player; game bets with the rest by the night's
// call, nothing moved past our number or info-only), Fliff prop pricing (the page's arithmetic equals the engine's, a prop is staked only at a
// Fliff number, typed numbers are re-priced, validated and remembered), grading, and the old links.
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
  // how tonight's amount is split: everything on Fliff, props first (at most half), game bets with the rest by the night's call
  const byId = {}; fixture.bets.forEach((b) => { byId[b.id] = b; });
  const moved = new Set((fixture.lines.moved || []).filter((m) => m.still_ok === false).map((m) => m.bet_id));
  const FACT = { BET: 1, LIGHT: 0.5, PASS: 0 };
  const feedOk = fixture.props.rows.filter((r) => r.ok).map((r) => r.id);
  for (const A of [3, 8, 10, 40, 250]) {
    const pl = await p.evaluate((a) => { const x = window.JG_DEBUG.plan(a); return { total: x.total, games: x.games, props: x.props, verdict: x.verdict, rows: x.rows.map((r) => ({ id: r.id, prop: !!r.prop, stake: r.stake, legs: r.legs.map((l) => l.id) })) }; }, A);
    assert(pl.total <= A + 1e-9 && pl.games <= (A - pl.props) * FACT[pl.verdict] + 1e-9, "$" + A + ": game bets use at most " + FACT[pl.verdict] + " of what props leave on a " + pl.verdict + " night (games " + pl.games + ", props " + pl.props + ")");
    assert(pl.props <= A / 2 + 1e-9, "$" + A + ": props get at most half of the amount (" + pl.props + ")");
    assert(pl.rows.every((r) => Math.abs(r.stake / 0.25 - Math.round(r.stake / 0.25)) < 1e-9), "$" + A + ": stakes are whole Fliff-minimum steps");
    assert(pl.rows.filter((r) => !r.prop).every((r) => r.legs.every((id) => byId[id] && byId[id].kind === "total" && !byId[id].info_only && !moved.has(id))), "$" + A + ": game bets are totals only, nothing info-only or moved past our number");
    assert(pl.rows.filter((r) => r.prop).every((r) => feedOk.includes(r.id)), "$" + A + ": the only props staked are ones priced at a Fliff number with an edge");
  }
  assert(await p.evaluate(() => window.JG_DEBUG.plan(0).total === 0), "$0: nothing staked");
  await p.fill("#amount", "0"); await p.waitForTimeout(100);
  assert(/Enter tonight's amount/.test(await p.evaluate(() => document.getElementById("tHead").textContent)), "amount 0 asks for an amount");
  await p.fill("#amount", "20"); await p.waitForTimeout(100);
  assert(await p.evaluate(() => localStorage.getItem("jg.amount")) === "20", "tonight's amount is remembered on this device");
  const planCards = await p.evaluate(() => document.querySelectorAll("#plan .bet:not(.chk):not(.off)").length);
  const expect = await p.evaluate(() => window.JG_DEBUG.plan(20).rows.length);
  assert(planCards === expect && !/PrizePicks/.test(await p.evaluate(() => document.getElementById("view-tonight").textContent)), "Tonight shows one card per planned bet, all on Fliff (" + planCards + ")");
  // a prop is never staked at DraftKings' stand-in number: only the row Fliff's board was read for is in the plan
  {
    const st = await p.evaluate(() => { const D = window.JG_DEBUG; return Object.keys(D.PRBY).map((id) => { const e = D.propNow(D.PRBY[id]); return { id, src: e.src, confirmed: !!e.confirmed, ok: !!e.ok, tag: D.propStatus(D.PRBY[id]).tag }; }); });
    assert(st.filter((x) => x.src === "dk").every((x) => !x.confirmed && x.tag === "Check Fliff"), "rows without a Fliff number are tagged Check Fliff and never staked");
    assert(st.filter((x) => x.confirmed && x.ok).map((x) => x.id).join() === feedOk.join() && feedOk.length === 1, "the row with a softer Fliff line (read by the model run) is the one prop with an edge");
    const bets = await p.evaluate(() => window.JG_DEBUG.propBets().map((x) => x.r.id));
    assert(bets.join() === feedOk.join(), "that prop is the one in tonight's plan");
  }
  // the recommendation checks player props: the closest to an edge are listed as "bet it only if Fliff shows X or better"
  {
    const rowsBy = {}; fixture.props.rows.forEach((r) => { rowsBy[r.id] = r; });
    const ck = await p.evaluate(() => { const x = window.JG_DEBUG.plan(20); return { checks: x.checks.map((c) => ({ id: c.r.id, pid: c.r.player_id, side: c.side, take: c.take, stake: c.stake, tip: c.r.tip })), firm: x.rows.filter((r) => r.prop).map((r) => r.row.player_id),
      cards: Array.from(document.querySelectorAll("#plan .bet.chk")).map((el) => ({ id: el.dataset.check, txt: el.textContent, boxes: el.querySelectorAll("input[data-fl]").length })) }; });
    assert(ck.checks.length >= 3 && ck.checks.length <= 5 && ck.cards.length === ck.checks.length && ck.cards.every((c) => c.boxes === 3), "Tonight lists " + ck.checks.length + " props to check on Fliff, each with boxes for Fliff's number");
    assert(ck.checks.every((c) => c.take === (c.side === "under" ? rowsBy[c.id].take_under : rowsBy[c.id].take_over)) && ck.cards.every((c) => /Bet it only if Fliff shows/.test(c.txt)), "each check card names the engine's take-it price");
    assert(new Set(ck.checks.map((c) => c.pid)).size === ck.checks.length && ck.checks.every((c) => !ck.firm.includes(c.pid) && rowsBy[c.id].ref === "dk" && Date.parse(c.tip) > Date.parse(fixture.generated)), "check cards: one per player, none already bet, none already checked, none tipped");
    // type a clearing number on a check card: it becomes a firm bet; a worse number makes it a skip
    const c0 = ck.checks[0], sel = "#plan input[data-fl='" + c0.side + "'][data-id='" + c0.id + "']";
    await p.fill(sel, String(c0.take)); await p.dispatchEvent(sel, "change"); await p.waitForTimeout(150);
    const firm = await p.evaluate((id) => ({ inPlan: window.JG_DEBUG.plan(20).rows.some((r) => r.id === id), chk: !!document.querySelector("#plan .bet.chk[data-check='" + id + "']"), cards: document.querySelectorAll("#plan .bet:not(.chk):not(.off)").length, rows: window.JG_DEBUG.plan(20).rows.length }), c0.id);
    assert(firm.inPlan && !firm.chk && firm.cards === firm.rows, "a check card whose Fliff number clears becomes a firm bet with a stake");
    const worse = c0.take - 15 > -100 && c0.take - 15 < 100 ? -115 : c0.take - 15;
    await p.fill(sel, String(worse)); await p.dispatchEvent(sel, "change"); await p.waitForTimeout(150);
    const skip = await p.evaluate((id) => ({ inPlan: window.JG_DEBUG.plan(20).rows.some((r) => r.id === id), skip: (document.querySelector("#plan .bet.off[data-skip='" + id + "']") || {}).textContent || "" }), c0.id);
    assert(!skip.inPlan && /Skip/.test(skip.skip) && /the desk needs \+3%/.test(skip.skip), "a check card whose Fliff number does not clear is listed as a skip, with the price it would need");
    await p.click("#plan .bet.off[data-skip='" + c0.id + "'] [data-flclear]"); await p.waitForTimeout(150);
    assert(await p.evaluate((id) => !!document.querySelector("#plan .bet.chk[data-check='" + id + "']") && Object.keys(window.JG_DEBUG.typed()).length === 0, c0.id), "clearing the number puts the prop back on the check list");
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
  // props tab: type Fliff's number on a row and it is re-priced, staked, remembered, and cleared again
  await p.goto("file://" + path.join(OUT, "fixture.html") + "#props"); await p.waitForTimeout(300);
  {
    const rows = fixture.props.rows, pick = rows.find((r) => r.ref === "dk" && r.stat === "pra"), other = rows.find((r) => r.player_id === pick.player_id && r.id !== pick.id);
    const box = (id, k) => "#prRows input[data-fl='" + k + "'][data-id='" + id + "']";
    const type = async (id, k, v) => { await p.fill(box(id, k), String(v)); await p.dispatchEvent(box(id, k), "change"); await p.waitForTimeout(120); };
    const tagOf = (id) => p.evaluate((i) => document.getElementById("p-" + i).querySelector(".side .tag").textContent, id);
    assert(await p.evaluate(() => document.querySelectorAll("#prRows .erow").length) === rows.filter((r) => Date.parse(r.tip) > Date.parse(fixture.generated)).length, "the props tab lists every priced prop that has not tipped");
    assert(await tagOf(pick.id) === "Check Fliff", "a row starts as Check Fliff");
    // the take-it price on the row is the engine's
    const take = await p.evaluate((i) => { const D = window.JG_DEBUG, r = D.PRBY[i], pr = D.propProbs(r, r.line); return { u: D.takeAm(pr.under, pr.tie), o: D.takeAm(pr.over, pr.tie), txt: document.getElementById("p-" + i).querySelector(".act").textContent }; }, pick.id);
    assert(take.u === pick.take_under && take.o === pick.take_over && take.txt.includes("Under " + pick.line + " at"), "the take-it price on the row equals the engine's (under " + take.u + ", over " + take.o + ")");
    // worse than the take price: not a bet
    await type(pick.id, "under", pick.take_under - 12);
    assert(["None", "Close"].includes(await tagOf(pick.id)), "odds worse than the take-it price are not a bet (" + (await tagOf(pick.id)) + ")");
    // at the take price: an edge, in the plan, one card more
    const before = await p.evaluate(() => window.JG_DEBUG.plan(40).rows.length);
    await type(pick.id, "under", pick.take_under);
    const at = await p.evaluate((i) => { const D = window.JG_DEBUG, x = D.plan(40); return { tag: D.propStatus(D.PRBY[i]).tag, rows: x.rows.length, inPlan: x.rows.some((r) => r.id === i), props: x.props, stored: JSON.parse(localStorage.getItem("jg.fliff." + D.date)).rows[i] }; }, pick.id);
    assert(at.tag === "Edge" && at.inPlan && at.rows === before + 1 && at.props <= 20 + 1e-9, "at the take-it price the prop is an Edge and joins the plan");
    assert(at.stored && at.stored.under === pick.take_under, "the typed Fliff number is remembered on this device");
    // a softer line with the same odds is better still; a harder line is worse
    const evAt = (i) => p.evaluate((id) => window.JG_DEBUG.propNow(window.JG_DEBUG.PRBY[id]).ev, i);
    const ev0 = await evAt(pick.id);
    await type(pick.id, "line", pick.line + 1); const evUp = await evAt(pick.id);
    await type(pick.id, "line", pick.line - 1); const evDn = await evAt(pick.id);
    assert(evUp > ev0 + 0.01 && evDn < ev0 - 0.01, "a higher Fliff line helps the under, a lower one hurts it (" + ev0.toFixed(3) + " → " + evUp.toFixed(3) + " / " + evDn.toFixed(3) + ")");
    await type(pick.id, "line", pick.line);
    // one prop per player: a second edge on the same player is marked but not staked
    await type(other.id, "under", 130);
    const two = await p.evaluate((ids) => { const D = window.JG_DEBUG, x = D.plan(40); return { n: x.rows.filter((r) => ids.includes(r.id)).length, tags: ids.map((i) => D.propStatus(D.PRBY[i])) }; }, [pick.id, other.id]);
    assert(two.n === 1 && two.tags.every((s) => s.tag === "Edge") && two.tags.some((s) => /one prop per player/.test(s.act)), "two edges on one player: both tagged Edge, one staked");
    // odds that are not American odds are refused
    await type(other.id, "under", 50);
    assert(await p.evaluate((sel) => document.querySelector(sel).getAttribute("aria-invalid") === "true", box(other.id, "under")) && (await p.evaluate((i) => window.JG_DEBUG.typed()[i].under, other.id)) === 130, "odds between −100 and +100 are marked invalid and ignored");
    // a line far outside the table is not priced
    await type(pick.id, "line", pick.line + 9);
    assert(await tagOf(pick.id) === "Re-check", "a Fliff line far from DraftKings' is flagged, not priced");
    // a wrong sign is called out
    await type(pick.id, "line", pick.line); await type(pick.id, "under", 150);
    assert(/check the sign/.test(await p.evaluate((i) => window.JG_DEBUG.propStatus(window.JG_DEBUG.PRBY[i]).act, pick.id)), "odds far better than DraftKings' ask you to check the sign");
    // sizing: a quarter of Kelly, counting the nightly amount as a tenth of the bankroll, in Fliff steps; props together at most half
    const sz = await p.evaluate(() => { const D = window.JG_DEBUG, x = D.plan(40); return x.rows.filter((r) => r.prop).map((r) => ({ stake: r.stake, want: Math.round(4000 * r.e.frac / 0.10 / 25) * 0.25 })); });
    assert(sz.length >= 2 && sz.reduce((s, r) => s + r.stake, 0) <= 20 + 1e-9 && sz.some((r) => Math.abs(r.stake - r.want) < 1e-9) && sz.every((r) => r.stake <= r.want + 1e-9), "each prop is staked at a quarter of Kelly on a tenth-of-bankroll night, and props stay within half the amount (" + sz.map((r) => r.stake).join(", ") + ")");
    // find box and clearing
    await p.fill("#propFind", "early"); await p.waitForTimeout(100);
    assert(await p.evaluate(() => { const r = document.querySelectorAll("#prRows .erow"); return r.length === 1 && /Early Tipper/.test(r[0].textContent); }), "the find box narrows the list to a player");
    await p.fill("#propFind", ""); await p.waitForTimeout(100);
    await p.click("#prClearAll"); await p.waitForTimeout(150);
    const cleared = await p.evaluate((i) => ({ n: Object.keys(window.JG_DEBUG.typed()).length, tag: window.JG_DEBUG.propStatus(window.JG_DEBUG.PRBY[i]).tag, bets: window.JG_DEBUG.propBets().length }), pick.id);
    assert(cleared.n === 0 && cleared.tag === "Check Fliff" && cleared.bets === feedOk.length, "clearing the typed numbers puts every row back to Check Fliff");
    // details and the bar's explanation
    await p.click("#p-" + pick.id + " .whybtn[data-why]"); await p.waitForTimeout(100);
    assert(/DraftKings' price/.test(await p.evaluate((i) => document.getElementById("p-" + i).textContent, pick.id)), "prop Details shows where the chance comes from");
    // a placed prop is graded from the box score like any single
    const graded = await p.evaluate(() => { const D = window.JG_DEBUG, leg = { game_id: "g", kind: "prop", side: "under", line: 24.5, player_id: "7", stat: "pts", dec: 1.9 };
      const day = (v) => ({ games: { g: { hs: 100, as: 90 } }, players: { g: v == null ? {} : { "7": { pts: v } } } });
      const bet = { type: "single", stake: 2, dec: 1.9, legs: [leg] };
      return [D.settleBet(bet, day(20)), D.settleBet(bet, day(30)), D.settleBet(bet, day(null))]; });
    assert(graded[0].result === "win" && Math.abs(graded[0].pnl - 1.8) < 1e-9 && graded[1].result === "loss" && graded[1].pnl === -2 && graded[2].result === "void" && graded[2].pnl === 0, "a prop single grades as win / loss, and as void (stake back) when the player does not play");
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
    const pe = JSON.parse(JSON.stringify(fixture)); pe.night.verdict = "PASS"; pe.night.score = 0; pe.lines.moved = [];
    pe.bets.forEach((x) => { if (x.id === "401909940-to-u") Object.assign(x, { eligible: true, shrunk_ev: 0.028, p_shrunk: 0.54, ev: 0.056 }); if (x.id === "401909940-to-o") Object.assign(x, { eligible: false, shrunk_ev: -0.12, p_shrunk: 0.46 }); });
    fs.writeFileSync(path.join(OUT, "passedge.html"), render(pe));
    const q = await openAt(browser, { viewport: { width: 1280, height: 900 } }, pe);
    await q.goto("file://" + path.join(OUT, "passedge.html") + "#tonight"); await q.waitForTimeout(300);
    const t = await q.evaluate(() => ({ sub: document.getElementById("tSub").textContent, head: document.getElementById("tHead").textContent, rows: window.JG_DEBUG.plan(50).rows.filter((r) => !r.prop).length, all: window.JG_DEBUG.plan(50).rows.length, verdict: document.getElementById("answer").dataset.verdict,
      edge: Array.from(document.querySelectorAll("#mapGames .erow.s-edge")).map((r) => r.querySelector(".act").textContent) }));
    assert(t.rows === 0, "PASS night with an edge: no game bets staked");
    // the night's call is about game bets: a prop with an edge at Fliff's number is still staked on a pass night
    assert(t.all === 1 && t.verdict === "PASS" && /one player prop/.test(t.head), "PASS night with a prop edge: the prop is staked, the call stays pass (" + t.head + ")");
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
  // ---- engine and page price a prop the same way: the page's arithmetic on the published rows equals the engine's
  {
    const rr = await openAt(browser, { viewport: { width: 1280, height: 900 } }, real);
    await rr.goto("file://" + path.join(OUT, "real.html") + "#props"); await rr.waitForTimeout(300);
    const rows = ((real.props || {}).rows || []);
    if (rows.length) {
      const got = await rr.evaluate(() => { const D = window.JG_DEBUG; return Object.keys(D.PRBY).map((id) => { const r = D.PRBY[id], e = D.propNow(r), pr = D.propProbs(r, r.line); return { id, side: e.side, ev: e.ev, src: e.src, confirmed: !!e.confirmed, u: D.takeAm(pr.under, pr.tie), o: D.takeAm(pr.over, pr.tie) }; }); });
      const by = {}; rows.forEach((r) => { by[r.id] = r; });
      const worst = Math.max(...got.map((g) => Math.abs(g.ev - by[g.id].ev)));
      assert(got.length === rows.length && worst < 2e-3 && got.every((g) => g.side === by[g.id].side || Math.abs(g.ev - by[g.id].ev) < 2e-3), "page and engine agree on the value of all " + rows.length + " published props (max EV gap " + worst.toFixed(5) + ")");
      // the published chances are rounded to 4 places, so a price can differ by a cent where the exact price sits on a whole number
      const off = got.filter((g) => Math.abs((g.u == null ? 0 : g.u) - (by[g.id].take_under == null ? 0 : by[g.id].take_under)) > 1 || Math.abs((g.o == null ? 0 : g.o) - (by[g.id].take_over == null ? 0 : by[g.id].take_over)) > 1);
      assert(off.length === 0, "page and engine agree on every take-it price to the cent of rounding (" + off.length + " off)");
      if ((real.props || {}).source === "dk_proxy") assert(got.every((g) => !g.confirmed) && (await rr.evaluate(() => window.JG_DEBUG.propBets().length)) === 0, "real slate without Fliff's board: no prop is staked until a Fliff number is typed");
    }
    // by hand: 56% at -110 is +6.9% a dollar; half the edge is kept (54.2%); quarter Kelly on that is 0.95% of the bankroll
    const hand = await rr.evaluate(() => window.JG_DEBUG.propEval(0.56, 0, -110));
    assert(Math.abs(hand.ev - (0.56 * (1 + 100 / 110) - 1)) < 1e-12 && Math.abs(hand.pCut - (110 / 210 + 0.5 * (0.56 - 110 / 210))) < 1e-12 && Math.abs(hand.frac - 0.25 * (hand.pCut * (210 / 110) - 1) / (100 / 110)) < 1e-12 && hand.ok, "prop arithmetic by hand: 56% at −110 is worth +6.9%, cut to 54.2%, staked at a quarter of Kelly");
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
    assert(await up.evaluate((g) => !document.getElementById("upd").hidden && /newer model run/.test(document.getElementById("upd").textContent) && document.getElementById("jg-data").textContent.includes(g), real.generated), "a newer build while a line is typed is offered, not forced");
    await up.click("#updGo"); await up.waitForTimeout(900);
    assert(await up.evaluate((g) => document.getElementById("jg-data").textContent.includes(g) && !!window.JG_DEBUG && document.getElementById("tHead").textContent.length > 0, later), "the newer build is loaded in place and runs");
    await up.close();
  }
  // ---- tomorrow's slate rides along: a switch shows it, the choice holds for the tab, and tonight is the default while it has a game to bet
  {
    const nextSlate = JSON.parse(JSON.stringify(fixture)), dayMs = 24 * 3600 * 1000, shift = (iso) => new Date(Date.parse(iso) + dayMs).toISOString().replace(".000Z", "Z");
    nextSlate.date = "2026-11-08"; nextSlate.run_label = "opener"; nextSlate.generated = "2026-11-07T19:29:50+00:00";
    nextSlate.games.forEach((g) => { g.tip = shift(g.tip); }); nextSlate.bets.forEach((b) => { if (b.tip) b.tip = shift(b.tip); }); nextSlate.props.rows.forEach((r) => { r.tip = shift(r.tip); });
    nextSlate.games = nextSlate.games.slice(0, 3);
    fs.writeFileSync(path.join(OUT, "twoday.html"), render(fixture, { next: nextSlate, build: nextSlate.generated }));
    const file = "file://" + path.join(OUT, "twoday.html");
    const d = await openAt(browser, { viewport: { width: 1280, height: 900 } }, fixture);
    const derr = []; d.on("pageerror", (e) => derr.push(String(e)));
    await d.goto(file + "#tonight"); await d.waitForTimeout(300);
    const a0 = await d.evaluate(() => ({ view: window.JG_DEBUG.VIEWDAY, date: window.JG_DEBUG.date, hidden: document.getElementById("daySwitch").hidden, btns: Array.from(document.querySelectorAll("#daySwitch button")).map((b) => [b.dataset.day, b.getAttribute("aria-pressed")]), head: document.getElementById("tHead").textContent }));
    assert(a0.view === "main" && a0.date === fixture.date && !a0.hidden && a0.btns.length === 2 && a0.btns[0][1] === "true" && a0.btns[1][0] === "2026-11-08", "with tomorrow priced, a switch offers both days and tonight is shown first");
    await d.fill("#amount", "20");
    await d.click("#daySwitch button[data-day='2026-11-08']"); await d.waitForTimeout(500);
    const a1 = await d.evaluate(() => ({ view: window.JG_DEBUG.VIEWDAY, date: window.JG_DEBUG.date, games: document.querySelectorAll("#mapGames .erow").length, kicker: document.getElementById("tDate").textContent, label: document.getElementById("amountLabel").textContent, pressed: document.querySelector("#daySwitch button[aria-pressed='true']").dataset.day, started: Array.from(document.querySelectorAll("#mapGames .erow .tag")).filter((x) => x.textContent === "Started").length, amount: document.getElementById("amount").value }));
    assert(a1.view === "next" && a1.date === "2026-11-08" && a1.games === 3 && a1.pressed === "2026-11-08" && a1.started === 0 && /Nov 8/.test(a1.kicker), "the switch loads tomorrow's slate: its date, its 3 games, none started (" + a1.kicker.slice(0, 40) + ")");
    assert(!/tonight\?/.test(a1.label) && a1.amount === "20", "tomorrow's view asks about that day's games and keeps the amount");
    // a Fliff number typed for tomorrow stays with tomorrow
    const rid = nextSlate.props.rows.find((r) => r.ref === "dk").id;
    await d.evaluate((i) => window.JG_DEBUG.setTyped(i, { line: window.JG_DEBUG.PRBY[i].line, over: null, under: 120, at: 1 }), rid);
    await d.goto(file + "#games"); await d.waitForTimeout(300);
    assert(await d.evaluate(() => window.JG_DEBUG.VIEWDAY === "next" && getComputedStyle(document.getElementById("daySwitch")).display !== "none"), "the choice holds across tabs and reloads, and the switch is on the Games tab too");
    await d.goto(file + "#tonight"); await d.waitForTimeout(300);
    await d.click("#daySwitch button[data-day='" + fixture.date + "']"); await d.waitForTimeout(500);
    const a2 = await d.evaluate((i) => ({ view: window.JG_DEBUG.VIEWDAY, date: window.JG_DEBUG.date, typedHere: Object.keys(window.JG_DEBUG.typed()).length, kept: !!(JSON.parse(localStorage.getItem("jg.fliff.2026-11-08") || "{}").rows || {})[i] }), rid);
    assert(a2.view === "main" && a2.date === fixture.date && a2.typedHere === 0 && a2.kept, "switching back shows tonight; a number typed for tomorrow is kept for tomorrow only");
    await d.goto(file + "#method"); await d.waitForTimeout(200);
    assert(await d.evaluate(() => getComputedStyle(document.getElementById("daySwitch")).display === "none"), "the switch is hidden on tabs that are not about a slate");
    assert(derr.length === 0, "no page errors with two slates: " + JSON.stringify(derr));
    await d.close();
    // once every game tonight has tipped, a fresh tab opens on tomorrow
    const late = await openAt(browser, { viewport: { width: 1280, height: 900 } }, fixture, 9);
    await late.goto(file + "#tonight"); await late.waitForTimeout(300);
    assert(await late.evaluate(() => window.JG_DEBUG.VIEWDAY === "next" && document.querySelectorAll("#mapGames .erow").length === 3), "after tonight's last tip the page opens on tomorrow's slate");
    await late.close();
    // one slate only: no switch
    const one = await openAt(browser, { viewport: { width: 1280, height: 900 } }, fixture);
    await one.goto("file://" + path.join(OUT, "fixture.html") + "#tonight"); await one.waitForTimeout(200);
    assert(await one.evaluate(() => document.getElementById("daySwitch").hidden && window.JG_DEBUG.VIEWDAY === "main"), "with one slate there is no switch");
    await one.close();
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
      assert(/is our chance this bet wins/.test(await ex.evaluate(() => document.getElementById("mapProps").textContent)) && /stand-in/.test(await ex.evaluate(() => document.getElementById("mapProps").textContent)), "tapping a prop's bar explains its percentage and says the number is a stand-in");
    }
    await ex.goto("file://" + path.join(OUT, "real.html") + "#method/percent"); await ex.waitForTimeout(300);
    const at60 = await ex.evaluate(() => document.getElementById("pctOut").textContent);
    assert(/60 wins in 100/.test(at60) && /1\.15×/.test(at60) && /\+\$14\.55/.test(at60) && /1\.08×/.test(at60), "the explainer at 60%: a -110 bet returns 1.15x (+$14.55 per 100 bets), a -125 prop 1.08x");
    await ex.fill("#pctSlider", "52"); await ex.dispatchEvent("#pctSlider", "input"); await ex.waitForTimeout(100);
    assert(/−\$0\.73/.test(await ex.evaluate(() => document.getElementById("pctOut").textContent)), "the explainer at 52%: a -110 total loses $0.73 per 100 bets");
    await ex.close();
  }
  // ---- the real slate: a PASS night stakes no game bets at any amount
  const r = await openAt(browser, { viewport: { width: 1280, height: 900 } }, real);
  await r.goto("file://" + path.join(OUT, "real.html") + "#tonight"); await r.waitForTimeout(300);
  if (real.night && String(real.night.verdict).toUpperCase() === "PASS") assert(await r.evaluate(() => [10, 100, 1000].every((a) => window.JG_DEBUG.plan(a).rows.length === 0)), "real slate (PASS): no game bets at any amount");
  // with nothing firm to bet, the headline still sends you to the props worth checking
  {
    const rs = await r.evaluate(() => ({ head: document.getElementById("tHead").textContent, n: window.JG_DEBUG.plan(10).checks.length, rows: window.JG_DEBUG.plan(10).rows.length, live: Object.values(window.JG_DEBUG.PRBY).filter((x) => Date.parse(x.tip) > Date.now()).length }));
    if (rs.rows === 0 && rs.live > 0) assert(rs.n > 0 && /check (one prop|\d+ props) on Fliff/.test(rs.head), "real slate: no firm bets, so the headline points to " + rs.n + " props to check (" + rs.head + ")");
  }
  await r.close();
  await browser.close();
  console.log("\n" + (checks - failures) + "/" + checks + " checks passed; screenshots in " + OUT);
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
