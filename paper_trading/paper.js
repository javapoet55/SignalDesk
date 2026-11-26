"use strict";
const $ = (id) => document.getElementById(id);
const names = [
  "Overview",
  "Trading Workspace",
  "Signal Manager",
  "Open Positions",
  "Order History",
  "Performance Analytics",
  "Trading Journal",
  "Portfolio Settings",
];
const research = new URLSearchParams(location.search).get("research") || "";
let active = names.includes(decodeURIComponent(location.hash.slice(1)))
  ? decodeURIComponent(location.hash.slice(1))
  : research
    ? names[1]
    : names[0];
let security = {},
  policy = {},
  barCount = 60,
  startDate = "",
  endDate = "";
let portfolios = [],
  pid = "",
  version = 0,
  summary = null,
  selected = "",
  ticket = null,
  offset = 0,
  filter = "",
  sort = "",
  ascending = true;
const esc = (v) =>
  String(v ?? "N/A").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const cash = (v) =>
  v == null
    ? "N/A"
    : Number(v).toLocaleString("en-US", { style: "currency", currency: "USD" });
const date = (v) => (v ? new Date(v * 1000).toLocaleString() : "N/A");
const box = (title, body) =>
  '<section class="pbox"><h2>' + esc(title) + "</h2>" + body + "</section>";
const input = (label, name, type = "text", value = "", attrs = "") =>
  "<label>" +
  esc(label) +
  '<input name="' +
  name +
  '" type="' +
  type +
  '" value="' +
  esc(value) +
  '" ' +
  attrs +
  "></label>";
const button = (label, action, id = "") =>
  '<button type="button" data-action="' +
  action +
  '" data-id="' +
  esc(id) +
  '">' +
  esc(label) +
  "</button>";
const metrics = (pairs) =>
  '<div class="metrics">' +
  pairs
    .map(
      ([k, v]) =>
        "<div><small>" +
        esc(k) +
        "</small><strong>" +
        esc(v) +
        "</strong></div>",
    )
    .join("") +
  "</div>";
