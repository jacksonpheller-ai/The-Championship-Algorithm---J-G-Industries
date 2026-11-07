// Dashboard checks with playwright (node tests/test_site.js [outdir]).
// Renders jghedge/site/template.html the way jghedge/site/__init__.py does, against three slates:
//   fixture  tests/fixtures/latest_sample.json (full data contract: policy, night, pickem, fliff, growth, lines)
//   real     docs/data/latest.json (whatever the nightly job last produced; new blocks may be missing)
//   empty    {} (no slate at all)
// and asserts: no console/page errors, no horizontal overflow at 390px on every tab, the custom-slip
// hit-count distribution computed in the browser from pickem.sims.bits equals the slips' p_dist in the
// JSON, one leg per player is enforced, the tier/opt-in rules, and the verdict colour classes.
// Writes light/dark screenshots of Tonight and the PrizePicks panel at 1280px into outdir.
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

(async () => {
  const fixture = readJson(path.join(ROOT, "tests/fixtures/latest_sample.json"));
  const real = readJson(path.join(ROOT, "docs/data/latest.json"));
  const pages = { fixture: render(fixture), real: render(real), empty: render({}) };
  for (const k of Object.keys(pages)) fs.writeFileSync(path.join(OUT, k + ".html"), pages[k]);
  const browser = await chromium.launch();
  for (const name of Object.keys(pages)) {
    const file = "file://" + path.join(OUT, name + ".html");
    // ---- errors + overflow at phone width on every tab, dark scheme
    const errs = [];
    const phone = await browser.newPage({ viewport: { width: 390, height: 844 }, colorScheme: "dark" });
    phone.on("pageerror", (e) => errs.push(String(e))); phone.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
    for (const v of ["desk", "games", "props", "record", "method"]) {
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
    assert(errs.length === 0, name + " no console/page errors: " + JSON.stringify(errs));
    await phone.close();
    // ---- desktop screenshots light + dark (Tonight full page, PrizePicks panel)
    for (const scheme of ["light", "dark"]) {
      const p = await browser.newPage({ viewport: { width: 1280, height: 900 }, colorScheme: scheme });
      p.on("pageerror", (e) => errs.push(String(e)));
      await p.goto(file + "#desk"); await p.waitForTimeout(600);
      if (name !== "empty") {
        await p.screenshot({ path: path.join(OUT, name + "_tonight_" + scheme + ".png"), fullPage: true });
        const panel = await p.$("#pickemPanel"); if (panel) await panel.screenshot({ path: path.join(OUT, name + "_prizepicks_" + scheme + ".png") });
      }
      await p.close();
    }
  }
  // ---- functional checks against the fixture
  const p = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const perr = []; p.on("pageerror", (e) => perr.push(String(e)));
  await p.goto("file://" + path.join(OUT, "fixture.html") + "#desk"); await p.waitForTimeout(400);
  const verdictCls = await p.evaluate(() => document.getElementById("verdict").className);
  assert(verdictCls === "verdict v-" + fixture.night.verdict.toLowerCase(), "verdict banner carries class for " + fixture.night.verdict + " (" + verdictCls + ")");
  assert(await p.evaluate(() => !document.getElementById("verdict").textContent.includes("undefined")), "verdict banner has no undefined text");
  // bit matrix: browser distribution == fixture p_dist for every slip (raw), and sums to 1
  for (const s of fixture.pickem.slips) {
    const d = await p.evaluate((ids) => window.JG_DEBUG.hitDist(ids, false), s.legs);
    const maxDiff = Math.max(...d.map((x, i) => Math.abs(x - s.p_dist[i])));
    assert(maxDiff < 1e-3, "custom-slip distribution from bits matches " + s.type + " p_dist (max diff " + maxDiff.toFixed(5) + ")");
    assert(Math.abs(d.reduce((a, b) => a + b, 0) - 1) < 1e-9, s.type + " distribution sums to 1");
  }
  // shrunk distribution: P(all hit) must not exceed the raw one
  { const s = fixture.pickem.slips[0]; const raw = await p.evaluate((ids) => window.JG_DEBUG.hitDist(ids, false), s.legs), shr = await p.evaluate((ids) => window.JG_DEBUG.hitDist(ids, true), s.legs);
    assert(shr[shr.length - 1] <= raw[raw.length - 1] + 1e-9, "shrunk P(all hit) <= raw P(all hit)"); }
  // tick two legs through the UI and read the custom-slip output
  const legIds = fixture.pickem.slips[0].legs;
  for (const id of legIds) await p.check("input[data-pk='" + id + "']");
  const outTxt = await p.evaluate(() => document.getElementById("pkCustomOut").textContent);
  assert(/Expected payout/.test(outTxt) && /Growth at/.test(outTxt), "custom slip renders EV and growth for two ticked legs");
  // one leg per player: a second leg for an already-ticked player is disabled
  const legs = fixture.pickem.legs, dupPlayer = legs.find((l) => legs.filter((x) => x.player_id === l.player_id).length > 1);
  if (dupPlayer) {
    await p.evaluate(() => { window.JG_DEBUG.pk.ticked = []; });
    await p.click("#pkClear");
    await p.check("input[data-pk='" + dupPlayer.id + "']");
    const other = legs.find((l) => l.player_id === dupPlayer.player_id && l.id !== dupPlayer.id);
    const disabled = await p.evaluate((id) => document.querySelector("input[data-pk='" + id + "']").disabled, other.id);
    assert(disabled, "one leg per player: second leg for " + dupPlayer.player + " is disabled once the first is ticked");
  }
  // min-entry warning at a small bankroll; tier and opt-in rules
  await p.fill("#bankroll", "10"); await p.waitForTimeout(150);
  assert(await p.evaluate(() => /10% of your bankroll|of your bankroll/.test(document.getElementById("pkCustomOut").textContent)), "warns when PrizePicks min entry exceeds 10% of a $10 bankroll");
  let tierTxt = await p.evaluate(() => document.getElementById("tierBox").textContent);
  assert(/tiny/.test(tierTxt), "$10 at $0.50 units shows the tiny tier");
  assert(await p.evaluate(() => !document.getElementById("optIn")), "no opt-in toggle below 100 units");
  await p.fill("#bankroll", "500"); await p.waitForTimeout(150);
  tierTxt = await p.evaluate(() => document.getElementById("tierBox").textContent);
  assert(/standard/.test(tierTxt), "$500 at $0.50 units shows the standard tier");
  const optInPresent = await p.evaluate(() => !!document.getElementById("optIn"));
  assert(optInPresent, "opt-in toggle appears at >= 100 units");
  if (fixture.night.verdict === "LIGHT") assert(await p.evaluate(() => document.getElementById("optIn").disabled), "opt-in is disabled on a LIGHT night");
  // optimiser excludes props and info-only bets, uses shrunk means
  const ids = await p.evaluate(() => window.JG_DEBUG.IDS);
  const byId = {}; fixture.bets.forEach((b) => { byId[b.id] = b; });
  assert(ids.every((id) => !byId[id] || (byId[id].kind === "total" && !byId[id].info_only)), "optimiser candidates are totals and parlays only (no props, no info-only)");
  const r = await p.evaluate(() => window.JG_DEBUG.optimise());
  const staked = Object.values(r.stakes).reduce((a, b) => a + b, 0);
  assert(staked <= 500 * (fixture.policy.night_cap) + 1e-9, "night cap respected at $500 (staked $" + staked + ")");
  assert(Object.values(r.stakes).every((s) => s <= 500 * fixture.policy.per_bet_cap + 1e-9), "per-bet cap respected at $500");
  // growth panel picks a row
  assert(await p.evaluate(() => /Median end/.test(document.getElementById("growthPanel").textContent)), "growth plan renders tiles from the grid");
  // Fliff instruction text shows the max line
  assert(await p.evaluate(() => /or better; skip/.test(document.getElementById("ticket").textContent)), "ticket rows carry the 'or better; skip' instruction");
  // moved-past list in the banner
  if ((fixture.lines.moved || []).some((m) => m.still_ok === false)) assert(await p.evaluate(() => /moved past our number/i.test(document.getElementById("verdict").textContent)), "banner lists lines moved past our number");
  assert(perr.length === 0, "no page errors during interaction: " + JSON.stringify(perr));
  await p.close();
  await browser.close();
  console.log("\n" + (checks - failures) + "/" + checks + " checks passed; screenshots in " + OUT);
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
