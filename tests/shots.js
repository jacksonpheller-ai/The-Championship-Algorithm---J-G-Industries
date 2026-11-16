// Screenshots of every page state at 390x844 and 1280x900 (node tests/shots.js [outdir]).
// States: no board yet, board read (not saved), saving, saved, from partner, board stale, not a board, pass night,
// all games tipped, no games. Each tab is also shot at both sizes. Also prints the Tonight tab's height.
const fs = require("fs"), path = require("path");
const { chromium } = require("playwright");
const ROOT = path.resolve(__dirname, "..");
const OUT = process.argv[2] || path.join(ROOT, "dist", "shots");
fs.mkdirSync(OUT, { recursive: true });
const readJson = (p) => (fs.existsSync(p) ? JSON.parse(fs.readFileSync(p, "utf8")) : {});
const template = fs.readFileSync(path.join(ROOT, "jghedge/site/template.html"), "utf8");
function render(slate, extra) {
  const data = Object.assign({ slate, ledger: readJson(path.join(ROOT, "docs/data/ledger.json")), backtest: readJson(path.join(ROOT, "docs/data/backtest.json")),
    replay: readJson(path.join(ROOT, "docs/data/replay_open.json")), results: {}, shared: readJson(path.join(ROOT, "docs/data/shared_ledger.json")), repo: "https://github.com/example/repo", pages_url: "", raw_url: "", live: false }, extra || {});
  return "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body>" + template.replace('"__JG_DATA__"', JSON.stringify(data).replace(/<\//g, "<\\/")) + "</body></html>";
}
const real = readJson(path.join(ROOT, "docs/data/latest.json"));
const PPNAME = { pts: "Points", reb: "Rebounds", ast: "Assists", fg3m: "3-PT Made", pra: "Pts+Rebs+Asts", pr: "Pts+Rebs", pa: "Pts+Asts", ra: "Rebs+Asts", stl: "Steals", blk: "Blocked Shots", tov: "Turnovers" };
function boardFrom(slate) {   // a board shaped like PrizePicks' (jghedge/data/prizepicks.py): the pool's lines, a few demons and goblins, two unknown players
  const pool = ((slate.pickem || {}).pool || []).filter((r) => PPNAME[r.stat]);
  const data = [], inc = {};
  const add = (i, name, team, stat, line, odds, tip) => { data.push({ type: "projection", id: "p" + i, attributes: { line_score: line, stat_type: stat, odds_type: odds, start_time: tip, status: "pre_game" }, relationships: { new_player: { data: { type: "new_player", id: "n" + name } } } }); inc[name] = { type: "new_player", id: "n" + name, attributes: { display_name: name, team } }; };
  pool.forEach((r, i) => { add(i, r.player, r.team, PPNAME[r.stat], r.line, "standard", r.tip); if (i % 9 === 0) add(5000 + i, r.player, r.team, PPNAME[r.stat], r.line + 3, "demon", r.tip); if (i % 11 === 0) add(7000 + i, r.player, r.team, PPNAME[r.stat], r.line - 3, "goblin", r.tip); });
  add(9001, "Bench Player One", "XXX", "Points", 6.5, "standard", (pool[0] || {}).tip); add(9002, "Bench Player Two", "XXX", "Rebounds", 3.5, "standard", (pool[0] || {}).tip);
  return { data, included: Object.values(inc), links: {} };
}
(async () => {
  const b = await chromium.launch();
  const SIZES = { phone: { width: 390, height: 844 }, desk: { width: 1280, height: 900 } };
  const states = {};
  const shot = async (name, slate, extra, hash, prep, offsetH) => {
    const html = path.join(OUT, "_" + name + ".html"); fs.writeFileSync(html, render(slate, extra));
    for (const [sz, vp] of Object.entries(SIZES)) {
      const pg = await b.newPage({ viewport: vp, deviceScaleFactor: 1 });
      if (slate && slate.generated) await pg.clock.setFixedTime(new Date(Date.parse(slate.generated) + (offsetH || 0) * 3600e3));
      await pg.route("https://**", (r) => r.fulfill({ status: 404, body: "" }));
      await pg.goto("file://" + html + "#" + (hash || "tonight")); await pg.waitForTimeout(400);
      if (prep) { await prep(pg); await pg.waitForTimeout(500); }
      const h = await pg.evaluate(() => document.documentElement.scrollHeight);
      await pg.screenshot({ path: path.join(OUT, name + "-" + sz + ".png"), fullPage: false });
      if (sz === "phone") await pg.screenshot({ path: path.join(OUT, name + "-" + sz + "-full.png"), fullPage: true });
      states[name + "-" + sz] = h; await pg.close();
    }
  };
  const board = JSON.stringify(boardFrom(real));
  const paste = (pg) => pg.evaluate((t) => window.JG_DEBUG.readPasted(t), board);
  await shot("no-board", real, {}, "tonight");
  await shot("board-read", real, {}, "tonight", paste);
  await shot("saving", real, {}, "tonight", async (pg) => { await paste(pg); await pg.waitForTimeout(300); await pg.evaluate(() => { const a = document.querySelector("#ppBarT a[data-pp='save']"); a.removeAttribute("target"); a.href = "#tonight"; a.click(); }); });
  await shot("save-failed", real, {}, "tonight", async (pg) => { await paste(pg); await pg.waitForTimeout(300); await pg.evaluate(() => { const a = document.querySelector("#ppBarT a[data-pp='save']"); a.removeAttribute("target"); a.href = "#tonight"; a.click(); const s = window.JG_DEBUG.save(); s.state = "failed"; window.JG_DEBUG.refreshViews(); }); });
  await shot("not-a-board", real, {}, "tonight", (pg) => pg.evaluate(() => window.JG_DEBUG.readPasted("hello")));
  await shot("props-board", real, {}, "props", paste);
  for (const t of ["games", "record", "method"]) await shot("tab-" + t, real, {}, t);
  await shot("all-tipped", real, {}, "tonight", null, 12);
  await shot("no-games", {}, {}, "tonight");
  console.log(JSON.stringify(states));
  await b.close();
})().catch((e) => { console.error(e); process.exit(2); });