function msg(s, error = false) {
  if (error && $("content")?.textContent === "Loading…")
    $("content").textContent =
      "This view could not be loaded. Select a tab to retry.";
  $("message").className = error ? "error" : "";
  $("message").textContent = s;
}
async function api(path, data) {
  const response = await fetch(
    "/paper/api/" + path,
    data === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Paper-Request": "1",
          },
          body: JSON.stringify(data),
        },
  );
  if (response.redirected) {
    location.assign(response.url);
    throw Error("Session expired.");
  }
  const result = await response.json();
  if (!response.ok) throw Error(result.error || "Request failed.");
  return result;
}
const resource = (name, data) => api("portfolios/" + pid + "/" + name, data);
function table(records, cols, actions = null) {
  if (!records.length) return '<p class="muted">No matching records.</p>';
  return (
    '<div class="ptable"><table><thead><tr>' +
    cols.map(([k, l]) => '<th scope="col">' + esc(l) + "</th>").join("") +
    (actions ? "<th>Actions</th>" : "") +
    "</tr></thead><tbody>" +
    records
      .map(
        (r) =>
          "<tr>" +
          cols
            .map(
              ([k, l, fmt]) =>
                "<td>" + esc(fmt ? fmt(r[k], r) : r[k]) + "</td>",
            )
            .join("") +
          (actions ? "<td>" + actions(r) + "</td>" : "") +
          "</tr>",
      )
      .join("") +
    "</tbody></table></div>"
  );
}
const posCols = [
  ["symbol", "Symbol"],
  ["company", "Company", (_, r) => security[r.symbol]?.company || "N/A"],
  ["qty", "Shares"],
  ["average", "Average cost", cash],
  ["price", "Reference", cash],
  ["value", "Value", cash],
  ["cost", "Total cost", cash],
  ["unrealized", "Open P&L", cash],
  [
    "pct",
    "Return %",
    (_, r) =>
      r.unrealized == null ? "N/A" : ((100 * r.unrealized) / r.cost).toFixed(2),
  ],
  ["allocation", "Allocation %"],
  ["opened", "Held since", date],
  ["stamp", "Price as of", date],
];
const fillCols = [
  ["symbol", "Symbol"],
  ["side", "Side"],
  ["qty", "Filled shares"],
  ["price", "Fill price", cash],
  ["notional", "Value", cash],
  ["fee", "Fee", cash],
  ["realized", "Realized P&L", cash],
  ["created", "Executed", date],
  ["source", "Source"],
  ["id", "Transaction ID"],
  ["signal", "Signal ID"],
  [
    "requested",
    "Requested shares",
    (_, r) => (r.preview ? JSON.parse(r.preview).qty : r.qty),
  ],
  ["slippage", "Simulated slippage", () => cash(0)],
  ["signal_status", "Status", (v) => v || "Simulated"],
];
function newPortfolio() {
  return box(
    "Create a virtual portfolio",
    '<form id="create">' +
      input("Portfolio name", "name", "text", "", 'required maxlength="80"') +
      input(
        "Initial virtual capital (USD)",
        "capital",
        "number",
        "100000.00",
        'required min=".01" step=".01"',
      ) +
      input("Description", "description") +
      input("Benchmark", "benchmark", "text", "SPY", "required") +
      input(
        "Simulated fee per fill (USD)",
        "fee",
        "number",
        0,
        'min="0" step=".01"',
      ) +
      '<label><span><input name="fractional" type="checkbox" checked> Allow fractional shares</span></label><button class="primary">Create portfolio</button></form>',
  );
}
async function init(prefer = "") {
  [portfolios, security, policy] = await Promise.all([
    api("portfolios"),
    api("securities"),
    api("entitlements"),
  ]);
  pid =
    portfolios.find((p) => p.id === prefer)?.id ||
    portfolios.find((p) => p.status === "active")?.id ||
    portfolios[0]?.id ||
    "";
  $("portfolio").innerHTML = portfolios
    .map(
      (p) =>
        '<option value="' +
        esc(p.id) +
        '">' +
        esc(p.name) +
        (p.status === "archived" ? " (archived)" : "") +
        "</option>",
    )
    .join("");
  $("portfolio").value = pid;
  await draw();
}
function go(name) {
  active = name;
  history.replaceState(null, "", "#" + encodeURIComponent(name));
  offset = 0;
  filter = "";
  sort = "";
  startDate = "";
  endDate = "";
  ticket = null;
  draw().catch((e) => msg(e.message, true));
}
async function draw() {
  const gen = ++version;
  $("heading").textContent = active;
  $("tabs").innerHTML = names
    .map(
      (n) =>
        '<button data-tab="' +
        n +
        '" ' +
        (active === n ? 'aria-current="page"' : "") +
        ">" +
        n +
        "</button>",
    )
    .join("");
  $("content").innerHTML =
    '<div class="muted paper-loading" role="status">Loading…</div>';
  if (!pid) {
    $("content").innerHTML = newPortfolio();
    return;
  }
  const needsMarks = [names[0], names[1], names[3], names[5]].includes(active);
  const v = needsMarks
    ? await resource("summary")
    : { portfolio: await resource("settings") };
  if (gen !== version) return;
  summary = v;
  let html = "";
  if (active === names[0]) {
    const [signals, fills, stats] = await Promise.all([
      resource("signals?limit=5"),
      resource("transactions?limit=5"),
      resource("analytics"),
    ]);
    html =
      metrics([
        ["Portfolio value", cash(v.equity)],
        ["Available cash", cash(v.portfolio.cash)],
        ["Invested value", cash(v.invested)],
        ["Total P&L", cash(v.pnl)],
        ["Initial capital", cash(v.portfolio.capital)],
        ["Realized P&L", cash(v.realized)],
        ["Unrealized P&L", cash(v.unrealized)],
        ["Return", v.return_pct == null ? "N/A" : v.return_pct + "%"],
        ["Daily P&L", cash(stats.daily_pnl)],
        ["Open positions", v.positions.length],
        ["Pending signals", v.pending],
        [
          "Oldest position mark",
          date(Math.min(...v.positions.map((r) => r.stamp || 0)) || 0).replace(
            "Invalid Date",
            "N/A",
          ),
        ],
      ]) +
      chartBox(stats) +
      box("Current positions", table(v.positions, posCols.slice(0, 7))) +
      box("Recent transactions", table(fills, fillCols.slice(0, 7))) +
      box("Recent signals", signalTable(signals)) +
      alloc(v);
  } else if (active === names[1]) {
    const watch = await api(
      "watchlist?research=" + encodeURIComponent(research),
    );
    if (gen !== version) return;
    selected = selected || watch[0] || "";
    html =
      '<div class="pworkspace">' +
      box(
        "Watchlist",
        input("Filter symbols", "watchSearch", "search") +
          '<div class="watch">' +
          watch
            .map((t) => {
              const m = security[t] || {},
                pct = m.change_today_pct,
                change =
                  pct != null && m.price != null && Number(pct) !== -100
                    ? m.price - m.price / (1 + pct / 100)
                    : null;
              return button(
                [
                  t,
                  m.company,
                  m.price == null ? "" : cash(m.price),
                  change == null ? "" : cash(change),
                  pct == null ? "" : pct + "%",
                ]
                  .filter(Boolean)
                  .join(" · "),
                "ticker",
                t,
              );
            })
            .join("") +
          "</div>" +
          (watch.length
            ? "<small>Prices and daily changes from the latest saved scan.</small>"
            : '<p class="muted">No saved scan tickers. Enter a symbol in the ticket.</p>'),
      ) +
      box(
        selected || "Price history",
        '<div id="candles">Select a symbol to view completed minute bars.</div><div id="linkedResearch"></div>',
      ) +
      box(
        "Manual signal",
        "<p class='muted'>Available cash · " +
          esc(cash(v.portfolio.cash)) +
          "</p>" +
          signalForm(),
      ) +
      '</div><div id="preview"></div>';
  } else if (active === names[2]) {
    const rs = await resource("signals?" + params());
    const exits = await resource("exits");
    html =
      filters() +
      box("Signals", signalTable(rs, true)) +
      pager(rs) +
      box(
        "Conditional sells",
        table(
          exits,
          [
            ["symbol", "Symbol"],
            ["qty", "Shares"],
            ["buy_price", "Buy price"],
            ["target", "Gain target"],
            ["stop", "Stop"],
            ["floor", "Sell limit"],
            ["status", "Status"],
          ],
          (r) =>
            ["Armed", "Stop triggered"].includes(r.status)
              ? button("Cancel exit", "cancel-exit", r.entry)
              : "",
        ),
      );
  } else if (active === names[3]) {
    html =
      filters() +
      box(
        "Open positions",
        table(filtered(v.positions).slice(offset, offset + 100), posCols, (r) =>
          button("Transactions", "position", r.symbol),
        ),
      ) +
      pager(filtered(v.positions).slice(offset, offset + 100)) +
      alloc(v);
  } else if (active === names[4]) {
    const [fills, orders] = await Promise.all([
      resource("transactions?" + params()),
      resource("orders?" + params()),
    ]);
    html =
      filters() +
      box("Simulated fills", table(fills, fillCols)) +
      box(
        "Order outcomes",
        table(orders, [
          ["id", "Signal / order ID"],
          ["status", "Status"],
          ["reason", "Reason"],
          ["created", "Processed", date],
        ]),
      ) +
      pager(fills.length > orders.length ? fills : orders);
  } else if (active === names[5]) {
    const st = await resource("analytics");
    html =
      chartBox(st) +
      metrics(Object.entries(st.metrics)) +
      alloc(v) +
      box("Monthly returns", tiles(st.monthly, "month", "pct", "%")) +
      box(
        "Daily profit and loss",
        valueChart(
          st.daily.map((r) => ({ label: r.day, value: Number(r.pnl) })),
          true,
        ),
      ) +
      box(
        "Historical portfolio value",
        valueChart(
          st.series.map((r) => ({
            label: date(r.created),
            value: Number(r.equity),
          })),
          false,
        ),
      ) +
      sectorChart(v) +
      box("Methodology", "<p>" + esc(st.method) + "</p>") +
      box(
        "Compare portfolios",
        '<button data-action="compare">Compare portfolio returns</button><div id="comparison"></div>',
      );
  } else if (active === names[6]) {
    const rs = await resource("journal?limit=100&offset=" + offset);
    const fields = [
      "hypothesis",
      "rationale",
      "strategy",
      "entry",
      "exit",
      "tags",
      "observations",
      "review",
      "lessons",
    ];
    html =
      box(
        "Research entry",
        '<form id="journal">' +
          input("Related signal ID (optional)", "signal") +
          fields
            .map((f) => input(f[0].toUpperCase() + f.slice(1), f))
            .join("") +
          '<button class="primary">Save journal entry</button></form>',
      ) +
      box(
        "Journal entries",
        rs
          .map(
            (r) =>
              "<article><h3>" +
              esc(date(r.created)) +
              "</h3>" +
              Object.entries(JSON.parse(r.fields))
                .filter(([, v]) => v)
                .map(([k, v]) => "<p><b>" + esc(k) + "</b> " + esc(v) + "</p>")
                .join("") +
              "</article>",
          )
          .join("") || "<p>No journal entries.</p>",
      ) +
      pager(rs);
  } else {
    const p = v.portfolio,
      ops = await resource("operations");
    html =
      box(
        "Portfolio settings",
        '<form id="settings">' +
          input("Name", "name", "text", p.name, "required") +
          input("Description", "description", "text", p.description) +
          input(
            "Simulated fee (USD)",
            "fee",
            "number",
            p.fee,
            'step=".01" min="0"',
          ) +
          '<label><span><input type="checkbox" name="fractional" ' +
          (p.fractional ? "checked" : "") +
          "> Fractional shares</span></label><p>" +
          esc(p.currency) +
          " · " +
          esc(p.benchmark) +
          " · " +
          esc(p.status) +
          " · Created " +
          esc(date(p.created)) +
          "</p><button>Save settings</button></form>",
      ) +
      newPortfolio() +
      box(
        "Ledger monitoring",
        metrics([
          ["Last snapshot", date(ops.last_snapshot)],
          ["Rejected signals", ops.signals.Rejected || 0],
          ["Expired signals", ops.signals.Expired || 0],
          ["Storage", ops.storage],
        ]),
      ) +
      box(
        "Record a corporate action",
        '<form id="action">' +
          input("Position symbol", "symbol", "text", "", "required") +
          '<label>Event<select name="kind"><option value="split">Stock split</option><option value="dividend">Cash dividend</option></select></label>' +
          input(
            "New shares per old share, or USD per share",
            "value",
            "number",
            "",
            'required min=".000001" step="any"',
          ) +
          input(
            "Published source and effective date",
            "reference",
            "text",
            "",
            "required",
          ) +
          input(
            "Retype symbol to confirm",
            "confirm_symbol",
            "text",
            "",
            "required",
          ) +
          "<button>Apply to current holdings</button></form>",
      ) +
      box(
        "Portfolio history",
        "<p>Reset archives this ledger and opens a fresh portfolio with the same starting capital.</p>" +
          button("Archive portfolio", "archive") +
          " " +
          button("Reset portfolio", "reset"),
      );
  }
  if (gen !== version) return;
  if (
    (active === names[6] && policy.ai) ||
    (active === names[5] && policy.compute_credits > 0)
  )
    html += box(
      "Research tools",
      (active === names[6]
        ? button("Summarize simulated results", "ai")
        : button("Run licensed benchmark study", "historical")) +
        '<p id="researchOutput"></p>',
    );
  $("content").innerHTML = html;
  if ($("chart")) renderEquity();
  if (active === names[1] && selected) await candles(gen);
}
function tiles(rs, label, value, unit) {
  if (!rs.length) return '<p class="muted">Insufficient history.</p>';
  return (
    '<div class="tiles">' +
    rs
      .map(
        (r) =>
          '<span class="' +
          (Number(r[value]) >= 0 ? "gain" : "loss") +
          '">' +
          esc(r[label]) +
          "<b>" +
          esc(r[value]) +
          esc(unit) +
          "</b></span>",
      )
      .join("") +
    "</div>"
  );
}
function valueChart(points, bars) {
  if (points.length < 2) return '<p class="muted">Insufficient history.</p>';
  const low = Math.min(...points.map((r) => r.value), ...(bars ? [0] : [])),
    high = Math.max(...points.map((r) => r.value), ...(bars ? [0] : []));
  const y = (v) => 12 + (95 * (high - v)) / (high - low || 1),
    w = 530 / points.length,
    x = (i) => 10 + w * (i + 0.5);
  const line = bars
    ? ""
    : '<polyline fill="none" stroke="#427dc4" stroke-width="2" points="' +
      points.map((r, i) => x(i) + "," + y(r.value)).join(" ") +
      '"/>';
  const marks = points
    .map((r, i) => {
      const label =
        "<title>" + esc(r.label) + " · " + cash(r.value) + "</title>";
      return bars
        ? '<rect x="' +
            (x(i) - w * 0.36) +
            '" y="' +
            Math.min(y(0), y(r.value)) +
            '" width="' +
            w * 0.72 +
            '" height="' +
            Math.max(1, Math.abs(y(0) - y(r.value))) +
            '" fill="' +
            (r.value >= 0 ? "#26836c" : "#bb5971") +
            '">' +
            label +
            "</rect>"
        : '<circle cx="' +
            x(i) +
            '" cy="' +
            y(r.value) +
            '" r="3" fill="#427dc4">' +
            label +
            "</circle>";
    })
    .join("");
  return (
    '<svg viewBox="0 0 600 120" role="img" aria-label="' +
    (bars ? "Daily simulated profit and loss" : "Historical virtual equity") +
    '">' +
    line +
    marks +
    '<g font-size="9" fill="currentColor"><text x="540" y="15">' +
    esc(cash(high)) +
    '</text><text x="540" y="107">' +
    esc(cash(low)) +
    "</text></g></svg>"
  );
}
function sectorChart(v) {
  if (!v.positions.length || !v.equity)
    return box("Sector exposure", "No marked positions.");
  const groups = {};
  v.positions.forEach((r) => {
    const k = security[r.symbol]?.sector || "Unclassified";
    groups[k] = (groups[k] || 0) + Number(r.value);
  });
  return box(
    "Sector exposure",
    Object.entries(groups)
      .map(
        ([k, n]) =>
          "<label>" +
          esc(k) +
          " · " +
          ((100 * n) / v.equity).toFixed(2) +
          '% <progress max="100" value="' +
          (100 * n) / v.equity +
          '"></progress></label>',
      )
      .join(""),
  );
}
function alloc(v) {
  const items = [
    { label: "Cash", value: Number(v.portfolio.cash) },
    ...v.positions.map((r) => ({ label: r.symbol, value: Number(r.value) })),
  ];
  if (v.equity == null || !Number(v.equity))
    return box("Portfolio allocation", "N/A");
  const colors = ["#4485ca", "#42aa95", "#ba8951", "#9b81c5", "#d5758e"];
  let cumulative = 0;
  const rings = items.map((r, i) => {
    const pct = (100 * r.value) / Number(v.equity);
    const start = cumulative;
    cumulative += pct;
    return (
      '<circle cx="60" cy="60" r="44" fill="none" stroke="' +
      colors[i % 5] +
      '" stroke-width="15" pathLength="100" stroke-dasharray="' +
      pct +
      " " +
      (100 - pct) +
      '" stroke-dashoffset="' +
      -start +
      '"><title>' +
      esc(r.label) +
      " " +
      pct.toFixed(1) +
      "%</title></circle>"
    );
  });
  return box(
    "Portfolio allocation",
    '<svg style="max-width:150px" viewBox="0 0 120 120" role="img" aria-label="Allocation by market value">' +
      rings.join("") +
      "</svg>" +
      items
        .map(
          (r, i) =>
            '<span class="swatch" style="background:' +
            colors[i % 5] +
            '"></span>' +
            esc(r.label) +
            " " +
            ((100 * r.value) / v.equity).toFixed(1) +
            "% &nbsp;",
        )
        .join(""),
  );
}
function filtered(rs) {
  const matches = rs.filter((r) =>
    Object.values(r).some((x) =>
      String(x).toLowerCase().includes(filter.toLowerCase()),
    ),
  );
  return sort
    ? matches.sort(
        (a, b) =>
          String(a[sort] ?? "").localeCompare(
            String(b[sort] ?? ""),
            undefined,
            { numeric: true },
          ) * (ascending ? 1 : -1),
      )
    : matches;
}
function params() {
  const q = new URLSearchParams({
    limit: "100",
    offset: String(offset),
    query: filter,
    sort,
    direction: sort && ascending ? "asc" : "desc",
  });
  if (startDate) q.set("start", Date.parse(startDate) / 1000);
  if (endDate) q.set("end", Date.parse(endDate) / 1000 + 86400);
  return q.toString();
}
function filters() {
  return (
    '<form id="filter" class="pactions">' +
    input("Search records", "query", "search", filter) +
    (active === names[3]
      ? ""
      : input("From (UTC)", "start", "date", startDate) +
        input("Through (UTC)", "end", "date", endDate)) +
    '<label>Sort by<select name="sort"><option value="">Newest first</option>' +
    ["symbol", "status", "created", "qty"]
      .map(
        (k) =>
          "<option " + (k === sort ? "selected" : "") + ">" + k + "</option>",
      )
      .join("") +
    '</select></label><label>Direction<select name="direction"><option value="asc" ' +
    (ascending ? "selected" : "") +
    '>Ascending</option><option value="desc" ' +
    (!ascending ? "selected" : "") +
    ">Descending</option></select></label><button>Apply</button></form>"
  );
}
function pager(rs) {
  return (
    '<div class="pactions">' +
    (offset ? button("Previous", "prev") : "") +
    (rs.length === 100 ? button("Next", "next") : "") +
    (policy.export
      ? '<a href="/paper/api/portfolios/' +
        pid +
        "/export?" +
        esc(params()) +
        "&amp;kind=" +
        (active === names[2]
          ? "signals"
          : active === names[6]
            ? "journal"
            : "transactions") +
        '">Export CSV</a><a href="/paper/api/portfolios/' +
        pid +
        '/report">Download PDF report</a>'
      : "") +
    "</div>"
  );
}
function signalTable(rs, actions = false) {
  const all = rs.map((r) => ({
    ...r,
    ...JSON.parse(r.ticket),
    ...JSON.parse(r.preview),
  }));
  return table(
    all,
    [
      ["symbol", "Symbol"],
      ["side", "Side"],
      ["method", "Sizing"],
      ["amount", "Requested"],
      ["qty", "Shares"],
      ["price", "Reference", cash],
      ["status", "Status"],
      ["notes", "Notes"],
      ["research", "Strategy Name"],
      ["created", "Created", date],
      ["processed", "Processed", date],
      ["reason", "Outcome"],
      ["id", "Signal ID"],
      ["origin", "Signal source", () => "Manual"],
      ["source", "Market data"],
    ],
    actions
      ? (r) =>
          button("Duplicate", "duplicate", r.id) +
          (["Draft", "Pending"].includes(r.status)
            ? " " +
              button("Edit", "edit", r.id) +
              (r.status === "Draft"
                ? ""
                : " " + button("Review", "review", r.id)) +
              " " +
              button("Cancel", "cancel", r.id)
            : "")
      : null,
  );
}
const sizingLabels = {
  shares: "Share quantity",
  dollars: "Dollar amount (USD)",
  percent: "Percentage (%)",
};
function signalForm() {
  const t = ticket || {};
  const percent = t.method?.endsWith("_pct");
  const unit = percent ? "percent" : t.method || "shares";
  return (
    '<form id="signal">' +
    input(
      "Symbol",
      "symbol",
      "text",
      selected,
      'required pattern="[A-Za-z][A-Za-z0-9.\\-]{0,9}"',
    ) +
    '<label>Direction<select name="side">' +
    ["Buy", "Sell"]
      .map(
        (s) =>
          "<option " + (s === t.side ? "selected" : "") + ">" + s + "</option>",
      )
      .join("") +
    '</select></label><fieldset class="sizing"><legend>Position sizing</legend><div>' +
    Object.entries({
      shares: "Quantity",
      dollars: "Dollars",
      percent: "Percent",
    })
      .map(
        ([key, label]) =>
          '<label><input type="radio" name="unit" value="' +
          key +
          '" ' +
          (key === unit ? "checked" : "") +
          ">" +
          label +
          "</label>",
      )
      .join("") +
    '</div></fieldset><label id="basis-label" ' +
    (percent ? "" : "hidden") +
    '>Percentage of<select name="basis">' +
    Object.entries({
      cash_pct: "Available cash",
      equity_pct: "Portfolio equity",
      position_pct: "Existing position",
    })
      .map(
        ([key, label]) =>
          '<option value="' +
          key +
          '" ' +
          (key ===
          (percent ? t.method : t.side === "Sell" ? "position_pct" : "cash_pct")
            ? "selected"
            : "") +
          (key === "position_pct" && t.side !== "Sell" ? " disabled" : "") +
          ">" +
          label +
          "</option>",
      )
      .join("") +
    "</select></label>" +
    '<label><span id="amount-label">' +
    sizingLabels[unit] +
    '</span><input name="amount" type="number" min=".000001" step="any" required value="' +
    esc(t.amount || "") +
    '"></label>' +
    '<div id="pct-shortcuts" ' +
    (percent ? "" : "hidden") +
    ">" +
    [25, 50, 75, 100].map((n) => button(n + "%", "percent", n)).join(" ") +
    "</div>" +
    '<fieldset id="sell-conditions" class="exit-fields" ' +
    (t.side === "Sell" ? "disabled hidden" : "") +
    "><legend>Sell conditions</legend>" +
    input(
      "Gain from buy price (%)",
      "gain_pct",
      "number",
      t.exit?.gain_pct || "",
      'min="0.000001" step="any" placeholder="Optional"',
    ) +
    input(
      "Stop price (USD)",
      "stop_price",
      "number",
      t.exit?.stop_price || "",
      'min="0.000001" step="any" placeholder="Optional"',
    ) +
    input(
      "Sell limit price (USD)",
      "limit_price",
      "number",
      t.exit?.limit_price || "",
      'min="0.000001" step="any" placeholder="Optional"',
    ) +
    "<small>Confirming the buy also authorizes a simulated sale of its shares. A stop activates at or below the stop price and fills only at or above the limit.</small></fieldset>" +
    input("Strategy Name", "research", "text", t.research || research) +
    input("Signal notes", "notes", "text", t.notes || "") +
    '<button class="primary">Preview signal</button><button type="submit" value="draft">Save draft</button></form>'
  );
}
function review(r) {
  const t = JSON.parse(r.ticket),
    p = JSON.parse(r.preview);
  return box(
    "Review simulated " + t.side.toLowerCase() + " · " + t.symbol,
    metrics([
      ["Requested", t.amount + " · " + t.method],
      ["Calculation basis", p.basis],
      ["Shares", p.qty],
      ["Price", cash(p.price)],
      ["Notional", cash(p.notional)],
      ["Fee", cash(p.fee)],
      ["Available cash", cash(p.cash_before)],
      ["Cash after", cash(p.cash_after)],
      ["Shares before", p.held_before],
      ["Shares after", p.held_after],
      ["Allocation after", p.allocation + "%"],
      ["Reference time", date(p.stamp)],
      ...(p.exit?.target
        ? [["Sell at gain of " + t.exit.gain_pct + "%", p.exit.target + " USD"]]
        : []),
      ...(p.exit?.stop
        ? [
            ["Stop price", p.exit.stop + " USD"],
            ["Minimum sell price", p.exit.floor + " USD"],
          ]
        : []),
    ]) +
      "<p>" +
      esc(p.source) +
      " · " +
      esc(r.status) +
      "</p>" +
      (t.provenance?.run
        ? '<p><a href="/strategies/results/' +
          encodeURIComponent(t.provenance.run) +
          '">View linked research results</a> · ' +
          esc(t.provenance.strategy_sha256 || "Strategy version unavailable") +
          "</p>"
        : "") +
      button(
        p.exit
          ? "Confirm buy and authorize conditional sale"
          : "Confirm simulated transaction",
        "confirm",
        r.id,
      ) +
      " " +
      button("Cancel signal", "cancel", r.id),
  );
}
async function candles(gen) {
  const ticker = selected;
  try {
    const [bars, fills] = await Promise.all([
      api("bars?symbol=" + encodeURIComponent(selected)),
      resource("transactions?limit=500&symbol=" + encodeURIComponent(selected)),
    ]);
    if (gen !== version || ticker !== selected || !$("candles")) return;
    if (!bars.length) {
      $("candles").textContent =
        "No bars available in this account’s history window.";
      return;
    }
    const data = bars.slice(-barCount),
      lo = Math.min(...data.map((b) => b.l)),
      hi = Math.max(...data.map((b) => b.h));
    const y = (p) => 15 + (115 * (hi - p)) / (hi - lo || 1),
      vol = Math.max(1, ...data.map((b) => b.v)),
      w = 500 / data.length;
    const marks = data.map((b, i) => {
      const x = 10 + (i + 0.5) * w,
        c = b.c >= b.o ? "#20896b" : "#d05a74";
      return (
        "<g><title>" +
        esc(date(b.stamp)) +
        " O " +
        b.o +
        " H " +
        b.h +
        " L " +
        b.l +
        " C " +
        b.c +
        " V " +
        b.v +
        '</title><path stroke="' +
        c +
        '" d="M' +
        x +
        " " +
        y(b.h) +
        "V" +
        y(b.l) +
        '"/><rect fill="' +
        c +
        '" x="' +
        (x - w * 0.3) +
        '" y="' +
        Math.min(y(b.o), y(b.c)) +
        '" width="' +
        w * 0.6 +
        '" height="' +
        Math.max(1, Math.abs(y(b.o) - y(b.c))) +
        '"/><rect fill="' +
        c +
        '" opacity=".45" x="' +
        (x - w * 0.3) +
        '" y="' +
        (175 - (30 * b.v) / vol) +
        '" width="' +
        w * 0.6 +
        '" height="' +
        (30 * b.v) / vol +
        '"/></g>'
      );
    });
    const holding = summary.positions.find((p) => p.symbol === selected);
    const run = $("signal")?.elements.namedItem("research")?.value;
    if (run) {
      try {
        const saved = await api(
          "research?run=" +
            encodeURIComponent(run) +
            "&symbol=" +
            encodeURIComponent(ticker),
        );
        if (gen !== version || ticker !== selected || !$("linkedResearch"))
          return;
        $("linkedResearch").innerHTML = box(
          "Saved strategy research",
          '<a href="/strategies/results/' +
            encodeURIComponent(run) +
            '">' +
            esc(saved.provenance.strategy || run) +
            "</a><p>" +
            esc(saved.provenance.mode || "Historical research") +
            " · " +
            esc(saved.provenance.started_utc || "Time unavailable") +
            "</p>" +
            table(
              Object.entries(saved.result).map(([field, value]) => ({
                field,
                value,
              })),
              [
                ["field", "Saved field"],
                ["value", "At research time"],
              ],
            ) +
            "<h3>Subsequent simulated fills</h3>" +
            table(
              fills.filter((f) => JSON.parse(f.ticket).research === run),
              fillCols.slice(0, 7),
            ),
        );
      } catch (err) {
        if (gen === version && $("linkedResearch"))
          $("linkedResearch").textContent = err.message;
      }
    } else if ($("linkedResearch")) $("linkedResearch").textContent = "";

    const ma = data
      .map((_, i) => {
        const end = bars.length - data.length + i + 1;
        if (end < 20) return "";
        const average =
          bars.slice(end - 20, end).reduce((total, bar) => total + bar.c, 0) /
          20;
        return 10 + (i + 0.5) * w + "," + y(average);
      })
      .filter(Boolean);
    const basis =
      holding && Number(holding.average) >= lo && Number(holding.average) <= hi
        ? '<path stroke="#b78943" stroke-dasharray="4 3" d="M10 ' +
          y(Number(holding.average)) +
          'h500"><title>Average entry</title></path>'
        : "";
    $("candles").closest("section").querySelector("h2").textContent =
      ticker + " · Price history";
    const extras = fills
      .filter((r) => r.stamp >= data[0].stamp && r.stamp <= data.at(-1).stamp)
      .map((r) => {
        const i = data.findIndex((b) => b.stamp >= r.stamp);
        return (
          '<text x="' +
          (10 + (i + 0.5) * w) +
          '" y="' +
          y(Number(r.price)) +
          '" fill="' +
          (r.side === "Buy" ? "#158465" : "#bd465c") +
          '"><title>' +
          esc(r.side) +
          " " +
          esc(r.qty) +
          "</title>" +
          (r.side === "Buy" ? "▲" : "▼") +
          "</text>"
        );
      })
      .join("");
    $("candles").innerHTML =
      [30, 60, 240, 500].map((n) => button(n + " bars", "bars", n)).join(" ") +
      '<svg viewBox="0 0 525 185" role="img" aria-label="Minute candlesticks and volume">' +
      '<g fill="currentColor" font-size="9"><text x="2" y="10">' +
      esc(cash(hi)) +
      '</text><text x="2" y="142">' +
      esc(cash(lo)) +
      "</text></g>" +
      marks.join("") +
      basis +
      '<polyline fill="none" stroke="#869edd" points="' +
      ma.join(" ") +
      '"><title>20-bar simple moving average</title></polyline>' +
      extras +
      "</svg>" +
      (holding
        ? "<p>Held " +
          esc(holding.qty) +
          " · Average entry " +
          cash(holding.average) +
          "</p>"
        : "") +
      "<p>" +
      cash(data.at(-1).c) +
      " · " +
      esc(date(data.at(-1).stamp)) +
      "</p><small>Completed Finviz minute bars · " +
      (Date.now() / 1000 - data.at(-1).stamp > 120 ? "Historical" : "Recent") +
      "</small>";
  } catch (e) {
    if (gen === version && $("candles")) $("candles").textContent = e.message;
  }
}
let series = [],
  range = "ALL";
function chartBox(st) {
  series = st.series;
  return box(
    "Portfolio performance",
    '<div class="pactions">' +
      ["1D", "1W", "1M", "3M", "6M", "YTD", "1Y", "ALL"]
        .map((r) => button(r, "range", r))
        .join("") +
      '</div><div class="pactions">' +
      input("From", "from", "date") +
      input("Through", "through", "date") +
      '</div><div id="chart"></div>',
  );
}
function renderEquity() {
  const days = { "1D": 1, "1W": 7, "1M": 30, "3M": 90, "6M": 182, "1Y": 365 };
  const end = Date.now() / 1000,
    start =
      range === "YTD"
        ? new Date(new Date().getFullYear(), 0, 1) / 1000
        : days[range]
          ? end - days[range] * 86400
          : 0;
  const a = document.querySelector("[name=from]")?.valueAsNumber / 1000,
    b = document.querySelector("[name=through]")?.valueAsNumber / 1000;
  const rs = series.filter(
    (r) =>
      r.created >= Math.max(start, a || 0) &&
      r.created < (b ? b + 86400 : Infinity),
  );
  if (rs.length < 2) {
    $("chart").textContent =
      "At least two valuation snapshots are needed for this range.";
    return;
  }
  const keys = ["return_pct", "benchmark_pct", "drawdown"],
    vals = rs.flatMap((r) => keys.map((k) => r[k]).filter((n) => n != null));
  const lo = Math.min(0, ...vals),
    hi = Math.max(0, ...vals),
    x = (i) =>
      15 +
      (570 * (rs[i].created - rs[0].created)) /
        (rs.at(-1).created - rs[0].created || 1),
    y = (v) => 12 + (130 * (hi - v)) / (hi - lo || 1);
  $("chart").innerHTML =
    '<svg viewBox="0 0 600 160" role="img" aria-label="Portfolio, benchmark and drawdown percentages">' +
    keys
      .map((k, j) => {
        let line = "";
        let first = true;
        rs.forEach((r, i) => {
          if (r[k] == null) {
            first = true;
            return;
          }
          line += (first ? "M" : "L") + x(i) + " " + y(r[k]);
          first = false;
        });
        return (
          '<path fill="none" stroke="' +
          ["#4287df", "#29a588", "#cc6882"][j] +
          '" stroke-width="2" d="' +
          line +
          '"/>'
        );
      })
      .join("") +
    rs
      .map(
        (r, i) =>
          '<circle cx="' +
          x(i) +
          '" cy="' +
          y(r.return_pct) +
          '" r="4" fill="#4287df"><title>' +
          esc(date(r.created)) +
          " · " +
          cash(r.equity) +
          " · " +
          r.return_pct +
          "%</title></circle>",
      )
      .join("") +
    "</svg><small>Blue · portfolio return　 Green · benchmark　 Rose · drawdown</small>";
}
$("portfolio").onchange = () => {
  pid = $("portfolio").value;
  go(active);
};
$("newSignal").onclick = () => go(names[1]);
document.addEventListener("input", (e) => {
  if (e.target.closest("#signal") && e.target.name === "symbol")
    selected = e.target.value.trim().toUpperCase();
  if (e.target.name === "watchSearch")
    document
      .querySelectorAll(".watch button")
      .forEach(
        (b) =>
          (b.hidden = !b.textContent.includes(e.target.value.toUpperCase())),
      );
  if (["from", "through"].includes(e.target.name)) renderEquity();
  if (e.target.closest("#signal") && $("preview")) $("preview").innerHTML = "";
});
document.addEventListener("change", (e) => {
  const f = e.target.closest("#signal");
  if (f && ["side", "unit"].includes(e.target.name)) {
    $("sell-conditions").hidden = $("sell-conditions").disabled =
      f.side.value === "Sell";
    const percent = f.elements.unit.value === "percent";
    const basis = f.elements.basis;
    basis.querySelector('[value="position_pct"]').disabled =
      f.side.value !== "Sell";
    if (f.side.value !== "Sell" && basis.value === "position_pct")
      basis.value = "cash_pct";
    $("basis-label").hidden = !percent;
    $("pct-shortcuts").hidden = !percent;
    $("amount-label").textContent = sizingLabels[f.elements.unit.value];
    if (e.target.name === "unit") f.amount.value = "";
  }
  if (
    ["symbol", "research"].includes(e.target.name) &&
    e.target.closest("#signal") &&
    selected
  )
    candles(version);
});
document.addEventListener("click", async (e) => {
  const el = e.target.closest("button");
  if (!el) return;
  if (el.dataset.tab) {
    go(el.dataset.tab);
    return;
  }
  const a = el.dataset.action,
    id = el.dataset.id;
  if (!a) return;
  try {
    msg("");
    el.disabled = true;
    if (a === "cancel-exit") {
      await resource("exits/" + id + "/cancel", {});
      await draw();
      msg("Conditional sale canceled.");
    }
    if (a === "ticker") {
      selected = id;
      await draw();
    }
    if (a === "percent") {
      const f = $("signal");
      f.amount.value = id;
      $("preview").innerHTML = "";
    }
    if (a === "confirm" || a === "cancel") {
      const r = await resource("signals/" + id + "/" + a, {});
      msg(r.status + (r.reason ? " · " + r.reason : ""));
      await draw();
    }
    if (a === "review" || a === "duplicate" || a === "edit") {
      const all = await resource("signals?" + params()),
        r = all.find((x) => x.id === id);
      if (!r) throw Error("Refresh this page.");
      if (a === "review") {
        $("content").innerHTML = review(r);
      } else {
        ticket = JSON.parse(r.ticket);
        if (a === "edit") ticket.replaces = id;
        selected = ticket.symbol;
        active = names[1];
        await draw();
      }
    }
    if (a === "prev" || a === "next") {
      offset = Math.max(0, offset + (a === "next" ? 100 : -100));
      await draw();
    }
    if (a === "position") {
      active = names[4];
      filter = id;
      offset = 0;
      await draw();
    }
    if (a === "range") {
      range = id;
      renderEquity();
    }
    if (a === "bars") {
      barCount = Number(id);
      await candles(version);
    }
    if (a === "ai" || a === "historical") {
      const r = await resource(a === "ai" ? "explanation" : "historical", {});
      $("researchOutput").textContent = Object.entries(r)
        .map(([k, v]) => k + " · " + v)
        .join(" | ");
    }
    if (a === "archive" || a === "reset") {
      const name = prompt("Type the portfolio name to " + a + " its ledger.");
      if (name != null) {
        const p = await resource("settings", { action: a, confirm_name: name });
        await init(p.id);
      }
    }
    if (a === "compare") {
      const vs = await Promise.all(
        portfolios.map((p) => api("portfolios/" + p.id + "/summary")),
      );
      const historyRows = await Promise.all(
        portfolios.map(async (p) => {
          const a = await api("portfolios/" + p.id + "/analytics");
          return a.monthly.map((r) => ({ name: p.name, ...r }));
        }),
      );
      $("comparison").innerHTML =
        table(
          vs.map((v) => ({ ...v, name: v.portfolio.name })),
          [
            ["name", "Portfolio"],
            ["equity", "Value", cash],
            ["return_pct", "Return %"],
          ],
        ) +
        table(historyRows.flat(), [
          ["name", "Portfolio"],
          ["month", "Month"],
          ["pct", "Historical return %"],
        ]);
    }
  } catch (err) {
    msg(err.message, true);
  } finally {
    el.disabled = false;
  }
});
document.addEventListener("submit", async (e) => {
  const f = e.target;
  if (!f.closest(".paper")) return;
  e.preventDefault();
  const data = Object.fromEntries(new FormData(f)),
    submit = f.querySelector("button:not([type=button])");
  try {
    submit.disabled = true;
    msg("");
    if (f.id === "create" || f.id === "settings") {
      data.fractional = f.fractional.checked;
      const p =
        f.id === "create"
          ? await api("portfolios", data)
          : await resource("settings", data);
      active = names[0];
      await init(p.id);
      msg("Portfolio saved.");
    }
    if (f.id === "signal") {
      data.method = data.unit === "percent" ? data.basis : data.unit;
      delete data.unit;
      delete data.basis;
      if (data.method.endsWith("_pct") && Number(data.amount) > 100)
        throw Error("Percentage must be at most 100.");
      if (data.method === "position_pct" && data.side !== "Sell")
        throw Error("Position percentage requires a sell signal.");
      const signature = JSON.stringify(data);
      if (f.dataset.signature !== signature) {
        f.dataset.key = crypto.randomUUID();
        f.dataset.signature = signature;
      }
      if (e.submitter?.value === "draft") {
        await resource("draft", {
          ...data,
          request_key: f.dataset.key + "-draft",
        });
        go(names[2]);
        msg("Draft saved.");
        return;
      }
      const r = await resource("preview", {
        ...data,
        replaces: ticket?.replaces,
        request_key: f.dataset.key,
      });
      $("preview").innerHTML = review(r);
      $("preview").scrollIntoView({ block: "nearest" });
    }
    if (f.id === "filter") {
      filter = data.query;
      sort = data.sort;
      ascending = data.direction === "asc";
      startDate = data.start || "";
      endDate = data.end || "";
      offset = 0;
      await draw();
    }
    if (f.id === "action") {
      await resource("action", {
        ...data,
        request_key: f.dataset.key || (f.dataset.key = crypto.randomUUID()),
      });
      await draw();
      msg("Corporate action recorded.");
    }
    if (f.id === "journal") {
      await resource("journal", data);
      await draw();
      msg("Journal entry saved.");
    }
  } catch (err) {
    msg(err.message, true);
  } finally {
    submit.disabled = false;
  }
});
init().catch((e) => msg(e.message, true));
