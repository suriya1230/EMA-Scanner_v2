"use client";

import { useState, useEffect, useCallback, useRef, useMemo, memo } from "react";
import { TrendingUp, TrendingDown, Target, Database, CheckCircle2, XCircle, DollarSign, Award, CalendarClock } from "lucide-react";

// ─── Config ───────────────────────────────────────────────────────────────────
const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000/api";

const MARKETS = ["futures", "spot"];

// ─── Formatting ───────────────────────────────────────────────────────────────
function fmtPrice(p, sym) {
  if (p == null) return "—";
  if (p >= 1000)  return p.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (p >= 1)     return p.toFixed(4);
  return p.toFixed(6);
}
function fmtVol(v) {
  if (v == null) return "—";
  if (v >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (v >= 1e3) return `$${(v / 1e3).toFixed(2)}K`;
  return `$${v.toFixed(2)}`;
}
function fmtDate(ms) {
  const d = new Date(ms);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}
function fmtDateTime(ms) {
  const d = new Date(ms);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }) +
    ", " + d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: true }) + " IST";
}
function fmtTime(dt) {
  if (!dt) return "—";
  const d = new Date(dt);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }) +
    ", " + d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: true }) + " IST";
}
function fmtSym(sym) {
  return { base: sym.replace("USDT",""), quote: "/USDT" };
}
// Time-only (no date) — for spots like the Swing Strategy State cell where
// the date is already shown elsewhere on the row and just adds clutter.
function fmtTimeOnly(dt) {
  if (!dt) return "—";
  const d = new Date(dt);
  return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: true }) + " IST";
}
// Elapsed wall-clock time since a zone's detected_at — the backend's own
// zone_age counts whole 1d candles, which rounds anything confirmed within
// the last ~24h down to "0d" and hides how fresh it actually is.
function fmtZoneAge(detectedAtMs) {
  if (!detectedAtMs) return "—";
  const totalMinutes = Math.max(0, Math.floor((Date.now() - detectedAtMs) / 60000));
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}
// Fixed-duration formatter (given an explicit ms span, not "elapsed from
// now") — used for the Swing Backtest's entry->exit trade duration.
function fmtDuration(ms) {
  if (ms == null) return "—";
  const totalMinutes = Math.max(0, Math.floor(ms / 60000));
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `${days}d ${hours}h`;
  return `${hours}h ${minutes}m`;
}

// ─── Shared components ───────────────────────────────────────────────────────
const TH = ({ children, right, onClick, sorted, dir }) => (
  <th onClick={onClick} style={{
    padding: "10px 14px", fontSize: 11, fontWeight: 700, letterSpacing: "0.07em",
    textTransform: "uppercase", color: "#9ca3af", textAlign: right ? "right" : "left",
    whiteSpace: "nowrap", borderBottom: "1px solid #e8eaed", background: "#f8f9fb",
    cursor: onClick ? "pointer" : "default", userSelect: "none",
  }}>
    {children}{sorted ? <span style={{ marginLeft: 3, opacity: 0.6 }}>{dir === "asc" ? "↑" : "↓"}</span> : null}
  </th>
);

const Trend = ({ t }) => {
  const bull = t === "Bullish", neu = t === "Neutral";
  return <span style={{ display:"flex", alignItems:"center", gap:4 }}>
    {!neu && <span style={{ fontSize:10, color: bull?"#22c55e":"#ef4444" }}>{bull?"▲":"▼"}</span>}
    <span style={{ fontWeight:600, fontSize:13, color: neu?"#9ca3af":bull?"#16a34a":"#dc2626" }}>{t}</span>
  </span>;
};

const SigBadge = ({ s }) => {
  if (!s) return <span style={{ color:"#d1d5db" }}>—</span>;
  return <span style={{
    display:"inline-block", padding:"3px 10px", borderRadius:5, fontSize:11,
    fontWeight:700, letterSpacing:"0.04em",
    background: s==="BUY"?"#16a34a":"#dc2626", color:"#fff"
  }}>{s}</span>;
};

const ScoreBadge = ({ v }) => {
  const s = v ?? 0;
  const color = s >= 70 ? "#16a34a" : s >= 40 ? "#f59e0b" : "#dc2626";
  const bg    = s >= 70 ? "#e7f8ef" : s >= 40 ? "#fef3e2" : "#fdecec";
  return (
    <span style={{
      display:"inline-block", minWidth:34, textAlign:"center", padding:"3px 10px",
      borderRadius:6, fontSize:13, fontWeight:700, color, background:bg, border:`1px solid ${color}33`,
    }}>{s.toFixed(0)}</span>
  );
};

const SummaryCard = ({ label, badge, badgeBg, badgeColor, value, valueColor, bg, Icon }) => (
  <div style={{
    background:bg, borderRadius:16, padding:"18px 20px", border:"3px solid #fff",
    boxShadow:"0 1px 4px rgba(0,0,0,0.05)", position:"relative", overflow:"hidden", minHeight:96,
  }}>
    <div style={{ display:"flex", alignItems:"center", gap:8, flexWrap:"wrap" }}>
      <span style={{ fontSize:11, fontWeight:800, letterSpacing:"0.06em", color:"#374151", textTransform:"uppercase" }}>{label}</span>
      <span style={{
        fontSize:10, fontWeight:700, padding:"2px 9px", borderRadius:999,
        background:badgeBg, color:badgeColor, whiteSpace:"nowrap",
      }}>{badge}</span>
    </div>
    <div style={{ fontSize:30, fontWeight:800, color:valueColor, marginTop:8 }}>{value}</div>
    <Icon size={30} strokeWidth={2} style={{ position:"absolute", right:16, bottom:14, opacity:0.5, color:valueColor }}/>
  </div>
);

// Free-form Risk:Reward entry — sits alongside the 1:2 / 2:4 presets so any
// ratio can be backtested, not just the two built-in ones. Uncontrolled
// inputs keyed on `value` so clicking a preset button resets them to match,
// without fighting the parent's controlled rrMode state on every keystroke.
const RRCustomInput = ({ value, onChange, disabled }) => {
  // value can also be the "swing" sentinel (no colon) when Swing SL/TP mode
  // is active — fall back to a default pair so these inputs stay controlled
  // (defined) instead of flipping to undefined and tripping React's warning.
  const [riskDefault, rewardDefault] = value === "swing" ? ["1", "2"] : value.split(":");
  const [risk, setRisk] = useState(riskDefault);
  const [reward, setReward] = useState(rewardDefault);

  // Reset the fields to match whenever a preset button changes `value`
  // externally — otherwise a stale typed value would linger after a preset click.
  useEffect(() => { setRisk(riskDefault); setReward(rewardDefault); }, [riskDefault, rewardDefault]);

  const apply = () => {
    const r = parseFloat(risk), w = parseFloat(reward);
    if (r > 0 && w > 0) onChange(`${r}:${w}`);
  };

  // Same look as the 1:2 / 2:4 preset buttons — plain bordered box, not a
  // standout colored pill, so the custom entry reads as part of the same set.
  const inputStyle = {
    width:44, padding:"5px 6px", borderRadius:7, border:"1px solid #e5e7eb",
    fontSize:12, fontWeight:600, textAlign:"center", color:"#6b7280",
    outline:"none", background:"#fff",
  };

  return (
    <div style={{ marginLeft:"auto", display:"flex", alignItems:"center", gap:6 }}>
      <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase", whiteSpace:"nowrap" }}>
        Customize Risk &amp; Reward
      </span>
      <input
        type="number" step="0.5" min="0.1" disabled={disabled} value={risk} title="Risk %"
        onChange={e => setRisk(e.target.value)}
        onKeyDown={e => { if (e.key === "Enter") apply(); }}
        style={inputStyle}
      />
      <span style={{ color:"#9ca3af", fontSize:12 }}>:</span>
      <input
        type="number" step="0.5" min="0.1" disabled={disabled} value={reward} title="Reward %"
        onChange={e => setReward(e.target.value)}
        onKeyDown={e => { if (e.key === "Enter") apply(); }}
        style={inputStyle}
      />
      <button onClick={apply} disabled={disabled} style={{
        padding:"5px 12px", borderRadius:7, fontSize:12, fontWeight:700,
        cursor: disabled ? "not-allowed" : "pointer",
        border:"1px solid #e5e7eb", background:"#fff", color:"#6b7280",
      }}>Apply</button>
    </div>
  );
};

const ChgCell = ({ v }) => (
  <span style={{ color: v>=0?"#16a34a":"#dc2626", fontWeight:500 }}>
    {v>=0?"+":""}{v.toFixed(2)}%
  </span>
);

const MarketToggle = ({ market, setMarket }) => (
  <div style={{ display:"flex", gap:4 }}>
    {MARKETS.map(m => (
      <button key={m} onClick={()=>setMarket(m)} style={{
        padding:"4px 12px", borderRadius:6, fontSize:12, fontWeight:600, cursor:"pointer",
        border:market===m?"1.5px solid #16a34a":"1px solid #e5e7eb", background:"transparent",
        color:market===m?"#16a34a":"#6b7280", textTransform:"capitalize",
      }}>{m}</button>
    ))}
  </div>
);

// Most recent confirmed swing low/high strictly before `uptoIdx` (the last
// fully-closed candle at entry time) — a pivot at index i only counts once
// `strength` candles exist on BOTH sides with a higher low (or lower high),
// and all of those confirming candles must also be <= uptoIdx so this never
// looks at data that wouldn't exist yet at the moment of entry.
function findRecentSwingLow(candles, uptoIdx, strength = 2, maxLookback = 100) {
  const minIdx = Math.max(strength, uptoIdx - maxLookback);
  for (let i = uptoIdx - strength; i >= minIdx; i--) {
    let isPivot = true;
    for (let k = 1; k <= strength; k++) {
      if (candles[i].low >= candles[i - k].low || candles[i].low >= candles[i + k].low) { isPivot = false; break; }
    }
    if (isPivot) return candles[i].low;
  }
  return null;
}
function findRecentSwingHigh(candles, uptoIdx, strength = 2, maxLookback = 100) {
  const minIdx = Math.max(strength, uptoIdx - maxLookback);
  for (let i = uptoIdx - strength; i >= minIdx; i--) {
    let isPivot = true;
    for (let k = 1; k <= strength; k++) {
      if (candles[i].high <= candles[i - k].high || candles[i].high <= candles[i + k].high) { isPivot = false; break; }
    }
    if (isPivot) return candles[i].high;
  }
  return null;
}

// ─── Backtest using DB signals (exact same signals as scanner page) ───────────
// Instead of recalculating EMA crossovers on the frontend (which can differ
// from the backend due to logic version mismatches), we fetch the signals
// directly from the DB and use them as-is. Only the SL/TP simulation runs
// on the frontend using the candle OHLC data.
function runBacktestFromSignals(candles, dbSignals, rrMode, windowDays, candleMs = 3_600_000, timeframe = '1h', capital = 1000) {
  if (!candles || candles.length === 0 || !dbSignals) return [];

  // rrMode is any "risk:reward" string, e.g. "1:2", "2:4", a custom
  // user-entered ratio like "1.5:3", or the sentinel "swing" — meaning SL is
  // derived from market structure (nearest swing low/high) instead of a
  // fixed percentage, with TP always set to double that risk distance.
  const isSwing = rrMode === "swing";
  const [riskPct, rewardPct] = isSwing ? [null, null] : rrMode.split(":").map(Number);
  const SWING_BUFFER = 0.001; // 0.1% beyond the swing point, so SL sits "just" past it rather than exactly on it
  const SWING_FALLBACK_PCT = 1; // used only if no swing point is found in the lookback window

  // Window cutoff — relative to last candle
  const windowMs     = windowDays * 24 * 3_600_000;
  const lastCandleMs = candles[candles.length - 1].openTimeMs;
  const cutoffMs     = lastCandleMs - windowMs;

  // Filter signals to window
  const windowSignals = dbSignals.filter(s => s.crossTimeMs >= cutoffMs);

  if (windowSignals.length === 0) return [];

  const symbol   = candles[0]?.symbol || "";
  const symLabel = symbol.replace("USDT", "") + "/USDT";

  function interpolateHitTime(candleOpenMs, open, high, low, level, hitType) {
    let t;
    if (hitType === "high") t = high !== open ? (level - open) / (high - open) : 0.5;
    else                    t = open !== low  ? (open - level) / (open - low)  : 0.5;
    t = Math.max(0, Math.min(1, t));
    return candleOpenMs + t * candleMs;
  }

  function simulateTradeFromIdx(type, entryPrice, startCandleIdx, sigCandleIdx) {
    let stopLoss, targetPrice;
    if (isSwing) {
      if (type === "BUY") {
        const swingLow = findRecentSwingLow(candles, sigCandleIdx);
        stopLoss = swingLow != null ? swingLow * (1 - SWING_BUFFER) : entryPrice * (1 - SWING_FALLBACK_PCT / 100);
        const risk = entryPrice - stopLoss;
        targetPrice = entryPrice + risk; // TP = same distance as SL (1:1)
      } else {
        const swingHigh = findRecentSwingHigh(candles, sigCandleIdx);
        stopLoss = swingHigh != null ? swingHigh * (1 + SWING_BUFFER) : entryPrice * (1 + SWING_FALLBACK_PCT / 100);
        const risk = stopLoss - entryPrice;
        targetPrice = entryPrice - risk; // TP = same distance as SL (1:1)
      }
    } else if (type === "BUY") {
      stopLoss    = entryPrice * (1 - riskPct  / 100);
      targetPrice = entryPrice * (1 + rewardPct / 100);
    } else {
      stopLoss    = entryPrice * (1 + riskPct  / 100);
      targetPrice = entryPrice * (1 - rewardPct / 100);
    }
    let exitPrice = null, exitTimeMs = null, exitReason = null;
    for (let j = startCandleIdx; j < candles.length; j++) {
      const c = candles[j];
      if (type === "BUY") {
        if (c.low  <= stopLoss)    { exitPrice = stopLoss;    exitTimeMs = interpolateHitTime(c.openTimeMs, c.open, c.high, c.low, stopLoss,    "low");  exitReason = "Stop Loss Hit"; break; }
        if (c.high >= targetPrice) { exitPrice = targetPrice; exitTimeMs = interpolateHitTime(c.openTimeMs, c.open, c.high, c.low, targetPrice, "high"); exitReason = "Target Hit";   break; }
      } else {
        if (c.high >= stopLoss)    { exitPrice = stopLoss;    exitTimeMs = interpolateHitTime(c.openTimeMs, c.open, c.high, c.low, stopLoss,    "high"); exitReason = "Stop Loss Hit"; break; }
        if (c.low  <= targetPrice) { exitPrice = targetPrice; exitTimeMs = interpolateHitTime(c.openTimeMs, c.open, c.high, c.low, targetPrice, "low");  exitReason = "Target Hit";   break; }
      }
    }
    return { stopLoss, targetPrice, exitPrice, exitTimeMs, exitReason };
  }

  const trades    = [];
  let openTradeRef = null;

  for (const sig of windowSignals) {
    const { type, crossPrice, crossTimeMs } = sig;

    // Find the signal candle by matching openTimeMs to cross_time (rounded to candle open)
    // cross_time from DB is the EMA cross time — find the candle that contains it
    let sigCandleIdx = -1;
    for (let i = 0; i < candles.length; i++) {
      if (candles[i].openTimeMs <= crossTimeMs && crossTimeMs < candles[i].openTimeMs + candleMs) {
        sigCandleIdx = i;
        break;
      }
    }
    // Fallback: use closest candle before crossTimeMs
    if (sigCandleIdx === -1) {
      for (let i = candles.length - 1; i >= 0; i--) {
        if (candles[i].openTimeMs <= crossTimeMs) { sigCandleIdx = i; break; }
      }
    }
    if (sigCandleIdx === -1) continue;  // signal candle not in fetched data

    // Entry = the NEXT candle's open price/time — the signal candle has
    // already closed by the time you could realistically act on it, so the
    // earliest honest fill is the following candle's open, not a price
    // inside (or the close of) the candle that already happened.
    // If that next candle doesn't exist yet (signal fired on the most
    // recent candle available), skip this signal entirely until it's
    // picked up on a later refresh once that candle exists.
    const entryCandleIdx = sigCandleIdx + 1;
    if (entryCandleIdx >= candles.length) continue;

    const entryTimeMs = candles[entryCandleIdx].openTimeMs;
    if (entryTimeMs > Date.now()) continue;

    const entryPrice  = candles[entryCandleIdx].open;
    const signalTime  = fmtDateTime(crossTimeMs);
    const entryTime   = fmtDateTime(entryTimeMs);

    // If previous trade is still open → force close at this entry price
    if (openTradeRef !== null) {
      const prev       = openTradeRef;
      const forceExit  = entryPrice;
      const forceTime  = entryTimeMs;
      const gainPct    = prev.tradeSignal === "BUY"
        ? ((forceExit - prev.entryPrice) / prev.entryPrice) * 100
        : ((prev.entryPrice - forceExit) / prev.entryPrice) * 100;
      const gainAmount = prev.tradeSignal === "BUY"
        ? forceExit - prev.entryPrice : prev.entryPrice - forceExit;
      const durationMs = forceTime - prev._entryTimeMs;
      const dh = Math.floor(durationMs / 3_600_000);
      const dm = Math.floor((durationMs % 3_600_000) / 60000);
      prev.entryClose     = forceExit;
      prev.entryCloseTime = fmtDateTime(forceTime);
      prev.exitReason     = "Closed by new signal";
      prev.duration       = dh >= 24 ? `${Math.floor(dh/24)}d ${dh%24}h` : `${dh}h ${dm}m`;
      prev.result         = gainPct >= 0 ? "WIN" : "LOSS";
      prev.gainPct        = gainPct;
      prev.gainAmount     = gainAmount;
      prev.gainDollar     = (capital * gainPct) / 100;
      prev._exitTimeMs    = forceTime;
      openTradeRef        = null;
    }

    // Simulate this trade — walk forward starting at the entry candle itself
    const sim = simulateTradeFromIdx(type, entryPrice, entryCandleIdx, sigCandleIdx);
    const { stopLoss, targetPrice, exitPrice, exitTimeMs, exitReason } = sim;

    if (!exitPrice) {
      const row = {
        date: fmtDate(entryTimeMs), timeFrame: timeframe.toUpperCase(), symbol: symLabel,
        tradeSignal: type, signalTime, entryTime, entryPrice, stopLoss, targetPrice,
        entryClose: null, entryCloseTime: null, exitReason: "Open", duration: "—",
        result: "OPEN", gainPct: null, gainAmount: null, gainDollar: null,
        _entryTimeMs: entryTimeMs, _exitTimeMs: null, _signalTimeMs: crossTimeMs,
      };
      trades.push(row);
      openTradeRef = row;
      continue;
    }

    const gainPct    = type === "BUY" ? ((exitPrice - entryPrice) / entryPrice) * 100 : ((entryPrice - exitPrice) / entryPrice) * 100;
    const gainAmount = type === "BUY" ? exitPrice - entryPrice : entryPrice - exitPrice;
    const gainDollar = (capital * gainPct) / 100;
    const result     = exitReason === "Target Hit" ? "WIN" : "LOSS";
    const durationMs = exitTimeMs - entryTimeMs;
    const dh         = Math.floor(durationMs / 3_600_000);
    const dm         = Math.floor((durationMs % 3_600_000) / 60000);
    const duration   = dh >= 24 ? `${Math.floor(dh/24)}d ${dh%24}h` : `${dh}h ${dm}m`;

    trades.push({
      date: fmtDate(entryTimeMs), timeFrame: timeframe.toUpperCase(), symbol: symLabel,
      tradeSignal: type, signalTime, entryTime, entryPrice, stopLoss, targetPrice,
      entryClose: exitPrice, entryCloseTime: fmtDateTime(exitTimeMs), exitReason,
      duration, result, gainPct, gainAmount, gainDollar,
      _entryTimeMs: entryTimeMs, _exitTimeMs: exitTimeMs, _signalTimeMs: crossTimeMs,
    });
    openTradeRef = null;
  }

  return trades;
}


// Toggle to false to hide the "Swing SL/TP" button from both backtest pages
// without removing the feature — flip back to true to bring it back.
const SHOW_SWING_BUTTON = false;

const SIGS  = ["All","BUY","SELL"];
const BT_PERIOD_DAYS = { day: 1, week: 7, month: 30 };
const SCOLS = [
  {k:"rank",     l:"#",            s:true},
  {k:"symbol",   l:"Symbol",       s:true},
  {k:"ema_trend",l:"EMA Trend",    s:true},
  {k:"score",    l:"Score",        s:true,  r:true},
  {k:"price",    l:"Price",        s:true,  r:true},
  {k:"change_1h",l:"1H %",         s:true,  r:true},
  {k:"change_24h",l:"24H %",       s:true,  r:true},
  {k:"volume_24h",l:"Volume (24H)",s:true,  r:true},
  {k:"last_signal",l:"Last Signal",s:true},
  {k:"signal_time",l:"Signal Time",s:true},
  {k:"details",  l:"Details",      s:false},
];

// Runs `worker` over `items` with at most `limit` in flight at once — a CSV
// can reference hundreds of distinct (symbol, interval) pairs, and firing
// that many fetches all at once (unbounded Promise.all) queues up behind the
// browser's per-host connection cap and makes page load look stuck.
async function runWithConcurrency(items, worker, limit) {
  const results = new Array(items.length);
  let next = 0;
  async function runOne() {
    while (next < items.length) {
      const idx = next++;
      results[idx] = await worker(items[idx], idx);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, runOne));
  return results;
}
const CSV_CANDLE_CONCURRENCY = 15;
// Rendering all rows as real DOM nodes (thousands of <tr>, each with hover
// handlers) is what made every click/scroll janky — paginating keeps the
// live DOM small regardless of how many rows match the current filters.
const CSV_PAGE_SIZE = 50;

function ScannerPageImpl({ onBacktest, onScreenerBacktest, onHome }) {
  const [modal, setModal] = useState(null);
  const [csvUploading, setCsvUploading] = useState(false);
  const [csvUploadError, setCsvUploadError] = useState(null);
  const csvInputRef = useRef(null);

  // CSV Backtest — all inline on this page, no separate page/navigation.
  // Displayed as a scanner-style table (same columns/components as the main
  // table above), not a trade-result table — each CSV row can be on its own
  // timeframe (5m/15m/30m/1h/...), shown as a small tag next to Signal Time.
  const [csvSummary, setCsvSummary] = useState(null);
  const [csvRows, setCsvRows] = useState([]);
  const [csvLoading, setCsvLoading] = useState(false);
  const [csvError, setCsvError] = useState(null);
  const [csvSort, setCsvSort] = useState({ k:"signal_time", dir:"desc" });
  const [csvSearch, setCsvSearch] = useState("");
  const [csvTrendFilter, setCsvTrendFilter] = useState("All");
  const [csvSigFilter, setCsvSigFilter] = useState("All");
  const [csvPage, setCsvPage] = useState(1);
  const csvFetchIdRef = useRef(0);

  const loadCsvRows = useCallback(async () => {
    const requestId = ++csvFetchIdRef.current;
    setCsvLoading(true);
    setCsvError(null);
    try {
      const sigRes = await fetch(`${API_BASE}/csv-signals`);
      if (!sigRes.ok) throw new Error(`API ${sigRes.status}`);
      const rawSignals = await sigRes.json();

      // Each (symbol, timeframe) pair needs its own candle fetch — a CSV can
      // mix timeframes per row, unlike the rest of this app.
      const byPair = new Map();
      for (const s of rawSignals) {
        const key = `${s.symbol}|${s.interval}`;
        if (!byPair.has(key)) byPair.set(key, []);
        byPair.get(key).push(s);
      }

      const nowMs = Date.now();
      const pairCandles = new Map();
      await runWithConcurrency([...byPair.keys()], async key => {
        const [symbol, interval] = key.split("|");
        // Only need enough candles to cover ~30h back (for the 24h change/
        // volume calcs below), not the full 1000-candle history — cuts
        // payload size a lot when there are hundreds of pairs to load.
        const limit = Math.min(1000, Math.max(50, Math.ceil((30 * 3_600_000) / intervalToMs(interval))));
        const res = await fetch(`${API_BASE}/csv-candles/${symbol}?interval=${interval}&limit=${limit}`);
        if (!res.ok) { pairCandles.set(key, []); return; }
        const rawCandles = await res.json();
        pairCandles.set(key, rawCandles.map(r => ({
          openTimeMs: r[0], open: parseFloat(r[1]), high: parseFloat(r[2]),
          low: parseFloat(r[3]), close: parseFloat(r[4]), volume: parseFloat(r[5]),
        })));
      }, CSV_CANDLE_CONCURRENCY);

      if (csvFetchIdRef.current !== requestId) return;

      const findAtOrBefore = (candles, targetMs) => {
        for (let i = candles.length - 1; i >= 0; i--) {
          if (candles[i].openTimeMs <= targetMs) return candles[i];
        }
        return null;
      };

      const rows = rawSignals.map(s => {
        const candles = pairCandles.get(`${s.symbol}|${s.interval}`) || [];
        const latest = candles[candles.length - 1] || null;
        const c1h = findAtOrBefore(candles, nowMs - 3_600_000);
        const c24h = findAtOrBefore(candles, nowMs - 86_400_000);
        const change_1h = (latest && c1h && c1h.close) ? ((latest.close - c1h.close) / c1h.close) * 100 : 0;
        const change_24h = (latest && c24h && c24h.close) ? ((latest.close - c24h.close) / c24h.close) * 100 : 0;
        const volume_24h = candles.filter(c => c.openTimeMs >= nowMs - 86_400_000).reduce((sum, c) => sum + c.volume, 0);

        const ema_trend = (s.ema_fast != null && s.ema_mid != null && s.ema_slow != null)
          ? (s.ema_fast > s.ema_mid && s.ema_mid > s.ema_slow ? "Bullish"
             : s.ema_fast < s.ema_mid && s.ema_mid < s.ema_slow ? "Bearish" : "Neutral")
          : "Neutral";

        return {
          symbol: s.symbol, interval: s.interval, ema_trend,
          score: s.score, price: s.cross_price,
          change_1h, change_24h, volume_24h,
          last_signal: s.signal_type, signal_time: s.cross_time, cross_price: s.cross_price,
          ema_7: s.ema_fast, ema_25: s.ema_mid, ema_99: s.ema_slow,
          _isCsv: true,
        };
      });

      setCsvRows(rows);
    } catch (e) {
      if (csvFetchIdRef.current === requestId) {
        setCsvRows([]);
        setCsvError(e.message);
      }
    } finally {
      if (csvFetchIdRef.current === requestId) setCsvLoading(false);
    }
  }, []);

  // Hydrate from whatever was already imported, so a page reload still shows
  // the last uploaded CSV's coins instead of an empty page.
  useEffect(() => { loadCsvRows(); }, [loadCsvRows]);

  // Direct inline upload — no navigation to a separate page. The file picker
  // fires as soon as a file is chosen; results render right on this page.
  const handleCsvFileSelected = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file again later
    if (!file) return;
    setCsvUploading(true);
    setCsvUploadError(null);
    setCsvSummary(null);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await fetch(`${API_BASE}/csv-import`, { method: "POST", body: formData });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail?.detail || `API ${res.status}`);
      }
      const summary = await res.json();
      setCsvSummary(summary);
      if (summary.signals_imported > 0) loadCsvRows();
    } catch (err) {
      setCsvUploadError(err.message);
    } finally {
      setCsvUploading(false);
    }
  };

  const csvToggleSort = k => setCsvSort(s => s.k===k ? {k, dir:s.dir==="asc"?"desc":"asc"} : {k, dir:"desc"});
  // Scanner page now stays mounted in the background (see App()) so its data
  // survives navigating to Backtest and back — without useMemo here, these
  // would re-filter/re-sort thousands of rows on every unrelated render
  // elsewhere in the app (e.g. any click that changes App's `page` state),
  // even while this page is hidden, causing exactly that kind of lag.
  const csvFilteredRows = useMemo(() => csvRows.filter(r => {
    if (csvTrendFilter !== "All" && r.ema_trend !== csvTrendFilter) return false;
    if (csvSigFilter !== "All" && r.last_signal !== csvSigFilter) return false;
    if (csvSearch && !r.symbol.toLowerCase().includes(csvSearch.toLowerCase())) return false;
    return true;
  }), [csvRows, csvTrendFilter, csvSigFilter, csvSearch]);
  const csvSortedRows = useMemo(() => [...csvFilteredRows].sort((a, b) => {
    const dir = csvSort.dir === "asc" ? 1 : -1;
    const av = a[csvSort.k], bv = b[csvSort.k];
    if (av == null && bv == null) return 0;
    if (av == null) return 1; if (bv == null) return -1;
    return typeof av === "string" ? dir*av.localeCompare(bv) : dir*(av-bv);
  }), [csvFilteredRows, csvSort]);

  const csvPageCount = Math.max(1, Math.ceil(csvSortedRows.length / CSV_PAGE_SIZE));
  // Filters/search/sort changing the result set can leave `csvPage` pointing
  // past the new last page (e.g. was on page 5, a filter now only has 2) —
  // clamp instead of rendering an empty page.
  useEffect(() => {
    if (csvPage > csvPageCount) setCsvPage(csvPageCount);
  }, [csvPage, csvPageCount]);
  useEffect(() => { setCsvPage(1); }, [csvTrendFilter, csvSigFilter, csvSearch, csvSort]);
  const csvPageRows = useMemo(() => {
    const start = (csvPage - 1) * CSV_PAGE_SIZE;
    return csvSortedRows.slice(start, start + CSV_PAGE_SIZE);
  }, [csvSortedRows, csvPage]);

  const csvStats = useMemo(() => {
    const bullish = csvRows.filter(r => r.ema_trend === "Bullish").length;
    const bearish = csvRows.filter(r => r.ema_trend === "Bearish").length;
    const scored  = csvRows.filter(r => r.last_signal);
    const avgScore = scored.length
      ? Math.round(scored.reduce((s, r) => s + (r.score || 0), 0) / scored.length)
      : 0;
    const distinctSymbols = new Set(csvRows.map(r => r.symbol)).size;
    return { bullish, bearish, avgScore, distinctSymbols };
  }, [csvRows]);
  const { bullish: csvBullishCount, bearish: csvBearishCount, avgScore: csvAvgScore, distinctSymbols: csvDistinctSymbols } = csvStats;

  return (
    <div style={{ fontFamily:"'Inter',system-ui,sans-serif", background:"#f5f6f8", minHeight:"100vh", width:"100%", color:"#111827" }}>
      {/* Header */}
      <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", padding:"20px 24px 16px", flexWrap:"wrap", gap:12 }}>
        <div style={{ display:"flex", alignItems:"center", gap:14 }}>
          <button onClick={onHome} style={{
            display:"flex", alignItems:"center", gap:5, padding:"5px 12px",
            borderRadius:7, border:"1px solid #e5e7eb", background:"transparent",
            fontSize:12, fontWeight:600, color:"#374151", cursor:"pointer",
          }}>← Home</button>
          <div>
            <div style={{ fontSize:26, fontWeight:800, letterSpacing:"-0.02em" }}>EMA SCANNER</div>
            <div style={{ fontSize:12, color:"#9ca3af", fontWeight:600, marginTop:2, letterSpacing:"0.02em" }}>
              TRIPLE EMA STRATEGY 7 › 25 › 99
            </div>
          </div>
        </div>
        {/* Once there's data, this same Upload CSV button (and Backtest
            Summary) lives at the right end of the filter bar below instead —
            keep it here too only for the empty-state case, where that bar
            doesn't render at all yet. */}
        {csvRows.length === 0 && (
          <div style={{ display:"flex", alignItems:"center", gap:14, flexWrap:"wrap" }}>
            <input type="file" accept=".csv" ref={csvInputRef} onChange={handleCsvFileSelected} style={{ display:"none" }}/>
            <button onClick={() => csvInputRef.current?.click()} disabled={csvUploading} style={{
              padding:"6px 16px", borderRadius:8, border:"1px solid #6366f1",
              background:"transparent", color:"#6366f1", fontSize:12, fontWeight:700,
              cursor: csvUploading ? "not-allowed" : "pointer", opacity: csvUploading ? 0.6 : 1,
            }}>{csvUploading ? "Uploading…" : "Upload CSV"}</button>
            {csvUploadError && <span style={{ color:"#dc2626", fontSize:11 }}>⚠ {csvUploadError}</span>}
          </div>
        )}
      </div>

      {/* Upload feedback */}
      {csvSummary && (
        <div style={{ margin:"0 24px 16px", background:"#fff", borderRadius:14, border:"1px solid #e5e7eb", padding:"16px 20px" }}>
          <div style={{ fontSize:12, color:"#374151" }}>
            Fetched candle history for <strong>{csvSummary.symbols_fetched}</strong> coin(s), imported{" "}
            <strong>{csvSummary.signals_imported}</strong> signal(s).
          </div>
          {csvSummary.errors?.length > 0 && (
            <div style={{ marginTop:6, color:"#dc2626", fontSize:12 }}>
              {csvSummary.errors.length} coin(s) failed to fetch:
              <ul style={{ margin:"4px 0 0 18px" }}>
                {csvSummary.errors.slice(0, 10).map((e, i) => <li key={i}>{e}</li>)}
              </ul>
            </div>
          )}
          {csvSummary.warnings?.length > 0 && (
            <div style={{ marginTop:6, color:"#9a5b13", fontSize:12 }}>
              {csvSummary.warnings.length} row(s) skipped:
              <ul style={{ margin:"4px 0 0 18px" }}>
                {csvSummary.warnings.slice(0, 10).map((w, i) => <li key={i}>{w}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}

      {csvLoading && csvRows.length === 0 ? (
        <div style={{ padding:60, textAlign:"center", color:"#9ca3af", fontSize:13 }}>Loading CSV data…</div>
      ) : csvError ? (
        <div style={{ margin:"0 24px 16px", padding:24, textAlign:"center", color:"#dc2626", fontSize:13, background:"#fff", borderRadius:14, border:"1px solid #e5e7eb" }}>⚠ {csvError}</div>
      ) : csvRows.length === 0 ? (
        <div style={{ margin:"0 24px 16px", padding:60, textAlign:"center", color:"#9ca3af", fontSize:13, background:"#fff", borderRadius:14, border:"1px solid #e5e7eb" }}>
          No CSV data yet — click "Upload CSV" to import your signals.
        </div>
      ) : (
        <>
          {/* Summary cards */}
          <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(220px,1fr))", gap:16, padding:"0 24px 16px" }}>
            <SummaryCard
              label="Bullish" badge="7›25›99" badgeBg="#bbf1d3" badgeColor="#166534"
              value={csvBullishCount} valueColor="#16a34a" bg="#e7f8ef" Icon={TrendingUp}
            />
            <SummaryCard
              label="Bearish" badge="99›25›7" badgeBg="#fbcfd1" badgeColor="#991b1b"
              value={csvBearishCount} valueColor="#dc2626" bg="#fdecec" Icon={TrendingDown}
            />
            <SummaryCard
              label="Avg Score" badge="AVG" badgeBg="#111827" badgeColor="#fff"
              value={csvAvgScore} valueColor="#111827" bg="#e7e7fb" Icon={Target}
            />
            <SummaryCard
              label="Stored" badge="TOTAL" badgeBg="#fcd9a8" badgeColor="#9a5b13"
              value={csvDistinctSymbols} valueColor="#f59e0b" bg="#fdf1e2" Icon={Database}
            />
          </div>

          {/* Search */}
          <div style={{ padding:"0 24px 12px" }}>
            <div style={{ position:"relative", maxWidth:260 }}>
              <span style={{ position:"absolute", left:14, top:"50%", transform:"translateY(-50%)", color:"#9ca3af", fontSize:13 }}>⌕</span>
              <input placeholder="Search coins…" value={csvSearch} onChange={e=>setCsvSearch(e.target.value)} style={{
                width:"100%", padding:"9px 14px 9px 32px", borderRadius:10, border:"1px solid #e5e7eb", fontSize:13,
                color:"#374151", outline:"none", background:"#fff", boxSizing:"border-box",
              }}/>
            </div>
            <div style={{ marginTop:8, fontSize:12, color:"#9ca3af" }}>
              <strong style={{ color:"#374151" }}>{csvSortedRows.length}</strong> entries
            </div>
          </div>

          {/* Filters */}
          <div style={{ padding:"16px 24px", display:"flex", alignItems:"center", gap:16, flexWrap:"wrap", borderTop:"1px solid #e5e7eb", borderBottom:"1px solid #e5e7eb" }}>
            <div style={{ display:"flex", gap:4 }}>
              {["All","Bullish","Bearish"].map(t => (
                <button key={t} onClick={()=>setCsvTrendFilter(t)} style={{
                  padding:"6px 16px", borderRadius:8, fontSize:12, fontWeight:700, cursor:"pointer",
                  border: csvTrendFilter===t ? "1px solid #111827" : "1px solid #e5e7eb",
                  background: csvTrendFilter===t ? "#111827" : "#fff",
                  color: csvTrendFilter===t ? "#fff" : "#6b7280", textTransform:"uppercase",
                }}>{t}</button>
              ))}
            </div>
            <div style={{ width:1, height:20, background:"#e5e7eb" }}/>
            <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase" }}>Signal</span>
            <div style={{ display:"flex", gap:4 }}>
              {SIGS.map(s => (
                <button key={s} onClick={()=>setCsvSigFilter(s)} style={{
                  padding:"5px 13px", borderRadius:7, fontSize:12, fontWeight:600, cursor:"pointer",
                  border:csvSigFilter===s?"1.5px solid #f59e0b":"1px solid #e5e7eb", background:csvSigFilter===s?"#fff7ed":"#fff",
                  color:csvSigFilter===s?"#f59e0b":"#6b7280",
                }}>{s}</button>
              ))}
            </div>
            <div style={{ marginLeft:"auto", display:"flex", alignItems:"center", gap:8, flexWrap:"wrap" }}>
              <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase" }}>Sort</span>
              {[["signal_time","Time"],["score","Score"],["volume_24h","Vol"],["change_24h","24H %"]].map(([k,l]) => (
                <button key={k} onClick={()=>csvToggleSort(k)} style={{
                  padding:"5px 13px", borderRadius:7, fontSize:12, fontWeight:600, cursor:"pointer",
                  border:csvSort.k===k?"1.5px solid #6366f1":"1px solid #e5e7eb", background:csvSort.k===k?"#eef2ff":"#fff",
                  color:csvSort.k===k?"#6366f1":"#6b7280",
                }}>{l}{csvSort.k===k ? (csvSort.dir==="asc"?" ↑":" ↓") : ""}</button>
              ))}
              <div style={{ width:1, height:20, background:"#e5e7eb" }}/>
              <button onClick={onScreenerBacktest} style={{
                padding:"6px 16px", borderRadius:8, border:"1px solid #6366f1",
                background:"transparent", color:"#6366f1", fontSize:12, fontWeight:700, cursor:"pointer", whiteSpace:"nowrap",
              }}>Backtest Summary</button>
              <input type="file" accept=".csv" ref={csvInputRef} onChange={handleCsvFileSelected} style={{ display:"none" }}/>
              <button onClick={() => csvInputRef.current?.click()} disabled={csvUploading} style={{
                padding:"6px 16px", borderRadius:8, border:"1px solid #6366f1",
                background:"transparent", color:"#6366f1", fontSize:12, fontWeight:700, whiteSpace:"nowrap",
                cursor: csvUploading ? "not-allowed" : "pointer", opacity: csvUploading ? 0.6 : 1,
              }}>{csvUploading ? "Uploading…" : "Upload CSV"}</button>
              {csvUploadError && <span style={{ color:"#dc2626", fontSize:11 }}>⚠ {csvUploadError}</span>}
            </div>
          </div>

          {/* Table */}
          <div style={{ margin:"16px 24px 24px", background:"#fff", borderRadius:14, border:"1px solid #e5e7eb", overflow:"hidden" }}>
          <div style={{ overflowX:"auto" }}>
            <table style={{ width:"100%", borderCollapse:"collapse", fontSize:13 }}>
              <thead>
                <tr>
                  {SCOLS.map(col => (
                    <TH key={col.k} right={col.r} onClick={col.s?()=>csvToggleSort(col.k):null}
                      sorted={csvSort.k===col.k} dir={csvSort.dir}>{col.l}</TH>
                  ))}
                </tr>
              </thead>
              <tbody>
                {csvSortedRows.length === 0 ? (
                  <tr><td colSpan={11} style={{ padding:48, textAlign:"center", color:"#9ca3af", fontSize:13 }}>No coins match your filters.</td></tr>
                ) : csvPageRows.map((row, iOnPage) => {
                  const i = (csvPage - 1) * CSV_PAGE_SIZE + iOnPage;
                  const { base, quote } = fmtSym(row.symbol);
                  return (
                    <tr key={`${row.symbol}-${row.interval}-${i}`}
                      style={{ borderBottom:"1px solid #f3f4f6", background:i%2===0?"#fff":"#fafafa", transition:"background 0.1s" }}
                      onMouseEnter={e=>e.currentTarget.style.background="#f0f9ff"}
                      onMouseLeave={e=>e.currentTarget.style.background=i%2===0?"#fff":"#fafafa"}
                    >
                      <td style={{ padding:"11px 14px", color:"#9ca3af", fontWeight:500 }}>{i + 1}</td>
                      <td style={{ padding:"11px 14px", fontWeight:700, cursor:"pointer" }} onClick={()=>setModal(row)}>
                        <span style={{ color:"#f59e0b" }}>{base}</span>
                        <span style={{ color:"#9ca3af", fontSize:11 }}>{quote}</span>
                      </td>
                      <td style={{ padding:"11px 14px" }}><Trend t={row.ema_trend}/></td>
                      <td style={{ padding:"11px 14px", textAlign:"right" }}><ScoreBadge v={row.score}/></td>
                      <td style={{ padding:"11px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", fontWeight:500 }}>{fmtPrice(row.price)}</td>
                      <td style={{ padding:"11px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums" }}><ChgCell v={row.change_1h}/></td>
                      <td style={{ padding:"11px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums" }}><ChgCell v={row.change_24h}/></td>
                      <td style={{ padding:"11px 14px", textAlign:"right", color:"#374151", fontVariantNumeric:"tabular-nums" }}>{fmtVol(row.volume_24h)}</td>
                      <td style={{ padding:"11px 14px" }}><SigBadge s={row.last_signal}/></td>
                      <td style={{ padding:"11px 14px", color:"#6b7280", whiteSpace:"nowrap", fontSize:12 }}>
                        {row.signal_time ? fmtTime(row.signal_time) : "—"}
                        <span style={{ marginLeft:6, fontSize:10, color:"#9ca3af", border:"1px solid #e5e7eb", borderRadius:4, padding:"1px 5px" }}>{row.interval}</span>
                      </td>
                      <td style={{ padding:"11px 14px" }}>
                        <button
                          onClick={() => setModal(row)}
                          style={{
                            padding:"4px 12px", borderRadius:6, border:"1px solid #6366f1",
                            background:"transparent", color:"#6366f1", fontSize:11, fontWeight:600,
                            cursor:"pointer", whiteSpace:"nowrap", transition:"all 0.15s",
                          }}
                          onMouseEnter={e=>{e.target.style.background="#6366f1";e.target.style.color="#fff";}}
                          onMouseLeave={e=>{e.target.style.background="transparent";e.target.style.color="#6366f1";}}
                        >
                          Details
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {csvSortedRows.length > 0 && (
            <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", padding:"12px 16px", borderTop:"1px solid #e5e7eb" }}>
              <span style={{ fontSize:12, color:"#9ca3af" }}>
                Showing {(csvPage-1)*CSV_PAGE_SIZE+1}–{Math.min(csvPage*CSV_PAGE_SIZE, csvSortedRows.length)} of {csvSortedRows.length}
              </span>
              <div style={{ display:"flex", alignItems:"center", gap:8 }}>
                <button onClick={()=>setCsvPage(p=>Math.max(1,p-1))} disabled={csvPage<=1} style={{
                  padding:"6px 14px", borderRadius:7, fontSize:12, fontWeight:600,
                  border:"1px solid #e5e7eb", background:"#fff", color:csvPage<=1?"#d1d5db":"#374151",
                  cursor:csvPage<=1?"default":"pointer",
                }}>Prev</button>
                <span style={{ fontSize:12, color:"#6b7280" }}>Page {csvPage} of {csvPageCount}</span>
                <button onClick={()=>setCsvPage(p=>Math.min(csvPageCount,p+1))} disabled={csvPage>=csvPageCount} style={{
                  padding:"6px 14px", borderRadius:7, fontSize:12, fontWeight:600,
                  border:"1px solid #e5e7eb", background:"#fff", color:csvPage>=csvPageCount?"#d1d5db":"#374151",
                  cursor:csvPage>=csvPageCount?"default":"pointer",
                }}>Next</button>
              </div>
            </div>
          )}
          </div>
        </>
      )}

      {/* Detail modal */}
      {modal && (
        <div onClick={()=>setModal(null)} style={{ position:"fixed", inset:0, background:"rgba(0,0,0,0.35)", display:"flex", alignItems:"center", justifyContent:"center", zIndex:1000 }}>
          <div onClick={e=>e.stopPropagation()} style={{ background:"#fff", borderRadius:14, padding:28, width:440, border:"1px solid #e5e7eb" }}>
            <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", marginBottom:20 }}>
              <div>
                <div style={{ fontSize:22, fontWeight:700 }}>
                  <span style={{ color:"#f59e0b" }}>{fmtSym(modal.symbol).base}</span>
                  <span style={{ color:"#9ca3af", fontSize:14 }}>/USDT</span>
                </div>
                <Trend t={modal.ema_trend}/>
              </div>
              <button onClick={()=>setModal(null)} style={{ background:"none", border:"none", cursor:"pointer", fontSize:20, color:"#9ca3af" }}>×</button>
            </div>
            <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:12, marginBottom:16 }}>
              {[
                {l:"Score", v:<ScoreBadge v={modal.score}/>},
                {l:"Price", v:`$${fmtPrice(modal.price)}`},
                {l:"1H Change", v:<ChgCell v={modal.change_1h}/>},
                {l:"24H Change", v:<ChgCell v={modal.change_24h}/>},
                {l:"Volume (24H)", v:fmtVol(modal.volume_24h)},
                {l:"Last Signal", v:<SigBadge s={modal.last_signal}/>},
                {l:"Cross Price", v:modal.cross_price?`$${fmtPrice(modal.cross_price)}`:"—"},
                {l:"Signal Time", v:modal.signal_time?fmtTime(modal.signal_time):"—"},
              ].map(item => (
                <div key={item.l} style={{ background:"#f9fafb", borderRadius:8, padding:"10px 14px" }}>
                  <div style={{ fontSize:11, color:"#9ca3af", fontWeight:600, textTransform:"uppercase", letterSpacing:"0.06em", marginBottom:4 }}>{item.l}</div>
                  <div style={{ fontSize:14, fontWeight:500, color:"#111827" }}>{item.v}</div>
                </div>
              ))}
            </div>
            {(modal.ema_7||modal.ema_25||modal.ema_99) && (
              <div style={{ background:"#f9fafb", borderRadius:8, padding:"10px 14px" }}>
                <div style={{ fontSize:11, color:"#9ca3af", fontWeight:600, textTransform:"uppercase", letterSpacing:"0.06em", marginBottom:8 }}>EMA Values</div>
                <div style={{ display:"flex", gap:16 }}>
                  {[["EMA 7",modal.ema_7],["EMA 25",modal.ema_25],["EMA 99",modal.ema_99]].map(([l,v])=>(
                    <div key={l}><div style={{ fontSize:11, color:"#9ca3af" }}>{l}</div><div style={{ fontSize:13, fontWeight:600, color:"#374151" }}>{v?fmtPrice(v):"—"}</div></div>
                  ))}
                </div>
              </div>
            )}
            <button onClick={()=>{setModal(null);onBacktest(modal);}} style={{
              marginTop:16, width:"100%", padding:"9px 0", borderRadius:8,
              border:"1px solid #6366f1", background:"transparent", color:"#6366f1",
              fontSize:13, fontWeight:600, cursor:"pointer",
            }}>Run Backtest for {fmtSym(modal.symbol).base}/USDT</button>
          </div>
        </div>
      )}
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        input:focus{border-color:#f59e0b!important;box-shadow:0 0 0 3px rgba(245,158,11,.12);outline:none;}
      `}</style>
    </div>
  );
}
// Wrapped in memo() because App() now keeps this component permanently
// mounted (just hidden) so its data survives navigating away and back —
// without memo, every click anywhere in the app (any `page` state change)
// would still re-run this whole function, including re-mapping thousands of
// CSV rows into table JSX, even while completely invisible.
const ScannerPage = memo(ScannerPageImpl);

// ─── Screener Backtest Page (aggregate Day/Week/Month WIN/LOSS/OPEN) ─────────
const BT_PERIOD_LABELS = [["day","Day"],["week","Week"],["month","Month"]];

function ScreenerBacktestPage({ onBack }) {
  const [btPeriod, setBtPeriod] = useState("day");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [btRrMode, setBtRrMode] = useState("1:2");
  const [btCapital, setBtCapital] = useState(1000);
  const [btLoading, setBtLoading] = useState(true);
  const [btError, setBtError] = useState(null);
  const [trades, setTrades] = useState([]);
  const [btTimeframeFilter, setBtTimeframeFilter] = useState("All");
  const [sort, setSort] = useState({ k:"_signalTimeMs", dir:"desc" });
  const [exporting, setExporting] = useState(false);
  const fetchIdRef = useRef(0);
  const customStartRef = useRef(null);
  const customEndRef = useRef(null);

  // Aggregate Day/Week/Month backtest — how many of ALL 1H signals in this
  // window resolved as WIN/LOSS, or are still OPEN. Reuses the same
  // signal+candle-driven SL/TP simulation as the per-coin Backtest page
  // (runBacktestFromSignals) — never recomputed on the backend, per this
  // app's "signals from DB, SL/TP simulation on the frontend" convention.
  const fetchBacktestStats = useCallback(async () => {
    // Guards against out-of-order responses: if the period/RR mode changes
    // again before this request lands (e.g. Day -> Month clicked quickly),
    // a slower older request must not be allowed to clobber the newer one's
    // state once it resolves.
    const requestId = ++fetchIdRef.current;

    // Wait for BOTH From and To before reloading — picking just one used to
    // silently fall back to whatever Day/Week/Month preset was last
    // selected (isCustom below requires both), reloading with a range the
    // user hadn't actually finished choosing yet.
    if (Boolean(customStart) !== Boolean(customEnd)) {
      setBtLoading(false);
      return;
    }

    setBtError(null);
    setBtTimeframeFilter("All"); // last period's selected timeframe may not exist in the new result set

    // Picking both a from/to date always takes priority over the Day/Week/
    // Month buttons — no separate "Custom" mode to switch into first.
    const isCustom = !!(customStart && customEnd);
    // "Day" means the actual calendar day (IST midnight to now) — same
    // boundary the backend's "Signals today" count uses — not a rolling
    // last-24-hours window, which would spill into yesterday evening.
    const isCalendarDay = btPeriod === "day" && !isCustom;

    setBtLoading(true);
    try {
      // /api/csv-signals returns everything with no day/range filter, so the
      // window is always narrowed down to [rangeStartMs, rangeEndMs] here —
      // unlike the old scanner endpoint, there's no server-side "last N days"
      // shortcut to lean on.
      let rangeStartMs, rangeEndMs;
      if (isCustom) {
        rangeStartMs = new Date(`${customStart}T00:00:00`).getTime();
        rangeEndMs   = new Date(`${customEnd}T23:59:59.999`).getTime();
      } else if (isCalendarDay) {
        const IST_OFFSET_MS = 5.5 * 3_600_000;
        const nowIst = Date.now() + IST_OFFSET_MS;
        const todayIstMidnightIst = Math.floor(nowIst / 86_400_000) * 86_400_000;
        rangeStartMs = todayIstMidnightIst - IST_OFFSET_MS; // back to real UTC ms
        rangeEndMs   = Date.now();
      } else {
        rangeStartMs = Date.now() - BT_PERIOD_DAYS[btPeriod] * 86_400_000;
        rangeEndMs   = Date.now();
      }

      const sigRes = await fetch(`${API_BASE}/csv-signals`);
      if (!sigRes.ok) throw new Error(`API ${sigRes.status}`);
      const rawSignals = await sigRes.json();

      // A CSV can carry several timeframes per coin (5m/15m/1h/...), each
      // needing its own candle fetch and its own candle-duration for
      // dedupe/simulation — group by (symbol, interval) instead of just
      // symbol, same pattern as ScannerPage.loadCsvRows. The backend can
      // also end up with more than one stored row for what's really the
      // same crossover, so collapse by which candle it maps to, same as
      // before, just using each pair's own interval instead of a fixed 1H.
      const byPair = new Map();
      const seenKeys = new Set();
      for (const s of rawSignals) {
        const crossTimeMs = new Date(s.cross_time).getTime();
        if (crossTimeMs < rangeStartMs || crossTimeMs > rangeEndMs) continue;
        const candleMs = intervalToMs(s.interval);
        const dedupeKey = `${s.symbol}|${s.interval}|${s.signal_type}|${Math.floor(crossTimeMs / candleMs)}`;
        if (seenKeys.has(dedupeKey)) continue;
        seenKeys.add(dedupeKey);
        const pairKey = `${s.symbol}|${s.interval}`;
        if (!byPair.has(pairKey)) byPair.set(pairKey, []);
        byPair.get(pairKey).push({ type: s.signal_type, crossPrice: s.cross_price, crossTimeMs });
      }

      // The selected Period can span well beyond what 1000 candles covers
      // for a short timeframe (1000 5m candles ≈ 3.5 days) — same fix as the
      // per-coin Backtest Details page's ensure-depth call, just applied
      // per pair here since this view mixes every coin/timeframe at once.
      // A wide Month/custom range can mean hundreds of pairs genuinely need
      // fresh Binance history at once — firing all of those concurrently
      // (unbounded Promise.all) floods Binance from many separate ccxt
      // clients that can't see each other's rate limiting, which gets them
      // throttled and stuck retrying with multi-second backoffs. Bounding
      // concurrency here (same helper/limit ScannerPage's CSV load uses)
      // keeps total in-flight requests reasonable so it actually finishes.
      const periodDays = Math.ceil((rangeEndMs - rangeStartMs) / 86_400_000);
      const perPair = await runWithConcurrency([...byPair.entries()], async ([pairKey, sigs]) => {
        const [symbol, interval] = pairKey.split("|");
        const neededCandles = Math.ceil((periodDays * 86_400_000) / intervalToMs(interval)) + 50;
        if (neededCandles > 1000) {
          try {
            await fetch(`${API_BASE}/csv-candles/${symbol}/ensure-depth?interval=${interval}&days=${periodDays}`, { method: "POST" });
          } catch {
            // Non-fatal — fall through and use whatever history is already stored.
          }
        }
        const candleLimit = Math.min(20_000, Math.max(1000, neededCandles));
        const res = await fetch(`${API_BASE}/csv-candles/${symbol}?interval=${interval}&limit=${candleLimit}`);
        if (!res.ok) return [];
        const rawCandles = await res.json();
        const candles = rawCandles.map(r => ({
          symbol, openTimeMs: r[0],
          open: parseFloat(r[1]), high: parseFloat(r[2]), low: parseFloat(r[3]), close: parseFloat(r[4]),
        }));
        // runBacktestFromSignals requires oldest->newest.
        const sortedSigs = [...sigs].sort((a, b) => a.crossTimeMs - b.crossTimeMs);
        return runBacktestFromSignals(candles, sortedSigs, btRrMode, 3650, intervalToMs(interval), interval, btCapital);
      }, CSV_CANDLE_CONCURRENCY);

      if (fetchIdRef.current !== requestId) return; // a newer request has since superseded this one

      setTrades(perPair.flat());
    } catch (e) {
      if (fetchIdRef.current === requestId) {
        setTrades([]);
        setBtError(e.message);
      }
    } finally {
      if (fetchIdRef.current === requestId) setBtLoading(false);
    }
  }, [btPeriod, customStart, customEnd, btRrMode, btCapital]);

  useEffect(() => { fetchBacktestStats(); }, [fetchBacktestStats]);

  // Distinct timeframes present in the current result set, ordered by actual
  // duration (5m before 1h before 1d) rather than alphabetically.
  const availableTimeframes = useMemo(() => {
    const seen = [...new Set(trades.map(t => t.timeFrame))];
    seen.sort((a, b) => intervalToMs(a.toLowerCase()) - intervalToMs(b.toLowerCase()));
    return seen;
  }, [trades]);

  const filteredTrades = useMemo(() => (
    btTimeframeFilter === "All" ? trades : trades.filter(t => t.timeFrame === btTimeframeFilter)
  ), [trades, btTimeframeFilter]);

  // Stat cards (Won/Loss/PnL/Win rate) recompute from whichever timeframe is
  // selected, instead of always reflecting the full unfiltered result set.
  const btStats = useMemo(() => {
    const won    = filteredTrades.filter(t => t.result === "WIN").length;
    const lost   = filteredTrades.filter(t => t.result === "LOSS").length;
    const closed = filteredTrades.filter(t => t.result === "WIN" || t.result === "LOSS");
    const pnl    = closed.reduce((sum, t) => sum + t.gainPct, 0);
    const pnlDollar = closed.reduce((sum, t) => sum + t.gainDollar, 0);
    const winRate = closed.length > 0 ? (won / closed.length) * 100 : null;
    return { won, lost, pnl, pnlDollar, winRate, total: filteredTrades.length };
  }, [filteredTrades]);

  const toggleSort = k => setSort(s => s.k===k ? {k, dir:s.dir==="asc"?"desc":"asc"} : {k, dir:"desc"});
  const sortedTrades = [...filteredTrades].sort((a, b) => {
    const dir = sort.dir === "asc" ? 1 : -1;
    const av = a[sort.k], bv = b[sort.k];
    if (av == null && bv == null) return 0;
    if (av == null) return 1; if (bv == null) return -1;
    return typeof av === "string" ? dir*av.localeCompare(bv) : dir*(av-bv);
  });

  // Export CSV — re-runs the same signal+candle simulation for Day, Week,
  // AND Month (independent of whichever period is currently selected on
  // screen) and downloads one combined file with a Period column, so you
  // get all three windows in a single export rather than just what's shown.
  const exportAllPeriods = useCallback(async () => {
    setExporting(true);
    try {
      const allRows = [];
      // /csv-signals has no query params, so it's the same for every period —
      // fetch it once and reuse for Day/Week/Month instead of refetching 3x.
      const sigRes = await fetch(`${API_BASE}/csv-signals`);
      if (!sigRes.ok) throw new Error(`API ${sigRes.status}`);
      const rawSignals = await sigRes.json();

      for (const period of ["day", "week", "month"]) {
        // "day" means the actual calendar day (IST midnight to now), same as
        // the on-screen stats — not a rolling last-24-hours window.
        let rangeStartMs, rangeEndMs;
        if (period === "day") {
          const IST_OFFSET_MS = 5.5 * 3_600_000;
          const nowIst = Date.now() + IST_OFFSET_MS;
          const todayIstMidnightIst = Math.floor(nowIst / 86_400_000) * 86_400_000;
          rangeStartMs = todayIstMidnightIst - IST_OFFSET_MS;
          rangeEndMs   = Date.now();
        } else {
          rangeStartMs = Date.now() - BT_PERIOD_DAYS[period] * 86_400_000;
          rangeEndMs   = Date.now();
        }

        // Same (symbol, interval) grouping + near-duplicate collapse as the
        // on-screen stats — otherwise a signal the backend stored twice
        // would double-count in the export.
        const byPair = new Map();
        const seenKeys = new Set();
        for (const s of rawSignals) {
          const crossTimeMs = new Date(s.cross_time).getTime();
          if (crossTimeMs < rangeStartMs || crossTimeMs > rangeEndMs) continue;
          const candleMs = intervalToMs(s.interval);
          const dedupeKey = `${s.symbol}|${s.interval}|${s.signal_type}|${Math.floor(crossTimeMs / candleMs)}`;
          if (seenKeys.has(dedupeKey)) continue;
          seenKeys.add(dedupeKey);
          const pairKey = `${s.symbol}|${s.interval}`;
          if (!byPair.has(pairKey)) byPair.set(pairKey, []);
          byPair.get(pairKey).push({ type: s.signal_type, crossPrice: s.cross_price, crossTimeMs });
        }

        const periodDays = Math.ceil((rangeEndMs - rangeStartMs) / 86_400_000);
        const perPair = await runWithConcurrency([...byPair.entries()], async ([pairKey, sigs]) => {
          const [symbol, interval] = pairKey.split("|");
          const neededCandles = Math.ceil((periodDays * 86_400_000) / intervalToMs(interval)) + 50;
          if (neededCandles > 1000) {
            try {
              await fetch(`${API_BASE}/csv-candles/${symbol}/ensure-depth?interval=${interval}&days=${periodDays}`, { method: "POST" });
            } catch {
              // Non-fatal — fall through and use whatever history is already stored.
            }
          }
          const candleLimit = Math.min(20_000, Math.max(1000, neededCandles));
          const res = await fetch(`${API_BASE}/csv-candles/${symbol}?interval=${interval}&limit=${candleLimit}`);
          if (!res.ok) return [];
          const rawCandles = await res.json();
          const candles = rawCandles.map(r => ({
            symbol, openTimeMs: r[0],
            open: parseFloat(r[1]), high: parseFloat(r[2]), low: parseFloat(r[3]), close: parseFloat(r[4]),
          }));
          const sortedSigs = [...sigs].sort((a, b) => a.crossTimeMs - b.crossTimeMs);
          return runBacktestFromSignals(candles, sortedSigs, btRrMode, 3650, intervalToMs(interval), interval, btCapital);
        }, CSV_CANDLE_CONCURRENCY);

        for (const t of perPair.flat()) allRows.push({ period, ...t });
      }

      const header = [
        "Period","Symbol","Timeframe","Signal Type","Signal Time","Entry Time","Entry Price",
        "Stop Loss","Take Profit","Exit Time","Exit Price","Exit Reason",
        "Duration","PnL %","PnL Amount","PnL ($)","Result",
      ];
      const csvEscape = v => `"${String(v ?? "").replace(/"/g, '""')}"`;
      const lines = [header.join(",")];
      for (const t of allRows) {
        const open = t.result === "OPEN";
        lines.push([
          t.period.toUpperCase(), t.symbol, t.timeFrame, t.tradeSignal, t.signalTime || "", t.entryTime,
          t.entryPrice, t.stopLoss, t.targetPrice,
          open ? "Still running" : t.entryCloseTime,
          open ? "" : t.entryClose,
          t.exitReason, t.duration,
          open ? "" : t.gainPct.toFixed(2),
          open ? "" : t.gainAmount,
          open ? "" : t.gainDollar.toFixed(2),
          t.result,
        ].map(csvEscape).join(","));
      }

      // Summary block — same Won/Loss/Win Rate/PnL($) math as the on-screen
      // stat cards (btStats above), broken down per period plus a grand total.
      const summaryFor = rows => {
        const won = rows.filter(t => t.result === "WIN").length;
        const lost = rows.filter(t => t.result === "LOSS").length;
        const closed = rows.filter(t => t.result === "WIN" || t.result === "LOSS");
        const pnlDollar = closed.reduce((sum, t) => sum + t.gainDollar, 0);
        const winRate = closed.length > 0 ? (won / closed.length) * 100 : null;
        return { total: rows.length, won, lost, winRate, pnlDollar };
      };

      lines.push("");
      lines.push("Summary");
      lines.push(["Period","Total Trades","Won","Loss","Win Rate %","PnL ($)"].map(csvEscape).join(","));
      for (const period of ["day", "week", "month"]) {
        const s = summaryFor(allRows.filter(t => t.period === period));
        lines.push([
          period.toUpperCase(), s.total, s.won, s.lost,
          s.winRate != null ? s.winRate.toFixed(1) : "—",
          s.pnlDollar.toFixed(2),
        ].map(csvEscape).join(","));
      }
      const overall = summaryFor(allRows);
      lines.push([
        "TOTAL", overall.total, overall.won, overall.lost,
        overall.winRate != null ? overall.winRate.toFixed(1) : "—",
        overall.pnlDollar.toFixed(2),
      ].map(csvEscape).join(","));

      // Leading BOM so Excel detects UTF-8 and renders "—" correctly instead
      // of the mojibake "â€"" it shows without one.
      const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `backtest_${btRrMode.replace(":","-")}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e) {
      setBtError(`Export failed: ${e.message}`);
    } finally {
      setExporting(false);
    }
  }, [btRrMode, btCapital]);

  return (
    <div style={{ fontFamily:"'Inter',system-ui,sans-serif", background:"#f5f6f8", minHeight:"100vh", width:"100%", color:"#111827" }}>
      {/* Header */}
      <div style={{ display:"flex", alignItems:"center", gap:10, padding:"13px 24px", borderBottom:"1px solid #e8eaed", flexWrap:"wrap" }}>
        <button onClick={onBack} style={{
          display:"flex", alignItems:"center", gap:5, padding:"5px 12px",
          borderRadius:7, border:"1px solid #e5e7eb", background:"transparent",
          fontSize:12, fontWeight:600, color:"#374151", cursor:"pointer",
        }}>← Back</button>
        <span style={{ color:"#d1d5db", fontSize:14 }}>›</span>
        <span style={{ fontWeight:800, fontSize:17 }}>Backtest Summary</span>
        <span style={{ color:"#9ca3af", fontSize:12 }}>All uploaded CSV coins · mixed timeframes</span>
        <button onClick={exportAllPeriods} disabled={exporting} style={{
          marginLeft:"auto", display:"flex", alignItems:"center", gap:6,
          padding:"7px 16px", borderRadius:8, border:"none",
          background:"#111827", color:"#fff", fontSize:12, fontWeight:700,
          cursor: exporting ? "not-allowed" : "pointer", opacity: exporting ? 0.6 : 1,
        }}>{exporting ? "Exporting…" : "⭳ Export"}</button>
      </div>

      {/* Controls + cards */}
      <div style={{ padding:"20px 24px" }}>
        <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:16, flexWrap:"wrap" }}>
          <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase" }}>Period</span>
          <div style={{ display:"flex", gap:4 }}>
            {BT_PERIOD_LABELS.map(([k,l]) => (
              <button key={k} disabled={btLoading} onClick={()=>{ setBtPeriod(k); setCustomStart(""); setCustomEnd(""); }} style={{
                padding:"5px 14px", borderRadius:7, fontSize:12, fontWeight:700,
                cursor: btLoading ? "not-allowed" : "pointer",
                opacity: btLoading && (btPeriod!==k || customStart || customEnd) ? 0.5 : 1,
                border: (btPeriod===k && !customStart && !customEnd) ? "1px solid #111827" : "1px solid #e5e7eb",
                background: (btPeriod===k && !customStart && !customEnd) ? "#111827" : "#fff",
                color: (btPeriod===k && !customStart && !customEnd) ? "#fff" : "#6b7280",
              }}>{l}</button>
            ))}
          </div>
          <div style={{ display:"flex", alignItems:"center", gap:6 }}>
            <div style={{ position:"relative", display:"flex", alignItems:"center" }}>
              <input
                ref={customStartRef}
                type="date" disabled={btLoading} value={customStart} max={customEnd || undefined}
                onChange={e => setCustomStart(e.target.value)}
                className="date-input-custom-icon"
                style={{ padding:"4px 26px 4px 8px", borderRadius:6, border:"1px solid #e5e7eb", fontSize:12, color:"#374151" }}
              />
              <CalendarClock
                size={14} strokeWidth={2}
                onClick={() => customStartRef.current?.showPicker?.() ?? customStartRef.current?.focus()}
                style={{ position:"absolute", right:7, color:"#9ca3af", cursor: btLoading ? "default" : "pointer", pointerEvents: btLoading ? "none" : "auto" }}
              />
            </div>
            <span style={{ color:"#9ca3af", fontSize:12 }}>to</span>
            <div style={{ position:"relative", display:"flex", alignItems:"center" }}>
              <input
                ref={customEndRef}
                type="date" disabled={btLoading} value={customEnd} min={customStart || undefined}
                onChange={e => setCustomEnd(e.target.value)}
                className="date-input-custom-icon"
                style={{ padding:"4px 26px 4px 8px", borderRadius:6, border:"1px solid #e5e7eb", fontSize:12, color:"#374151" }}
              />
              <CalendarClock
                size={14} strokeWidth={2}
                onClick={() => customEndRef.current?.showPicker?.() ?? customEndRef.current?.focus()}
                style={{ position:"absolute", right:7, color:"#9ca3af", cursor: btLoading ? "default" : "pointer", pointerEvents: btLoading ? "none" : "auto" }}
              />
            </div>
          </div>
          <div style={{ width:1, height:20, background:"#e5e7eb" }}/>
          <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase" }}>RR</span>
          <div style={{ display:"flex", gap:4 }}>
            {RR_MODES.map(m => (
              <button key={m} disabled={btLoading} onClick={()=>setBtRrMode(m)} style={{
                padding:"5px 14px", borderRadius:7, fontSize:12, fontWeight:700,
                cursor: btLoading ? "not-allowed" : "pointer", opacity: btLoading && btRrMode!==m ? 0.5 : 1,
                border: btRrMode===m ? "1.5px solid #f59e0b" : "1px solid #e5e7eb",
                background: btRrMode===m ? "#fff7ed" : "#fff",
                color: btRrMode===m ? "#f59e0b" : "#6b7280",
              }}>{m}</button>
            ))}
            {SHOW_SWING_BUTTON && (
              <button disabled={btLoading} onClick={()=>setBtRrMode("swing")} title="SL = just past the nearest swing low/high before entry, TP = same distance" style={{
                padding:"5px 14px", borderRadius:7, fontSize:12, fontWeight:700,
                cursor: btLoading ? "not-allowed" : "pointer", opacity: btLoading && btRrMode!=="swing" ? 0.5 : 1,
                border: btRrMode==="swing" ? "1.5px solid #f59e0b" : "1px solid #e5e7eb",
                background: btRrMode==="swing" ? "#fff7ed" : "#fff",
                color: btRrMode==="swing" ? "#f59e0b" : "#6b7280",
              }}>Swing SL/TP</button>
            )}
          </div>
          <div style={{ width:1, height:20, background:"#e5e7eb" }}/>
          <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase" }}>Capital</span>
          <div style={{ display:"flex", alignItems:"center", gap:4 }}>
            <span style={{ color:"#9ca3af", fontSize:12 }}>$</span>
            <input
              type="number" min="1" step="100" disabled={btLoading} defaultValue={btCapital}
              onBlur={e => { const v = parseFloat(e.target.value); if (v > 0) setBtCapital(v); }}
              onKeyDown={e => { if (e.key === "Enter") e.target.blur(); }}
              style={{ width:80, padding:"4px 6px", borderRadius:6, border:"1px solid #e5e7eb", fontSize:12 }}
            />
          </div>
          {btLoading && <span style={{ fontSize:11, color:"#9ca3af" }}>Loading…</span>}
          <RRCustomInput value={btRrMode} onChange={setBtRrMode} disabled={btLoading}/>
        </div>

        {availableTimeframes.length > 0 && (
          <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:16, flexWrap:"wrap" }}>
            <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase" }}>Timeframe</span>
            <div style={{ display:"flex", gap:4, flexWrap:"wrap" }}>
              {["All", ...availableTimeframes].map(tf => (
                <button key={tf} onClick={()=>setBtTimeframeFilter(tf)} style={{
                  padding:"5px 14px", borderRadius:7, fontSize:12, fontWeight:700, cursor:"pointer",
                  border: btTimeframeFilter===tf ? "1.5px solid #6366f1" : "1px solid #e5e7eb",
                  background: btTimeframeFilter===tf ? "#eef2ff" : "#fff",
                  color: btTimeframeFilter===tf ? "#6366f1" : "#6b7280",
                }}>{tf}</button>
              ))}
            </div>
          </div>
        )}

        {btError ? (
          <div style={{ padding:48, textAlign:"center", color:"#dc2626", fontSize:14, background:"#fff", borderRadius:14, border:"1px solid #e5e7eb" }}>
            <div style={{ fontSize:22, marginBottom:8 }}>⚠</div>
            Could not load backtest stats: <code style={{ fontSize:12 }}>{btError}</code>
          </div>
        ) : (
          <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(220px,1fr))", gap:16 }}>
            <SummaryCard
              label="Won" badge="WIN" badgeBg="#bbf1d3" badgeColor="#166534"
              value={btStats?.won ?? "—"} valueColor="#16a34a" bg="#e7f8ef" Icon={CheckCircle2}
            />
            <SummaryCard
              label="Loss" badge="LOSS" badgeBg="#fbcfd1" badgeColor="#991b1b"
              value={btStats?.lost ?? "—"} valueColor="#dc2626" bg="#fdecec" Icon={XCircle}
            />
            <SummaryCard
              label="PnL ($)" badge={btStats && btStats.pnlDollar >= 0 ? "PROFIT" : "LOSS"}
              badgeBg={btStats && btStats.pnlDollar >= 0 ? "#bfe3fb" : "#fbcfd1"}
              badgeColor={btStats && btStats.pnlDollar >= 0 ? "#075985" : "#991b1b"}
              value={btStats ? `${btStats.pnlDollar >= 0 ? "+" : ""}$${btStats.pnlDollar.toFixed(2)}` : "—"}
              valueColor={btStats && btStats.pnlDollar >= 0 ? "#0891b2" : "#dc2626"} bg="#e6f6fd" Icon={DollarSign}
            />
            <SummaryCard
              label="Win Rate" badge={btStats && btStats.winRate >= 50 ? "GOOD" : "LOW"}
              badgeBg={btStats && btStats.winRate >= 50 ? "#bbf1d3" : "#fbcfd1"}
              badgeColor={btStats && btStats.winRate >= 50 ? "#166534" : "#991b1b"}
              value={btStats && btStats.winRate != null ? `${btStats.winRate.toFixed(1)}%` : "—"}
              valueColor={btStats && btStats.winRate >= 50 ? "#16a34a" : "#dc2626"} bg="#fdf1e2" Icon={Award}
            />
          </div>
        )}

        {/* All trades in this window */}
        {!btError && (
          <div style={{ marginTop:20, background:"#fff", borderRadius:14, border:"1px solid #e5e7eb", overflow:"hidden" }}>
            <div style={{ overflowX:"auto" }}>
              {btLoading ? (
                <div style={{ padding:60, textAlign:"center", color:"#9ca3af", fontSize:13 }}>Loading trades…</div>
              ) : sortedTrades.length === 0 ? (
                <div style={{ padding:60, textAlign:"center", color:"#9ca3af", fontSize:14 }}>
                  No signals in this window.
                </div>
              ) : (
                <table style={{ width:"100%", borderCollapse:"collapse", fontSize:12 }}>
                  <thead>
                    <tr>
                      {BCOLS.map(col => (
                        <TH key={col.k} right={col.r} onClick={()=>toggleSort(col.k)} sorted={sort.k===col.k} dir={sort.dir}>{col.l}</TH>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {sortedTrades.map((t, i) => {
                      const win    = t.result === "WIN";
                      const open   = t.result === "OPEN";
                      const forced = t.exitReason === "Closed by new signal";
                      const buy    = t.tradeSignal === "BUY";
                      return (
                        <tr key={i}
                          style={{ borderBottom:"1px solid #f3f4f6", background:i%2===0?"#fff":"#fafafa" }}
                          onMouseEnter={e=>e.currentTarget.style.background="#f5f3ff"}
                          onMouseLeave={e=>e.currentTarget.style.background=i%2===0?"#fff":"#fafafa"}
                        >
                          <td style={{ padding:"10px 14px", fontWeight:700 }}>
                            <span style={{ color:"#f59e0b" }}>{t.symbol.replace("/USDT","")}</span>
                            <span style={{ color:"#9ca3af", fontSize:10 }}>/USDT</span>
                          </td>
                          <td style={{ padding:"10px 14px", color:"#6b7280", fontSize:11 }}>{t.timeFrame}</td>
                          <td style={{ padding:"10px 14px" }}>
                            <span style={{
                              display:"inline-block", padding:"2px 8px", borderRadius:4, fontSize:11,
                              fontWeight:700, background:buy?"#dcfce7":"#fee2e2", color:buy?"#15803d":"#b91c1c"
                            }}>{t.tradeSignal}</span>
                          </td>
                          <td style={{ padding:"10px 14px", color:"#374151", whiteSpace:"nowrap", fontSize:11 }}>{t.signalTime || "—"}</td>
                          <td style={{ padding:"10px 14px", color:"#374151", whiteSpace:"nowrap", fontSize:11 }}>{t.entryTime}</td>
                          <td style={{ padding:"10px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", fontWeight:600, color:"#111827" }}>{fmtPrice(t.entryPrice)}</td>
                          <td style={{ padding:"10px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", color:"#dc2626" }}>{fmtPrice(t.stopLoss)}</td>
                          <td style={{ padding:"10px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", color:"#16a34a" }}>{fmtPrice(t.targetPrice)}</td>
                          <td style={{ padding:"10px 14px", color:"#374151", whiteSpace:"nowrap", fontSize:11 }}>
                            {open ? <span style={{ color:"#9ca3af" }}>Still running</span> : t.entryCloseTime}
                          </td>
                          <td style={{ padding:"10px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", fontWeight:500 }}>
                            {open ? <span style={{ color:"#9ca3af" }}>—</span> : fmtPrice(t.entryClose)}
                          </td>
                          <td style={{ padding:"10px 14px" }}>
                            <span style={{
                              fontSize:11, fontWeight:500,
                              color: open ? "#0891b2" : forced ? "#6366f1" : t.exitReason==="Target Hit" ? "#15803d" : "#b91c1c"
                            }}>{t.exitReason}</span>
                          </td>
                          <td style={{ padding:"10px 14px", color:"#6b7280", whiteSpace:"nowrap" }}>{t.duration}</td>
                          <td style={{ padding:"10px 14px", textAlign:"right", fontWeight:700, fontVariantNumeric:"tabular-nums",
                            color: open ? "#9ca3af" : t.gainPct>=0?"#16a34a":"#dc2626"
                          }}>
                            {open ? "—" : `${t.gainPct>=0?"+":""}${t.gainPct.toFixed(2)}%`}
                          </td>
                          <td style={{ padding:"10px 14px", textAlign:"right", fontWeight:600, fontVariantNumeric:"tabular-nums",
                            color: open ? "#9ca3af" : t.gainAmount>=0?"#16a34a":"#dc2626"
                          }}>
                            {open ? "—" : `${t.gainAmount>=0?"+":""}${fmtPrice(t.gainAmount)}`}
                          </td>
                          <td style={{ padding:"10px 14px", textAlign:"right", fontWeight:700, fontVariantNumeric:"tabular-nums",
                            color: open ? "#9ca3af" : t.gainDollar>=0?"#16a34a":"#dc2626"
                          }}>
                            {open ? "—" : `${t.gainDollar>=0?"+":""}$${t.gainDollar.toFixed(2)}`}
                          </td>
                          <td style={{ padding:"10px 14px" }}>
                            <span style={{
                              display:"inline-block", padding:"2px 8px", borderRadius:4, fontSize:11, fontWeight:700,
                              background: open ? "#e0f2fe" : forced ? "#ede9fe" : win ? "#dcfce7" : "#fee2e2",
                              color:      open ? "#0369a1" : forced ? "#6d28d9" : win ? "#15803d" : "#b91c1c"
                            }}>{t.result}</span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        )}
      </div>
      <style>{`
        .date-input-custom-icon::-webkit-calendar-picker-indicator{opacity:0;position:absolute;right:0;width:26px;height:100%;cursor:pointer;}
        .date-input-custom-icon::-webkit-inner-spin-button{display:none;}
      `}</style>
    </div>
  );
}

// ─── Details Page (coin details + full crossover history) ────────────────────
const ALL_TIMEFRAMES = ["1h", "2h", "4h", "6h"];
const CROSS_COLS = [
  {k:"signal_type", l:"Type"},
  {k:"interval",    l:"Timeframe"},
  {k:"cross_time",  l:"Signal Time"},
  {k:"cross_price", l:"Cross Price", r:true},
  {k:"result",      l:"Result"},
];

const BCOLS = [
  {k:"symbol",        l:"Symbol"},
  {k:"timeFrame",     l:"Timeframe"},
  {k:"tradeSignal",   l:"Signal Type"},
  {k:"_signalTimeMs", l:"Signal Time"},
  {k:"_entryTimeMs",  l:"Entry Time"},
  {k:"entryPrice",    l:"Entry Price",    r:true},
  {k:"stopLoss",      l:"Stop Loss",      r:true},
  {k:"targetPrice",   l:"Take Profit",    r:true},
  {k:"entryCloseTime",l:"Exit Time"},
  {k:"entryClose",    l:"Exit Price",     r:true},
  {k:"exitReason",    l:"Exit Reason"},
  {k:"duration",      l:"Duration"},
  {k:"gainPct",       l:"PnL %",          r:true},
  {k:"gainAmount",    l:"PnL Amount",     r:true},
  {k:"gainDollar",    l:"PnL ($)",        r:true},
  {k:"result",        l:"Result"},
];

const ResultBadge = ({ result }) => {
  if (result === "WIN")  return <span style={{ display:"inline-block", padding:"2px 8px", borderRadius:4, fontSize:11, fontWeight:700, background:"#dcfce7", color:"#15803d" }}>WIN</span>;
  if (result === "LOSS") return <span style={{ display:"inline-block", padding:"2px 8px", borderRadius:4, fontSize:11, fontWeight:700, background:"#fee2e2", color:"#b91c1c" }}>LOSS</span>;
  if (result === "OPEN") return <span style={{ display:"inline-block", padding:"2px 8px", borderRadius:4, fontSize:11, fontWeight:700, background:"#e0f2fe", color:"#0369a1" }}>OPEN</span>;
  return <span style={{ color:"#d1d5db" }}>—</span>;
};

const CROSS_HISTORY_DAYS = 30;

function DetailsPage({ row, market, onBack, onBacktest }) {
  const [signals, setSignals] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState(null);

  const symbol = row?.symbol || "";
  const { base } = fmtSym(symbol);

  const fetchSignals = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const perInterval = await Promise.all(ALL_TIMEFRAMES.map(async tf => {
        const res = await fetch(`${API_BASE}/signals?symbol=${symbol}&interval=${tf}&market=${market}&days=${CROSS_HISTORY_DAYS}&limit=500`);
        if (!res.ok) throw new Error(`API ${res.status}`);
        const data = await res.json();

        // The backend can end up with more than one stored row for the same
        // crossover (re-scans recomputing a slightly different interpolated
        // cross_time, sometimes minutes apart) — collapse by which candle the
        // signal actually falls into, since that's what determines its entry.
        const seen = new Map();
        for (const s of data) {
          const crossTimeMs = new Date(s.cross_time).getTime();
          const dedupeKey = `${s.signal_type}|${Math.floor(crossTimeMs / CANDLE_MS_MAP[tf])}`;
          if (!seen.has(dedupeKey)) seen.set(dedupeKey, { ...s, interval: tf, crossTimeMs });
        }
        const dedupedSignals = [...seen.values()];
        if (dedupedSignals.length === 0) return dedupedSignals;

        // Simulate each signal's SL/TP outcome (RR 1:2, same default as the
        // Backtest page) against candle data so we can show won/lost/open.
        const limit = LIMIT_MAP[tf] || 1500;
        const candleRes = await fetch(`${API_BASE}/candles/${symbol}?interval=${tf}&market=${market}&limit=${limit}`);
        if (!candleRes.ok) return dedupedSignals.map(s => ({ ...s, result: null }));
        const rawCandles = await candleRes.json();
        const candles = rawCandles.map(r => ({
          symbol, openTimeMs: r[0],
          open: parseFloat(r[1]), high: parseFloat(r[2]), low: parseFloat(r[3]), close: parseFloat(r[4]),
        }));
        const simSignals = [...dedupedSignals]
          .sort((a, b) => a.crossTimeMs - b.crossTimeMs)
          .map(s => ({ type: s.signal_type, crossPrice: s.cross_price, crossTimeMs: s.crossTimeMs }));
        // windowDays is set far larger than any signal age so nothing gets filtered out here —
        // the only real limit is how far back the fetched candles reach.
        const trades = runBacktestFromSignals(candles, simSignals, "1:2", 3650, CANDLE_MS_MAP[tf], tf);
        const resultByKey = new Map(trades.map(t => [`${t.tradeSignal}|${t.signalTime}`, t.result]));

        return dedupedSignals.map(s => ({
          ...s,
          result: resultByKey.get(`${s.signal_type}|${fmtDateTime(s.crossTimeMs)}`) ?? null,
        }));
      }));

      const merged = perInterval.flat().sort((a, b) => b.crossTimeMs - a.crossTimeMs);
      setSignals(merged);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [symbol, market]);

  useEffect(() => { fetchSignals(); }, [fetchSignals]);

  // Auto-refresh every 20s so an OPEN crossover picks up a live SL/TP hit
  // (fresh candle data) without needing to leave and re-open this page.
  useEffect(() => {
    const id = setInterval(fetchSignals, 20000);
    return () => clearInterval(id);
  }, [fetchSignals]);

  return (
    <div style={{ fontFamily:"'Inter',system-ui,sans-serif", background:"#fff", minHeight:"100vh", width:"100%", color:"#111827" }}>
      {/* Header */}
      <div style={{ display:"flex", alignItems:"center", gap:10, padding:"13px 24px", borderBottom:"1px solid #e8eaed", flexWrap:"wrap" }}>
        <button onClick={onBack} style={{
          display:"flex", alignItems:"center", gap:5, padding:"5px 12px",
          borderRadius:7, border:"1px solid #e5e7eb", background:"transparent",
          fontSize:12, fontWeight:600, color:"#374151", cursor:"pointer",
        }}>← Back</button>
        <span style={{ color:"#d1d5db", fontSize:14 }}>›</span>
        <span style={{ fontWeight:700, color:"#f59e0b", fontSize:15 }}>{base}</span>
        <span style={{ color:"#9ca3af", fontSize:14, fontWeight:400 }}>/USDT</span>
        <Trend t={row?.ema_trend}/>
      </div>

      {/* Detail cards */}
      <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(140px,1fr))", gap:12, padding:"16px 24px", borderBottom:"1px solid #e8eaed" }}>
        {[
          {l:"Score",        v:<ScoreBadge v={row?.score}/>},
          {l:"Price",        v:`$${fmtPrice(row?.price)}`},
          {l:"1H Change",    v:<ChgCell v={row?.change_1h ?? 0}/>},
          {l:"24H Change",   v:<ChgCell v={row?.change_24h ?? 0}/>},
          {l:"Volume (24H)", v:fmtVol(row?.volume_24h)},
          {l:"Last Signal",  v:<SigBadge s={row?.last_signal}/>},
          {l:"Cross Price",  v: row?.cross_price ? `$${fmtPrice(row.cross_price)}` : "—"},
          {l:"Signal Time",  v: row?.signal_time ? fmtTime(row.signal_time) : "—"},
        ].map(item => (
          <div key={item.l} style={{ background:"#f9fafb", borderRadius:8, padding:"10px 14px" }}>
            <div style={{ fontSize:11, color:"#9ca3af", fontWeight:600, textTransform:"uppercase", letterSpacing:"0.06em", marginBottom:4 }}>{item.l}</div>
            <div style={{ fontSize:14, fontWeight:500, color:"#111827" }}>{item.v}</div>
          </div>
        ))}
      </div>

      {/* Crossover history */}
      <div style={{ padding:"14px 24px", display:"flex", alignItems:"baseline", gap:8, flexWrap:"wrap" }}>
        <span style={{ fontSize:13, fontWeight:700 }}>All Crossovers</span>
        <span style={{ fontSize:12, color:"#9ca3af" }}>Showing last {CROSS_HISTORY_DAYS} days</span>
        <button onClick={() => onBacktest(row)} style={{
          marginLeft:"auto", padding:"6px 16px", borderRadius:8, border:"1px solid #6366f1",
          background:"transparent", color:"#6366f1", fontSize:12, fontWeight:700, cursor:"pointer",
        }}>Backtest</button>
      </div>

      <div style={{ overflowX:"auto" }}>
        {error ? (
          <div style={{ padding:48, textAlign:"center", color:"#dc2626", fontSize:14 }}>
            <div style={{ fontSize:22, marginBottom:8 }}>⚠</div>
            Could not load crossovers: <code style={{ fontSize:12 }}>{error}</code>
          </div>
        ) : loading ? (
          <div style={{ padding:60, textAlign:"center", color:"#9ca3af", fontSize:13 }}>Loading crossover history…</div>
        ) : !signals || signals.length === 0 ? (
          <div style={{ padding:60, textAlign:"center", color:"#9ca3af", fontSize:14 }}>
            No crossovers found for {base}/USDT.
          </div>
        ) : (
          <table style={{ width:"100%", borderCollapse:"collapse", fontSize:13 }}>
            <thead>
              <tr>
                {CROSS_COLS.map(col => (
                  <TH key={col.k} right={col.r}>{col.l}</TH>
                ))}
              </tr>
            </thead>
            <tbody>
              {signals.map((s, i) => (
                <tr key={i} style={{ borderBottom:"1px solid #f3f4f6", background:i%2===0?"#fff":"#fafafa" }}>
                  <td style={{ padding:"10px 14px" }}><SigBadge s={s.signal_type}/></td>
                  <td style={{ padding:"10px 14px", color:"#6b7280", fontWeight:600 }}>{s.interval.toUpperCase()}</td>
                  <td style={{ padding:"10px 14px", color:"#374151", whiteSpace:"nowrap" }}>{fmtTime(s.cross_time)}</td>
                  <td style={{ padding:"10px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums" }}>{fmtPrice(s.cross_price)}</td>
                  <td style={{ padding:"10px 14px" }}><ResultBadge result={s.result}/></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ─── Backtest Page ────────────────────────────────────────────────────────────
const RR_MODES   = ["1:2", "2:4"];
const WIN_DAYS   = [7, 14, 30];
const TIMEFRAMES = ["1h", "2h", "4h", "6h"];
const CANDLE_MS_MAP = {"1h":3_600_000,"2h":7_200_000,"4h":14_400_000,"6h":21_600_000};
const LIMIT_MAP     = {"1h":1500,"2h":900,"4h":570,"6h":450};

// Generic Binance-style interval -> milliseconds (5m/15m/30m/1h/4h/1d/...),
// unlike CANDLE_MS_MAP above which only covers the old scanner's fixed set —
// CSV-imported signals can be on any interval, so backtest pages that read
// CSV data need this instead.
function intervalToMs(interval) {
  const m = /^(\d+)([mhdwM])$/.exec(interval || "");
  if (!m) return 3_600_000;
  const unitMs = { m: 60_000, h: 3_600_000, d: 86_400_000, w: 604_800_000, M: 2_592_000_000 };
  return parseInt(m[1], 10) * unitMs[m[2]];
}

function StatCard({ label, value, color }) {
  return (
    <div style={{ background:"#f8f9fb", borderRadius:10, padding:"14px 18px", border:"1px solid #e8eaed", minWidth:110 }}>
      <div style={{ fontSize:10, color:"#9ca3af", fontWeight:700, letterSpacing:"0.07em", textTransform:"uppercase", marginBottom:6 }}>{label}</div>
      <div style={{ fontSize:22, fontWeight:700, color: color||"#111827" }}>{value}</div>
    </div>
  );
}

function BacktestPage({ scanRow, onBack }) {
  const [rrMode, setRrMode]       = useState("1:2");
  const [capital, setCapital]     = useState(1000);
  const [window, setWindow]       = useState(7);
  const [timeframe, setTimeframe] = useState(scanRow?.interval || "1h");
  const [candles, setCandles]     = useState(null);
  const [multiCandles, setMultiCandles] = useState(null); // Map<interval, candles[]> — only populated when timeframe === "All"
  const [dbSignals, setDbSignals] = useState(null);  // signals fetched from DB
  const [trades, setTrades]       = useState(null);
  const [loading, setLoading]     = useState(true);
  const [error, setError]         = useState(null);
  const [reloading, setReloading] = useState(false);
  const [sort, setSort]           = useState({ k:"_entryTimeMs", dir:"desc" });
  const [symbolSignals, setSymbolSignals] = useState([]); // every CSV signal for this symbol, any interval

  const symbol = scanRow?.symbol || "BTCUSDT";
  const { base } = fmtSym(symbol);
  const fetchIdRef = useRef(0);

  // A CSV can carry signals for the same coin at several different
  // timeframes (5m/15m/1h/...) — load them all once so we know exactly
  // which timeframes exist for THIS coin, instead of assuming a fixed set.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/csv-signals`);
        if (!res.ok || cancelled) return;
        const all = await res.json();
        if (cancelled) return;
        const forSymbol = all.filter(s => s.symbol === symbol);
        setSymbolSignals(forSymbol);
        const intervals = [...new Set(forSymbol.map(s => s.interval))].sort();
        setTimeframe(t => {
          if (intervals.length === 0) return t;
          if (intervals.includes(t)) return t;
          return intervals.includes(scanRow?.interval) ? scanRow.interval : intervals[0];
        });
      } catch {}
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol]);

  // Memoized so its array identity only changes when symbolSignals actually
  // does — it's a dependency of the fetchData useCallback below, and a
  // fresh array reference on every render there caused an infinite
  // fetch loop (fetchData's identity would change every render, re-running
  // the effect that calls it, which triggers the render that recomputes
  // this again, forever — matches the repeating identical requests seen).
  const availableIntervals = useMemo(
    () => [...new Set(symbolSignals.map(s => s.interval))].sort(),
    [symbolSignals]
  );

  // Fetches ONE interval's candles for `symbol` — ensuring deeper history
  // from Binance first if the selected Window needs more than the ~1000
  // candles the bulk CSV import stores per coin (see ensure-depth docstring
  // in csv_import.py). Shared by both the single-timeframe path and the
  // "All" path below, which just calls this once per available interval.
  const fetchCandlesFor = useCallback(async (iv, requestId) => {
    const neededCandles = Math.ceil((window * 86_400_000) / intervalToMs(iv)) + 50;
    if (neededCandles > 1000) {
      try {
        await fetch(`${API_BASE}/csv-candles/${symbol}/ensure-depth?interval=${iv}&days=${window}`, { method: "POST" });
      } catch {
        // Non-fatal — fall through and show whatever history is already stored.
      }
      if (fetchIdRef.current !== requestId) return null;
    }
    const candleLimit = Math.min(20_000, Math.max(1000, neededCandles));
    const res = await fetch(`${API_BASE}/csv-candles/${symbol}?interval=${iv}&limit=${candleLimit}`);
    if (!res.ok) {
      if (res.status === 404) return []; // no stored candles for this interval yet — not fatal for the "All" view
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail?.detail || `Candles API ${res.status}`);
    }
    const rawCandles = await res.json();
    return rawCandles.map(r => ({
      symbol, openTimeMs: r[0],
      open: parseFloat(r[1]), high: parseFloat(r[2]), low: parseFloat(r[3]),
      close: parseFloat(r[4]), volume: parseFloat(r[5]),
    }));
  }, [symbol, window]);

  // Fetch this coin's candles for the selected timeframe (or, if "All" is
  // selected, every timeframe this coin has signals for) from the CSV store.
  const fetchData = useCallback(async () => {
    // Guards against out-of-order responses: if the symbol/timeframe changes
    // again before this request lands, a slower older request must not be
    // allowed to clobber the newer one's state once it resolves.
    const requestId = ++fetchIdRef.current;
    setReloading(true);
    setError(null);
    try {
      if (timeframe === "All") {
        const map = new Map();
        for (const iv of availableIntervals) {
          const parsedCandles = await fetchCandlesFor(iv, requestId);
          if (fetchIdRef.current !== requestId) return;
          if (parsedCandles) map.set(iv, parsedCandles);
        }
        if (fetchIdRef.current !== requestId) return;
        setMultiCandles(map);
        setCandles(null);
        setDbSignals(null);
        return;
      }

      const parsedCandles = await fetchCandlesFor(timeframe, requestId);
      if (fetchIdRef.current !== requestId) return;

      // The CSV can carry more than one stored row for the same crossover
      // (re-uploads, slightly different interpolated cross_time) — collapse
      // by which candle the signal actually falls into, since that's what
      // determines its entry.
      const candleMs = intervalToMs(timeframe);
      const seenSignals = new Map();
      for (const s of symbolSignals) {
        if (s.interval !== timeframe) continue;
        const crossTimeMs = new Date(s.cross_time).getTime();
        const dedupeKey = `${s.signal_type}|${Math.floor(crossTimeMs / candleMs)}`;
        if (!seenSignals.has(dedupeKey)) {
          seenSignals.set(dedupeKey, {
            type:        s.signal_type,           // "BUY" | "SELL"
            crossPrice:  s.cross_price,
            crossTimeMs,
            ema7:  s.ema_fast, ema25: s.ema_mid, ema99: s.ema_slow,
          });
        }
      }
      // Sort oldest → newest for simulation walkthrough
      const parsedSignals = [...seenSignals.values()].sort((a, b) => a.crossTimeMs - b.crossTimeMs);

      if (fetchIdRef.current !== requestId) return; // a newer request has since superseded this one
      setMultiCandles(null);
      setCandles(parsedCandles);
      setDbSignals(parsedSignals);
    } catch(e) {
      if (fetchIdRef.current === requestId) setError(e.message);
    } finally {
      if (fetchIdRef.current === requestId) {
        setLoading(false);
        setReloading(false);
      }
    }
  }, [symbol, timeframe, symbolSignals, window, availableIntervals, fetchCandlesFor]);

  useEffect(() => {
    setLoading(true);
    setCandles(null);
    setMultiCandles(null);
    setDbSignals(null);
    setTrades(null);
    fetchData();
  }, [fetchData]);

  // Re-run simulation whenever candles, signals, rrMode, or window changes.
  useEffect(() => {
    if (timeframe === "All") {
      if (!multiCandles) return;
      const merged = [];
      for (const iv of availableIntervals) {
        const ivCandles = multiCandles.get(iv);
        if (!ivCandles || ivCandles.length === 0) continue;
        const candleMs = intervalToMs(iv);
        const seenSignals = new Map();
        for (const s of symbolSignals) {
          if (s.interval !== iv) continue;
          const crossTimeMs = new Date(s.cross_time).getTime();
          const dedupeKey = `${s.signal_type}|${Math.floor(crossTimeMs / candleMs)}`;
          if (!seenSignals.has(dedupeKey)) {
            seenSignals.set(dedupeKey, {
              type: s.signal_type, crossPrice: s.cross_price, crossTimeMs,
              ema7: s.ema_fast, ema25: s.ema_mid, ema99: s.ema_slow,
            });
          }
        }
        const ivSignals = [...seenSignals.values()].sort((a, b) => a.crossTimeMs - b.crossTimeMs);
        if (ivSignals.length === 0) continue;
        merged.push(...runBacktestFromSignals(ivCandles, ivSignals, rrMode, window, candleMs, iv, capital));
      }
      setTrades(merged);
    } else if (candles && dbSignals) {
      setTrades(runBacktestFromSignals(candles, dbSignals, rrMode, window, intervalToMs(timeframe), timeframe, capital));
    }
  }, [candles, dbSignals, multiCandles, rrMode, window, timeframe, capital, availableIntervals, symbolSignals]);

  const sortedTrades = trades ? [...trades].sort((a,b)=>{
    const dir = sort.dir==="asc"?1:-1;
    const av=a[sort.k], bv=b[sort.k];
    if (av==null&&bv==null) return 0;
    if (av==null) return 1; if (bv==null) return -1;
    return typeof av==="string"?dir*av.localeCompare(bv):dir*(av-bv);
  }) : [];

  // Stats: exclude OPEN and SKIP from wins/losses/P&L — only closed real trades count
  const closedTrades   = trades ? trades.filter(t => t.result === "WIN" || t.result === "LOSS") : [];
  const openTrades     = trades ? trades.filter(t => t.result === "OPEN") : [];
  const wins           = closedTrades.filter(t=>t.result==="WIN").length;
  const losses         = closedTrades.filter(t=>t.result==="LOSS").length;
  const closedCount    = closedTrades.length;
  const winRate        = closedCount > 0 ? ((wins/closedCount)*100).toFixed(1) : null;
  const totalPnl       = closedTrades.reduce((sum,t)=>sum+t.gainPct,0);
  const openCount      = openTrades.length;
  const allTradesCount = trades ? trades.length : 0;

  const toggleSort = k => setSort(s => s.k===k?{k,dir:s.dir==="asc"?"desc":"asc"}:{k,dir:"desc"});

  return (
    <div style={{ fontFamily:"'Inter',system-ui,sans-serif", background:"#fff", minHeight:"100vh", width:"100%", color:"#111827" }}>

      {/* Header */}
      <div style={{ display:"flex", alignItems:"center", gap:10, padding:"13px 24px", borderBottom:"1px solid #e8eaed", background:"#fff", flexWrap:"wrap" }}>
        <button onClick={onBack} style={{
          display:"flex", alignItems:"center", gap:5, padding:"5px 12px",
          borderRadius:7, border:"1px solid #e5e7eb", background:"transparent",
          fontSize:12, fontWeight:600, color:"#374151", cursor:"pointer",
        }}>← Back</button>
        <span style={{ color:"#d1d5db", fontSize:14 }}>›</span>
        <span style={{ fontWeight:700, color:"#f59e0b", fontSize:15 }}>{base}</span>
        <span style={{ color:"#9ca3af", fontSize:14, fontWeight:400 }}>/USDT</span>
        <span style={{ color:"#9ca3af", fontSize:13 }}>Backtest · Last {window} days · IST · EMA 7/25/99 · {timeframe.toUpperCase()} · Next candle open entry</span>
        <div style={{ marginLeft:"auto", display:"flex", alignItems:"center", gap:12 }}>
          <button onClick={fetchData} disabled={reloading} style={{
            display:"flex", alignItems:"center", gap:5,
            padding:"5px 14px", borderRadius:7, border:"1px solid #e5e7eb",
            background:"transparent", fontSize:12, fontWeight:600,
            color: reloading?"#9ca3af":"#374151", cursor:"pointer",
          }}>↻ {reloading?"Loading…":"Reload"}</button>
        </div>
      </div>

      {/* Controls */}
      <div style={{ padding:"12px 24px", borderBottom:"1px solid #e8eaed", display:"flex", alignItems:"center", gap:16, flexWrap:"wrap" }}>
        <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase" }}>Risk : Reward</span>
        {RR_MODES.map(m => (
          <button key={m} onClick={()=>setRrMode(m)} style={{
            padding:"4px 14px", borderRadius:6, fontSize:12, fontWeight:600, cursor:"pointer",
            border:rrMode===m?"1.5px solid #f59e0b":"1px solid #e5e7eb",
            background:"transparent", color:rrMode===m?"#f59e0b":"#6b7280",
          }}>RR {m}</button>
        ))}
        {SHOW_SWING_BUTTON && (
          <button onClick={()=>setRrMode("swing")} title="SL = just past the nearest swing low/high before entry, TP = same distance" style={{
            padding:"4px 14px", borderRadius:6, fontSize:12, fontWeight:600, cursor:"pointer",
            border:rrMode==="swing"?"1.5px solid #f59e0b":"1px solid #e5e7eb",
            background:"transparent", color:rrMode==="swing"?"#f59e0b":"#6b7280",
          }}>Swing SL/TP</button>
        )}
        <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase", marginLeft:8 }}>Capital</span>
        <div style={{ display:"flex", alignItems:"center", gap:4 }}>
          <span style={{ color:"#9ca3af", fontSize:12 }}>$</span>
          <input
            type="number" min="1" step="100" defaultValue={capital}
            onBlur={e => { const v = parseFloat(e.target.value); if (v > 0) setCapital(v); }}
            onKeyDown={e => { if (e.key === "Enter") e.target.blur(); }}
            style={{ width:80, padding:"4px 6px", borderRadius:6, border:"1px solid #e5e7eb", fontSize:12 }}
          />
        </div>
        <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase", marginLeft:8 }}>Window</span>
        {WIN_DAYS.map(d => (
          <button key={d} onClick={()=>setWindow(d)} style={{
            padding:"4px 14px", borderRadius:6, fontSize:12, fontWeight:600, cursor:"pointer",
            border:window===d?"1.5px solid #f59e0b":"1px solid #e5e7eb",
            background:"transparent", color:window===d?"#f59e0b":"#6b7280",
          }}>{d}d</button>
        ))}
        <div style={{ width:1, height:20, background:"#e5e7eb", marginLeft:8 }}/>
        <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase" }}>Timeframe</span>
        {availableIntervals.length > 1 && (
          <button onClick={()=>setTimeframe("All")} style={{
            padding:"4px 14px", borderRadius:6, fontSize:12, fontWeight:600, cursor:"pointer",
            border:timeframe==="All"?"1.5px solid #6366f1":"1px solid #e5e7eb",
            background:"transparent", color:timeframe==="All"?"#6366f1":"#6b7280",
            transition:"all 0.15s",
          }}>All</button>
        )}
        {(availableIntervals.length > 0 ? availableIntervals : [timeframe]).map(tf => (
          <button key={tf} onClick={()=>setTimeframe(tf)} style={{
            padding:"4px 14px", borderRadius:6, fontSize:12, fontWeight:600, cursor:"pointer",
            border:timeframe===tf?"1.5px solid #6366f1":"1px solid #e5e7eb",
            background:"transparent", color:timeframe===tf?"#6366f1":"#6b7280",
            transition:"all 0.15s",
          }}>{tf.toUpperCase()}</button>
        ))}
        <RRCustomInput value={rrMode} onChange={setRrMode}/>
      </div>

      {/* Stat cards */}
      {!loading && !error && trades && (
        <div style={{ display:"grid", gridTemplateColumns:"repeat(8,1fr)", gap:12, padding:"16px 24px", borderBottom:"1px solid #e8eaed" }}>
          <StatCard label="Total Trades" value={allTradesCount}/>
          <StatCard label="Open"         value={openCount} color="#0891b2"/>
          <StatCard label="Wins"         value={wins}   color="#16a34a"/>
          <StatCard label="Losses"       value={losses} color="#dc2626"/>
          <StatCard label="Win Rate"     value={winRate ? `${winRate}%` : "—"} color="#f59e0b"/>
          <StatCard label="Total P&L"    value={closedCount>0?`${totalPnl>=0?"+":""}${totalPnl.toFixed(2)}%`:"—"} color={totalPnl>=0?"#16a34a":"#dc2626"}/>
          <StatCard label="RR Mode"      value={rrMode === "swing" ? "Swing SL/TP" : rrMode} color="#6366f1"/>
          <StatCard label="Timeframe"    value={timeframe.toUpperCase()} color="#0891b2"/>
        </div>
      )}

      {/* Table */}
      <div style={{ overflowX:"auto" }}>
        {error ? (
          <div style={{ padding:48, textAlign:"center", color:"#dc2626", fontSize:14 }}>
            <div style={{ fontSize:22, marginBottom:8 }}>⚠</div>
            Failed to fetch candle data: <code style={{ fontSize:12 }}>{error}</code>
            <div style={{ marginTop:6, color:"#9ca3af", fontSize:12 }}>Check your backend server is running at {API_BASE}</div>
          </div>
        ) : loading ? (
          <div style={{ padding:60, textAlign:"center", color:"#9ca3af", fontSize:13 }}>Loading candle data from database…</div>
        ) : sortedTrades.length === 0 ? (
          <div style={{ padding:60, textAlign:"center", color:"#9ca3af", fontSize:14 }}>
            <div style={{ fontSize:24, marginBottom:8 }}>📊</div>
            No completed trades in last {window} days for {base}/USDT.
            <div style={{ marginTop:6, fontSize:12 }}>Try extending the window or adjusting the RR mode.</div>
          </div>
        ) : (
          <table style={{ width:"100%", borderCollapse:"collapse", fontSize:12 }}>
            <thead>
              <tr>
                {BCOLS.map(col => (
                  <TH key={col.k} right={col.r} onClick={()=>toggleSort(col.k)} sorted={sort.k===col.k} dir={sort.dir}>{col.l}</TH>
                ))}
              </tr>
            </thead>
            <tbody>
              {sortedTrades.map((t, i) => {
                const win     = t.result === "WIN";
                const open    = t.result === "OPEN";
                const forced  = t.exitReason === "Closed by new signal";
                const buy     = t.tradeSignal === "BUY";
                return (
                  <tr key={i}
                    style={{ borderBottom:"1px solid #f3f4f6", background:i%2===0?"#fff":"#fafafa" }}
                    onMouseEnter={e=>e.currentTarget.style.background="#f5f3ff"}
                    onMouseLeave={e=>e.currentTarget.style.background=i%2===0?"#fff":"#fafafa"}
                  >
                    {/* Symbol */}
                    <td style={{ padding:"10px 14px", fontWeight:700 }}>
                      <span style={{ color:"#f59e0b" }}>{t.symbol.replace("/USDT","")}</span>
                      <span style={{ color:"#9ca3af", fontSize:10 }}>/USDT</span>
                    </td>
                    {/* Timeframe */}
                    <td style={{ padding:"10px 14px", color:"#6b7280", fontSize:11 }}>{t.timeFrame}</td>
                    {/* Signal Type */}
                    <td style={{ padding:"10px 14px" }}>
                      <span style={{
                        display:"inline-block", padding:"2px 8px", borderRadius:4, fontSize:11,
                        fontWeight:700, background:buy?"#dcfce7":"#fee2e2", color:buy?"#15803d":"#b91c1c"
                      }}>{t.tradeSignal}</span>
                    </td>
                    {/* Signal Time */}
                    <td style={{ padding:"10px 14px", color:"#374151", whiteSpace:"nowrap", fontSize:11 }}>{t.signalTime || "—"}</td>
                    {/* Entry Time */}
                    <td style={{ padding:"10px 14px", color:"#374151", whiteSpace:"nowrap", fontSize:11 }}>{t.entryTime}</td>
                    {/* Entry Price */}
                    <td style={{ padding:"10px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", fontWeight:600, color:"#111827" }}>{fmtPrice(t.entryPrice)}</td>
                    {/* Stop Loss */}
                    <td style={{ padding:"10px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", color:"#dc2626" }}>{fmtPrice(t.stopLoss)}</td>
                    {/* Take Profit */}
                    <td style={{ padding:"10px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", color:"#16a34a" }}>{fmtPrice(t.targetPrice)}</td>
                    {/* Exit Time */}
                    <td style={{ padding:"10px 14px", color:"#374151", whiteSpace:"nowrap", fontSize:11 }}>
                      {open ? <span style={{ color:"#9ca3af" }}>Still running</span> : t.entryCloseTime}
                    </td>
                    {/* Exit Price */}
                    <td style={{ padding:"10px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", fontWeight:500 }}>
                      {open ? <span style={{ color:"#9ca3af" }}>—</span> : fmtPrice(t.entryClose)}
                    </td>
                    {/* Exit Reason */}
                    <td style={{ padding:"10px 14px" }}>
                      <span style={{
                        fontSize:11, fontWeight:500,
                        color: open ? "#0891b2" : forced ? "#6366f1" : t.exitReason==="Target Hit" ? "#15803d" : "#b91c1c"
                      }}>{t.exitReason}</span>
                    </td>
                    {/* Duration */}
                    <td style={{ padding:"10px 14px", color:"#6b7280", whiteSpace:"nowrap" }}>{t.duration}</td>
                    {/* PnL % */}
                    <td style={{ padding:"10px 14px", textAlign:"right", fontWeight:700, fontVariantNumeric:"tabular-nums",
                      color: open ? "#9ca3af" : t.gainPct>=0?"#16a34a":"#dc2626"
                    }}>
                      {open ? "—" : `${t.gainPct>=0?"+":""}${t.gainPct.toFixed(2)}%`}
                    </td>
                    {/* PnL Amount */}
                    <td style={{ padding:"10px 14px", textAlign:"right", fontWeight:600, fontVariantNumeric:"tabular-nums",
                      color: open ? "#9ca3af" : t.gainAmount>=0?"#16a34a":"#dc2626"
                    }}>
                      {open ? "—" : `${t.gainAmount>=0?"+":""}${fmtPrice(t.gainAmount)}`}
                    </td>
                    {/* PnL ($) */}
                    <td style={{ padding:"10px 14px", textAlign:"right", fontWeight:700, fontVariantNumeric:"tabular-nums",
                      color: open ? "#9ca3af" : t.gainDollar>=0?"#16a34a":"#dc2626"
                    }}>
                      {open ? "—" : `${t.gainDollar>=0?"+":""}$${t.gainDollar.toFixed(2)}`}
                    </td>
                    {/* Result */}
                    <td style={{ padding:"10px 14px" }}>
                      <span style={{
                        display:"inline-block", padding:"2px 8px", borderRadius:4, fontSize:11, fontWeight:700,
                        background: open ? "#e0f2fe" : forced ? "#ede9fe" : win ? "#dcfce7" : "#fee2e2",
                        color:      open ? "#0369a1" : forced ? "#6d28d9" : win ? "#15803d" : "#b91c1c"
                      }}>{t.result}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
      `}</style>
    </div>
  );
}

// ─── Home Page (coin universe — stored/Bullish/Bearish counts + full list) ───
// Landing page: shows every coin the Universe Collector has stored candles
// for (see universe_collector.py / universe_summary.py on the backend),
// each tagged with its current EMA 7/25/99 trend, computed purely from
// stored DB data — separate from the CSV-driven Scanner/EMA Crossover page.
const HOME_PAGE_SIZE = 50;
const HOME_COLS = [
  {k:"rank",         l:"#",         s:false},
  {k:"symbol",       l:"Symbol",    s:true},
  {k:"trend",        l:"EMA Trend", s:true},
  {k:"price",        l:"Price",     s:true, r:true},
  {k:"candle_count", l:"Candles",   s:true, r:true},
];

function HomePage({ onOpenScanner, onOpenSwing }) {
  const [summary, setSummary]       = useState(null);
  const [loading, setLoading]       = useState(true);
  const [error, setError]           = useState(null);
  const [collecting, setCollecting] = useState(false);
  const [collectMsg, setCollectMsg] = useState(null);
  const [search, setSearch]         = useState("");
  const [trendFilter, setTrendFilter] = useState("All");
  const [sort, setSort]             = useState({ k:"symbol", dir:"asc" });
  const [homePage, setHomePage]     = useState(1);

  const loadSummary = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/universe-summary`);
      if (!res.ok) throw new Error(`API ${res.status}`);
      setSummary(await res.json());
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadSummary(); }, [loadSummary]);

  // Kicks off the backend's Binance scan + candle backfill for every coin
  // with 24h volume >= $1M, then reloads the summary to reflect what got
  // stored — this can take a while for hundreds of coins.
  const handleCollect = async () => {
    setCollecting(true);
    setCollectMsg(null);
    try {
      const res = await fetch(`${API_BASE}/collect-universe`, { method: "POST" });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail?.detail || `API ${res.status}`);
      }
      const result = await res.json();
      setCollectMsg(
        `Stored ${result.pairs_stored}/${result.pairs_attempted} coin×timeframe pair(s) across ` +
        `${result.symbols_scanned} coin(s) (${result.intervals.join(", ")})` +
        (result.errors?.length ? ` — ${result.errors.length} failed.` : ".")
      );
      loadSummary();
    } catch (e) {
      setCollectMsg(`Collect failed: ${e.message}`);
    } finally {
      setCollecting(false);
    }
  };

  const coins = summary?.coins || [];
  const toggleSort = k => setSort(s => s.k===k ? {k, dir:s.dir==="asc"?"desc":"asc"} : {k, dir:"asc"});

  const filteredCoins = useMemo(() => coins.filter(c => {
    if (trendFilter !== "All" && (c.trend || "Insufficient Data") !== trendFilter) return false;
    if (search && !c.symbol.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  }), [coins, trendFilter, search]);

  const sortedCoins = useMemo(() => [...filteredCoins].sort((a, b) => {
    const dir = sort.dir === "asc" ? 1 : -1;
    const av = a[sort.k], bv = b[sort.k];
    if (av == null && bv == null) return 0;
    if (av == null) return 1; if (bv == null) return -1;
    return typeof av === "string" ? dir*av.localeCompare(bv) : dir*(av-bv);
  }), [filteredCoins, sort]);

  const homePageCount = Math.max(1, Math.ceil(sortedCoins.length / HOME_PAGE_SIZE));
  useEffect(() => {
    if (homePage > homePageCount) setHomePage(homePageCount);
  }, [homePage, homePageCount]);
  useEffect(() => { setHomePage(1); }, [trendFilter, search, sort]);
  const homePageRows = useMemo(() => {
    const start = (homePage - 1) * HOME_PAGE_SIZE;
    return sortedCoins.slice(start, start + HOME_PAGE_SIZE);
  }, [sortedCoins, homePage]);

  return (
    <div style={{ fontFamily:"'Inter',system-ui,sans-serif", background:"#f5f6f8", minHeight:"100vh", width:"100%", color:"#111827" }}>
      {/* Header */}
      <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", padding:"20px 24px 16px", flexWrap:"wrap", gap:12 }}>
        <div>
          <div style={{ fontSize:26, fontWeight:800, letterSpacing:"-0.02em" }}>COIN UNIVERSE</div>
          <div style={{ fontSize:12, color:"#9ca3af", fontWeight:600, marginTop:2, letterSpacing:"0.02em" }}>
            ALL STORED COINS
          </div>
        </div>
        <div style={{ display:"flex", alignItems:"center", gap:10, flexWrap:"wrap" }}>
          <button onClick={handleCollect} disabled={collecting} style={{
            padding:"7px 16px", borderRadius:8, border:"1px solid #e5e7eb",
            background:"#fff", color:"#374151", fontSize:12, fontWeight:700,
            cursor: collecting ? "not-allowed" : "pointer", opacity: collecting ? 0.6 : 1,
          }}>{collecting ? "Collecting…" : "↻ Collect Universe"}</button>
          <button onClick={onOpenScanner} style={{
            padding:"8px 18px", borderRadius:8, border:"none",
            background:"#6366f1", color:"#fff", fontSize:13, fontWeight:700, cursor:"pointer",
          }}>EMA Crossover →</button>
          <button onClick={onOpenSwing} style={{
            padding:"8px 18px", borderRadius:8, border:"none",
            background:"#0891b2", color:"#fff", fontSize:13, fontWeight:700, cursor:"pointer",
          }}>Swing Strategy →</button>
        </div>
      </div>

      {collectMsg && (
        <div style={{ margin:"0 24px 16px", background:"#fff", borderRadius:14, border:"1px solid #e5e7eb", padding:"12px 20px", fontSize:12, color:"#374151" }}>
          {collectMsg}
        </div>
      )}

      {loading ? (
        <div style={{ padding:60, textAlign:"center", color:"#9ca3af", fontSize:13 }}>Loading coin universe…</div>
      ) : error ? (
        <div style={{ margin:"0 24px 16px", padding:24, textAlign:"center", color:"#dc2626", fontSize:13, background:"#fff", borderRadius:14, border:"1px solid #e5e7eb" }}>⚠ {error}</div>
      ) : coins.length === 0 ? (
        <div style={{ margin:"0 24px 16px", padding:60, textAlign:"center", color:"#9ca3af", fontSize:13, background:"#fff", borderRadius:14, border:"1px solid #e5e7eb" }}>
          No coins stored yet — click "Collect Universe" to fetch and store every coin with 24h volume ≥ $1M.
        </div>
      ) : (
        <>
          {/* Summary cards */}
          <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(220px,1fr))", gap:16, padding:"0 24px 16px" }}>
            <SummaryCard
              label="Stored" badge={`${summary.market.toUpperCase()} ${summary.interval.toUpperCase()}`} badgeBg="#111827" badgeColor="#fff"
              value={summary.symbols_stored} valueColor="#111827" bg="#e7e7fb" Icon={Database}
            />
            <SummaryCard
              label="Bullish" badge="UP" badgeBg="#bbf1d3" badgeColor="#166534"
              value={summary.bullish} valueColor="#16a34a" bg="#e7f8ef" Icon={TrendingUp}
            />
            <SummaryCard
              label="Bearish" badge="DOWN" badgeBg="#fbcfd1" badgeColor="#991b1b"
              value={summary.bearish} valueColor="#dc2626" bg="#fdecec" Icon={TrendingDown}
            />
            <SummaryCard
              label="Neutral" badge="FLAT" badgeBg="#e5e7eb" badgeColor="#374151"
              value={summary.neutral} valueColor="#6b7280" bg="#f3f4f6" Icon={Target}
            />
          </div>

          {/* Search */}
          <div style={{ padding:"0 24px 12px" }}>
            <div style={{ position:"relative", maxWidth:260 }}>
              <span style={{ position:"absolute", left:14, top:"50%", transform:"translateY(-50%)", color:"#9ca3af", fontSize:13 }}>⌕</span>
              <input placeholder="Search coins…" value={search} onChange={e=>setSearch(e.target.value)} style={{
                width:"100%", padding:"9px 14px 9px 32px", borderRadius:10, border:"1px solid #e5e7eb", fontSize:13,
                color:"#374151", outline:"none", background:"#fff", boxSizing:"border-box",
              }}/>
            </div>
            <div style={{ marginTop:8, fontSize:12, color:"#9ca3af" }}>
              <strong style={{ color:"#374151" }}>{sortedCoins.length}</strong> coins
            </div>
          </div>

          {/* Filters */}
          <div style={{ padding:"16px 24px", display:"flex", alignItems:"center", gap:16, flexWrap:"wrap", borderTop:"1px solid #e5e7eb", borderBottom:"1px solid #e5e7eb" }}>
            <div style={{ display:"flex", gap:4 }}>
              {["All","Bullish","Bearish","Neutral"].map(t => (
                <button key={t} onClick={()=>setTrendFilter(t)} style={{
                  padding:"6px 16px", borderRadius:8, fontSize:12, fontWeight:700, cursor:"pointer",
                  border: trendFilter===t ? "1px solid #111827" : "1px solid #e5e7eb",
                  background: trendFilter===t ? "#111827" : "#fff",
                  color: trendFilter===t ? "#fff" : "#6b7280", textTransform:"uppercase",
                }}>{t}</button>
              ))}
            </div>
          </div>

          {/* Table */}
          <div style={{ margin:"16px 24px 24px", background:"#fff", borderRadius:14, border:"1px solid #e5e7eb", overflow:"hidden" }}>
            <div style={{ overflowX:"auto" }}>
              <table style={{ width:"100%", borderCollapse:"collapse", fontSize:13 }}>
                <thead>
                  <tr>
                    {HOME_COLS.map(col => (
                      <TH key={col.k} right={col.r} onClick={col.s?()=>toggleSort(col.k):null}
                        sorted={sort.k===col.k} dir={sort.dir}>{col.l}</TH>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {homePageRows.length === 0 ? (
                    <tr><td colSpan={HOME_COLS.length} style={{ padding:48, textAlign:"center", color:"#9ca3af", fontSize:13 }}>No coins match your filters.</td></tr>
                  ) : homePageRows.map((c, iOnPage) => {
                    const i = (homePage - 1) * HOME_PAGE_SIZE + iOnPage;
                    const { base, quote } = fmtSym(c.symbol);
                    return (
                      <tr key={c.symbol}
                        style={{ borderBottom:"1px solid #f3f4f6", background:i%2===0?"#fff":"#fafafa" }}
                        onMouseEnter={e=>e.currentTarget.style.background="#f0f9ff"}
                        onMouseLeave={e=>e.currentTarget.style.background=i%2===0?"#fff":"#fafafa"}
                      >
                        <td style={{ padding:"11px 14px", color:"#9ca3af", fontWeight:500 }}>{i + 1}</td>
                        <td style={{ padding:"11px 14px", fontWeight:700 }}>
                          <span style={{ color:"#f59e0b" }}>{base}</span>
                          <span style={{ color:"#9ca3af", fontSize:11 }}>{quote}</span>
                        </td>
                        <td style={{ padding:"11px 14px" }}>
                          {c.trend ? <Trend t={c.trend}/> : <span style={{ color:"#d1d5db", fontSize:12 }}>Insufficient data</span>}
                        </td>
                        <td style={{ padding:"11px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", fontWeight:500 }}>{fmtPrice(c.price)}</td>
                        <td style={{ padding:"11px 14px", textAlign:"right", color:"#9ca3af", fontVariantNumeric:"tabular-nums" }}>{c.candle_count}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {sortedCoins.length > 0 && (
              <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", padding:"12px 16px", borderTop:"1px solid #e5e7eb" }}>
                <span style={{ fontSize:12, color:"#9ca3af" }}>
                  Showing {(homePage-1)*HOME_PAGE_SIZE+1}–{Math.min(homePage*HOME_PAGE_SIZE, sortedCoins.length)} of {sortedCoins.length}
                </span>
                <div style={{ display:"flex", alignItems:"center", gap:8 }}>
                  <button onClick={()=>setHomePage(p=>Math.max(1,p-1))} disabled={homePage<=1} style={{
                    padding:"6px 14px", borderRadius:7, fontSize:12, fontWeight:600,
                    border:"1px solid #e5e7eb", background:"#fff", color:homePage<=1?"#d1d5db":"#374151",
                    cursor:homePage<=1?"default":"pointer",
                  }}>Prev</button>
                  <span style={{ fontSize:12, color:"#6b7280" }}>Page {homePage} of {homePageCount}</span>
                  <button onClick={()=>setHomePage(p=>Math.min(homePageCount,p+1))} disabled={homePage>=homePageCount} style={{
                    padding:"6px 14px", borderRadius:7, fontSize:12, fontWeight:600,
                    border:"1px solid #e5e7eb", background:"#fff", color:homePage>=homePageCount?"#d1d5db":"#374151",
                    cursor:homePage>=homePageCount?"default":"pointer",
                  }}>Next</button>
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// ─── Swing Strategy Page (Swing Zone Retest screener) ────────────────────────
// Reads GET /api/swing-zones (see app/services/swing_strategy.py on the
// backend) — every coin >= $10M 24h volume with a currently live
// (ARMED/TRIGGERED) long or short zone, detected from 1d candles per the
// swing-zone-retest rules (10% confirmation move, Z = swing candle's close,
// 5% stop / 10% target). Purely a read: all detection runs server-side.
const SWING_ALERT_BAND_PCT = 2; // rows within this distance to Z are the actionable watchlist
// Backend direction values stay LONG/SHORT (matches the strategy spec's own
// terminology) — displayed as BULLISH/BEARISH.
const SWING_DIRECTION_LABEL = { LONG: "BUY", SHORT: "SELL" };
// The live dashboard now keeps showing a side's last zone even after it
// resolves (TP_HIT/SL_HIT), so a coin's most recent outcome stays visible
// instead of vanishing the moment it closes — displayed here as a single
// "COMPLETED" badge, colored by win/loss rather than exposing the raw
// TP_HIT/SL_HIT state names.
const SWING_STATE_DISPLAY = {
  ARMED:     { label: "ARMED",     bg: "#f3f4f6", color: "#6b7280" },
  TRIGGERED: { label: "TRIGGERED", bg: "#e0f2fe", color: "#0369a1" },
  TP_HIT:    { label: "COMPLETED", bg: "#dcfce7", color: "#15803d" },
  SL_HIT:    { label: "COMPLETED", bg: "#fee2e2", color: "#b91c1c" },
  EXPIRED:   { label: "EXPIRED",   bg: "#f3f4f6", color: "#9ca3af" },
};
const SWING_SL_TP_VISIBLE_STATES = ["TRIGGERED", "TP_HIT", "SL_HIT"];
// Risk:Reward presets — sl/tp are whole percent values sent straight to
// /api/swing-zones and /api/swing-backtest's sl_pct/tp_pct query params.
// Matches the strategy spec's own two-variant backtest matrix exactly
// (-5%/+10% and -2%/+10%) — no extra presets beyond what's specified.
const SWING_RR_MODES = [
  { key: "1:2", label: "1:2", sl: 5, tp: 10 },
  { key: "1:5", label: "1:5", sl: 2, tp: 10 },
];
// Columns follow the 4-point flow in chronological order: 1) anchor
// (peak/trough) 2) candidate (the swing low/high — Z's own candle)
// 3) confirmation (the candle whose close ARMS the zone) 4) entry (the
// candle that touches Z). Each point's time+price are combined into a
// single column (price on top, time beneath), same stacking as the
// State cell's badge+timestamp.
const SWING_COLS = [
  {k:"symbol",           l:"Symbol"},
  {k:"direction",        l:"Direction"},
  {k:"state",            l:"State"},
  {k:"zeroth_price",     l:"0 Candle",    r:true},
  {k:"anchor_price",     l:"1st Candle",  r:true},
  {k:"z",                l:"Z Candle",    r:true},
  {k:"confirm_price",    l:"Confirm",     r:true},
  {k:"entry_price",      l:"Entry",       r:true},
  {k:"price",            l:"Price",            r:true},
  {k:"distance_pct",     l:"Distance %",       r:true},
  {k:"sl",               l:"SL",               r:true},
  {k:"tp",               l:"TP",               r:true},
  {k:"confirm_move_pct", l:"Confirm Move %",   r:true},
  {k:"zone_age",         l:"Zone Age",         r:true},
  {k:"body_ratio",       l:"Body Ratio",       r:true},
  {k:"swing_extreme",    l:"Swing Extreme",    r:true},
];

function SwingStrategyPage({ onHome, onBacktest }) {
  const [data, setData]                 = useState(null);
  const [loading, setLoading]           = useState(true);
  const [error, setError]               = useState(null);
  const [directionFilter, setDirectionFilter] = useState("All");
  const [stateFilter, setStateFilter]   = useState("All");
  const [search, setSearch]             = useState("");
  const [sort, setSort]                 = useState({ k:"detected_at", dir:"desc" });

  // `silent` skips the loading flag so background polls don't blank the
  // table out every few seconds — only the very first load (and a manual
  // Refresh click) shows the "Scanning…" placeholder.
  const load = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/swing-zones`);
      if (!res.ok) throw new Error(`API ${res.status}`);
      setData(await res.json());
    } catch (e) {
      setError(e.message);
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  // Zones are computed live from candles the Live Stream keeps updating in
  // real time — polling re-scans periodically so the dashboard tracks that
  // without needing a manual refresh every time.
  useEffect(() => {
    load();
    const id = setInterval(() => load({ silent: true }), 10_000);
    return () => clearInterval(id);
  }, [load]);

  const zones = data?.zones || [];
  const toggleSort = k => setSort(s => s.k===k ? {k, dir:s.dir==="asc"?"desc":"asc"} : {k, dir:"asc"});

  const filteredZones = useMemo(() => zones.filter(z => {
    if (directionFilter !== "All" && z.direction !== directionFilter.toUpperCase()) return false;
    if (stateFilter === "Completed" && !["TP_HIT","SL_HIT"].includes(z.state)) return false;
    if (stateFilter !== "All" && stateFilter !== "Completed" && z.state !== stateFilter.toUpperCase()) return false;
    if (search && !z.symbol.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  }), [zones, directionFilter, stateFilter, search]);

  // Distance % sorts by absolute value — it's the "how close to actionable"
  // metric (spec's primary sort key), not a signed one where +5% and -5%
  // should land at opposite ends of the list.
  const sortedZones = useMemo(() => [...filteredZones].sort((a, b) => {
    const dir = sort.dir === "asc" ? 1 : -1;
    let av = a[sort.k], bv = b[sort.k];
    if (sort.k === "distance_pct") { av = Math.abs(av); bv = Math.abs(bv); }
    if (av == null && bv == null) return 0;
    if (av == null) return 1; if (bv == null) return -1;
    return typeof av === "string" ? dir*av.localeCompare(bv) : dir*(av-bv);
  }), [filteredZones, sort]);

  return (
    <div style={{ fontFamily:"'Inter',system-ui,sans-serif", background:"#f5f6f8", minHeight:"100vh", width:"100%", color:"#111827" }}>
      {/* Header */}
      <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", padding:"20px 24px 16px", flexWrap:"wrap", gap:12 }}>
        <div style={{ display:"flex", alignItems:"center", gap:14 }}>
          <button onClick={onHome} style={{
            display:"flex", alignItems:"center", gap:5, padding:"5px 12px",
            borderRadius:7, border:"1px solid #e5e7eb", background:"transparent",
            fontSize:12, fontWeight:600, color:"#374151", cursor:"pointer",
          }}>← Home</button>
          <div>
            <div style={{ fontSize:26, fontWeight:800, letterSpacing:"-0.02em" }}>SWING STRATEGY</div>
            <div style={{ fontSize:12, color:"#9ca3af", fontWeight:600, marginTop:2, letterSpacing:"0.02em" }}>
              {data
                ? `${data.timeframe.toUpperCase()} · ${(data.swing_threshold*100).toFixed(0)}% CONFIRM · ${(data.tp_pct*100).toFixed(0)}/${(data.sl_pct*100).toFixed(0)} TP/SL`
                : "SWING ZONE RETEST"}
            </div>
          </div>
        </div>
        <div style={{ display:"flex", alignItems:"center", gap:10 }}>
          <div style={{ position:"relative", width:180 }}>
            <span style={{ position:"absolute", left:12, top:"50%", transform:"translateY(-50%)", color:"#9ca3af", fontSize:12 }}>⌕</span>
            <input placeholder="Search coins…" value={search} onChange={e=>setSearch(e.target.value)} style={{
              width:"100%", padding:"7px 12px 7px 28px", borderRadius:8, border:"1px solid #e5e7eb", fontSize:12,
              color:"#374151", outline:"none", background:"#fff", boxSizing:"border-box",
            }}/>
          </div>
          <button onClick={() => load()} disabled={loading} style={{
            padding:"7px 16px", borderRadius:8, border:"1px solid #e5e7eb",
            background:"#fff", color:"#374151", fontSize:12, fontWeight:700,
            cursor: loading ? "not-allowed" : "pointer", opacity: loading ? 0.6 : 1,
          }}>{loading ? "Scanning…" : "↻ Refresh"}</button>
        </div>
      </div>

      {loading ? (
        <div style={{ padding:60, textAlign:"center", color:"#9ca3af", fontSize:13 }}>Scanning swing zones…</div>
      ) : error ? (
        <div style={{ margin:"0 24px 16px", padding:24, textAlign:"center", color:"#dc2626", fontSize:13, background:"#fff", borderRadius:14, border:"1px solid #e5e7eb" }}>⚠ {error}</div>
      ) : zones.length === 0 ? (
        <div style={{ margin:"0 24px 16px", padding:60, textAlign:"center", color:"#9ca3af", fontSize:13, background:"#fff", borderRadius:14, border:"1px solid #e5e7eb" }}>
          No live zones right now — needs coins with stored 1d candles and 24h volume ≥ ${((data?.min_volume_usdt ?? 10_000_000)/1e6).toFixed(0)}M.
        </div>
      ) : (
        <>
          <div style={{ padding:"0 24px 12px", fontSize:12, color:"#9ca3af" }}>
            <strong style={{ color:"#374151" }}>{sortedZones.length}</strong> zone(s) across{" "}
            <strong style={{ color:"#374151" }}>{data.symbols_scanned}</strong> coin(s) ≥ ${(data.min_volume_usdt/1e6).toFixed(0)}M volume
          </div>

          {/* Filters */}
          <div style={{ padding:"16px 24px", display:"flex", alignItems:"center", gap:16, flexWrap:"wrap", borderTop:"1px solid #e5e7eb", borderBottom:"1px solid #e5e7eb" }}>
            <div style={{ display:"flex", gap:4 }}>
              {["All","Long","Short"].map(d => (
                <button key={d} onClick={()=>setDirectionFilter(d)} style={{
                  padding:"6px 16px", borderRadius:8, fontSize:12, fontWeight:700, cursor:"pointer",
                  border: directionFilter===d ? "1px solid #111827" : "1px solid #e5e7eb",
                  background: directionFilter===d ? "#111827" : "#fff",
                  color: directionFilter===d ? "#fff" : "#6b7280", textTransform:"uppercase",
                }}>{d === "All" ? "All" : SWING_DIRECTION_LABEL[d.toUpperCase()]}</button>
              ))}
            </div>
            <div style={{ width:1, height:20, background:"#e5e7eb" }}/>
            <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase" }}>State</span>
            <div style={{ display:"flex", gap:4 }}>
              {["All","Armed","Triggered","Completed"].map(s => (
                <button key={s} onClick={()=>setStateFilter(s)} style={{
                  padding:"5px 13px", borderRadius:7, fontSize:12, fontWeight:600, cursor:"pointer",
                  border:stateFilter===s?"1.5px solid #6366f1":"1px solid #e5e7eb", background:stateFilter===s?"#eef2ff":"#fff",
                  color:stateFilter===s?"#6366f1":"#6b7280",
                }}>{s}</button>
              ))}
            </div>
            <button onClick={onBacktest} style={{
              marginLeft:"auto", padding:"6px 16px", borderRadius:8, border:"1px solid #6366f1",
              background:"transparent", color:"#6366f1", fontSize:12, fontWeight:700, cursor:"pointer", whiteSpace:"nowrap",
            }}>Backtest Summary</button>
          </div>

          {/* Table */}
          <div style={{ margin:"16px 24px 24px", background:"#fff", borderRadius:14, border:"1px solid #e5e7eb", overflow:"hidden" }}>
            <div style={{ overflowX:"auto" }}>
              <table style={{ width:"100%", borderCollapse:"collapse", fontSize:13 }}>
                <thead>
                  <tr>
                    {SWING_COLS.map(col => (
                      <TH key={col.k} right={col.r} onClick={()=>toggleSort(col.k)} sorted={sort.k===col.k} dir={sort.dir}>{col.l}</TH>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sortedZones.length === 0 ? (
                    <tr><td colSpan={SWING_COLS.length} style={{ padding:48, textAlign:"center", color:"#9ca3af", fontSize:13 }}>No zones match your filters.</td></tr>
                  ) : sortedZones.map(z => {
                    const { base, quote } = fmtSym(z.symbol);
                    const long = z.direction === "LONG";
                    const alert = Math.abs(z.distance_pct) <= SWING_ALERT_BAND_PCT;
                    return (
                      <tr key={`${z.symbol}-${z.direction}`}
                        style={{ borderBottom:"1px solid #f3f4f6", background: alert ? "#fffbeb" : "#fff" }}
                      >
                        <td style={{ padding:"11px 14px", fontWeight:700 }}>
                          <span style={{ color:"#f59e0b" }}>{base}</span>
                          <span style={{ color:"#9ca3af", fontSize:11 }}>{quote}</span>
                        </td>
                        <td style={{ padding:"11px 14px" }}>
                          <span style={{
                            display:"inline-block", padding:"2px 8px", borderRadius:4, fontSize:11,
                            fontWeight:700, background: long ? "#dcfce7" : "#fee2e2", color: long ? "#15803d" : "#b91c1c",
                          }}>{SWING_DIRECTION_LABEL[z.direction]}</span>
                        </td>
                        <td style={{ padding:"11px 14px" }}>
                          <div style={{ display:"flex", flexDirection:"column", gap:3, alignItems:"flex-start" }}>
                            <span style={{
                              display:"inline-block", padding:"2px 8px", borderRadius:4, fontSize:11, fontWeight:700,
                              background: (SWING_STATE_DISPLAY[z.state] || SWING_STATE_DISPLAY.ARMED).bg,
                              color: (SWING_STATE_DISPLAY[z.state] || SWING_STATE_DISPLAY.ARMED).color,
                            }}>{(SWING_STATE_DISPLAY[z.state] || SWING_STATE_DISPLAY.ARMED).label}</span>
                            <span style={{ fontSize:10, color:"#9ca3af", whiteSpace:"nowrap" }}>
                              {/* Timestamp matches whichever state is currently shown — when it
                                  got ARMED, or when it TRIGGERED, or when it COMPLETED — not
                                  always the original confirmation time. */}
                              {fmtTimeOnly(
                                z.state === "ARMED" ? z.detected_at
                                : z.state === "TRIGGERED" ? z.triggered_at
                                : z.resolved_at
                              )}
                            </span>
                          </div>
                        </td>
                        {/* 0 candle — the prior extreme that validated the anchor (point 1) */}
                        <td style={{ padding:"11px 14px", textAlign:"right" }}>
                          <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:2 }}>
                            <span style={{ fontVariantNumeric:"tabular-nums", color:"#9ca3af" }}>{fmtPrice(z.zeroth_price)}</span>
                            <span style={{ fontSize:10, color:"#9ca3af", whiteSpace:"nowrap" }}>{fmtTime(z.zeroth_time)}</span>
                          </div>
                        </td>
                        {/* 1st candle — anchor (peak/trough) */}
                        <td style={{ padding:"11px 14px", textAlign:"right" }}>
                          <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:2 }}>
                            <span style={{ fontVariantNumeric:"tabular-nums", color:"#9ca3af" }}>{fmtPrice(z.anchor_price)}</span>
                            <span style={{ fontSize:10, color:"#9ca3af", whiteSpace:"nowrap" }}>{fmtTime(z.anchor_time)}</span>
                          </div>
                        </td>
                        {/* Z candle — the swing low/high itself */}
                        <td style={{ padding:"11px 14px", textAlign:"right" }}>
                          <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:2 }}>
                            <span style={{ fontVariantNumeric:"tabular-nums" }}>{fmtPrice(z.z)}</span>
                            <span style={{ fontSize:10, color:"#9ca3af", whiteSpace:"nowrap" }}>{fmtTime(z.candidate_time)}</span>
                          </div>
                        </td>
                        {/* Confirming candle */}
                        <td style={{ padding:"11px 14px", textAlign:"right" }}>
                          <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:2 }}>
                            <span style={{ fontVariantNumeric:"tabular-nums", color:"#9ca3af" }}>{fmtPrice(z.confirm_price)}</span>
                            <span style={{ fontSize:10, color:"#9ca3af", whiteSpace:"nowrap" }}>{fmtTime(z.detected_at)}</span>
                          </div>
                        </td>
                        {/* Entry candle */}
                        <td style={{ padding:"11px 14px", textAlign:"right" }}>
                          <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:2 }}>
                            <span style={{ fontVariantNumeric:"tabular-nums" }}>{z.triggered_at ? fmtPrice(z.entry_price) : "—"}</span>
                            <span style={{ fontSize:10, color:"#9ca3af", whiteSpace:"nowrap" }}>{z.triggered_at ? fmtTime(z.triggered_at) : ""}</span>
                          </div>
                        </td>
                        <td style={{ padding:"11px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", fontWeight:600 }}>{fmtPrice(z.price)}</td>
                        <td style={{ padding:"11px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", fontWeight:700, color: alert ? "#b45309" : "#374151" }}>
                          {z.distance_pct>=0?"+":""}{z.distance_pct.toFixed(2)}%
                        </td>
                        <td style={{ padding:"11px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", color:"#dc2626" }}>{SWING_SL_TP_VISIBLE_STATES.includes(z.state) ? fmtPrice(z.sl) : "—"}</td>
                        <td style={{ padding:"11px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", color:"#16a34a" }}>{SWING_SL_TP_VISIBLE_STATES.includes(z.state) ? fmtPrice(z.tp) : "—"}</td>
                        <td style={{ padding:"11px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", color:"#6b7280" }}>{z.confirm_move_pct.toFixed(2)}%</td>
                        <td style={{ padding:"11px 14px", textAlign:"right", color:"#9ca3af" }}>{fmtZoneAge(z.detected_at)}</td>
                        <td style={{ padding:"11px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", color:"#6b7280" }}>{z.body_ratio.toFixed(2)}</td>
                        <td style={{ padding:"11px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", color:"#9ca3af" }}>{fmtPrice(z.swing_extreme)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// ─── Swing Backtest Summary Page ──────────────────────────────────────────────
// Reads GET /api/swing-backtest — every historical trade (a zone that
// actually got entered) across all qualifying coins, resolved as WIN/LOSS
// or still OPEN. Unlike the EMA backtest, entry is always exactly Z and
// exit is always exactly TP or SL (this strategy has no slippage/partial-
// fill concept), so every WIN is +10% and every LOSS is -5% — the
// aggregation here is purely about which/how-many trades fall in the
// selected period, not simulating fill prices client-side.
const SWING_BT_PERIOD_LABELS = [["day","Day"],["week","Week"],["month","Month"]];
const SWING_BT_PERIOD_DAYS = { day: 1, week: 7, month: 30 };
const SWING_BT_COLS = [
  {k:"symbol",        l:"Symbol"},
  {k:"direction",     l:"Direction"},
  {k:"zeroth_price",  l:"0 Candle",   r:true},
  {k:"anchor_price",  l:"1st Candle", r:true},
  {k:"z",             l:"Z Candle",   r:true},
  {k:"confirm_price", l:"Confirm",    r:true},
  {k:"entry_price", l:"Entry",       r:true},
  {k:"sl",          l:"Stop Loss",   r:true},
  {k:"tp",          l:"Take Profit", r:true},
  {k:"exit_price",  l:"Exit",        r:true},
  {k:"duration_ms", l:"Duration",    r:true},
  {k:"gain_pct",    l:"PnL %",       r:true},
  {k:"gain_dollar", l:"PnL ($)",     r:true},
  {k:"result",      l:"Result"},
];

function SwingBacktestPage({ onBack }) {
  const [btPeriod, setBtPeriod]       = useState("day");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd]     = useState("");
  const [capital, setCapital]         = useState(1000);
  const [rrMode, setRrMode]           = useState("1:2");
  const [search, setSearch]           = useState("");
  const [loading, setLoading]         = useState(true);
  const [error, setError]             = useState(null);
  const [allTrades, setAllTrades]     = useState([]);
  const [sort, setSort]               = useState({ k:"entry_time", dir:"desc" });
  const [exporting, setExporting]     = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rr = SWING_RR_MODES.find(m => m.key === rrMode) || SWING_RR_MODES[0];
      const res = await fetch(`${API_BASE}/swing-backtest?sl_pct=${rr.sl}&tp_pct=${rr.tp}`);
      if (!res.ok) throw new Error(`API ${res.status}`);
      const json = await res.json();
      setAllTrades(json.trades || []);
    } catch (e) {
      setError(e.message);
      setAllTrades([]);
    } finally {
      setLoading(false);
    }
  }, [rrMode]);

  useEffect(() => { load(); }, [load]);

  // Window range — same convention as the EMA Backtest Summary page: "Day"
  // means the actual IST calendar day, not a rolling 24h window, and a
  // custom start/end always takes priority once both are picked.
  const { rangeStartMs, rangeEndMs } = useMemo(() => {
    if (customStart && customEnd) {
      return {
        rangeStartMs: new Date(`${customStart}T00:00:00`).getTime(),
        rangeEndMs: new Date(`${customEnd}T23:59:59.999`).getTime(),
      };
    }
    if (btPeriod === "day") {
      const IST_OFFSET_MS = 5.5 * 3_600_000;
      const nowIst = Date.now() + IST_OFFSET_MS;
      const todayIstMidnightIst = Math.floor(nowIst / 86_400_000) * 86_400_000;
      return { rangeStartMs: todayIstMidnightIst - IST_OFFSET_MS, rangeEndMs: Date.now() };
    }
    return { rangeStartMs: Date.now() - SWING_BT_PERIOD_DAYS[btPeriod] * 86_400_000, rangeEndMs: Date.now() };
  }, [btPeriod, customStart, customEnd]);

  const windowTrades = useMemo(
    () => allTrades.filter(t => t.entry_time >= rangeStartMs && t.entry_time <= rangeEndMs),
    [allTrades, rangeStartMs, rangeEndMs]
  );

  const filteredTrades = useMemo(() => windowTrades.filter(t => {
    if (search && !t.symbol.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  }), [windowTrades, search]);

  // Capital-based $ PnL — trades are otherwise fixed-percentage (+TP_PCT or
  // -SL_PCT), so this is the only place "capital" actually matters.
  const withDollar = useMemo(() => filteredTrades.map(t => ({
    ...t, gain_dollar: t.gain_pct != null ? (capital * t.gain_pct) / 100 : null,
  })), [filteredTrades, capital]);

  const toggleSort = k => setSort(s => s.k===k ? {k, dir:s.dir==="asc"?"desc":"asc"} : {k, dir:"desc"});
  const sortedTrades = useMemo(() => [...withDollar].sort((a, b) => {
    const dir = sort.dir === "asc" ? 1 : -1;
    const av = a[sort.k], bv = b[sort.k];
    if (av == null && bv == null) return 0;
    if (av == null) return 1; if (bv == null) return -1;
    return typeof av === "string" ? dir*av.localeCompare(bv) : dir*(av-bv);
  }), [withDollar, sort]);

  const stats = useMemo(() => {
    const won = filteredTrades.filter(t => t.result === "WIN").length;
    const lost = filteredTrades.filter(t => t.result === "LOSS").length;
    const closed = won + lost;
    const pnlDollar = withDollar.reduce((sum, t) => sum + (t.gain_dollar || 0), 0);
    const winRate = closed > 0 ? (won / closed) * 100 : null;
    return { won, lost, pnlDollar, winRate, total: filteredTrades.length };
  }, [filteredTrades, withDollar]);

  const exportCsv = useCallback(() => {
    setExporting(true);
    try {
      const header = [
        "Symbol","Direction",
        "0 Candle Time","0 Candle Price",
        "1st Candle Time","1st Candle Price",
        "Z Candle Time","Z Candle Price",
        "Confirm Time","Confirm Price",
        "Entry Time","Entry Price","Stop Loss","Take Profit",
        "Exit Time","Exit Price","Duration","PnL %","PnL ($)","Result",
      ];
      const csvEscape = v => `"${String(v ?? "").replace(/"/g, '""')}"`;
      const lines = [header.join(",")];
      for (const t of sortedTrades) {
        const open = t.result === "OPEN";
        lines.push([
          t.symbol, SWING_DIRECTION_LABEL[t.direction],
          t.zeroth_time ? fmtDateTime(t.zeroth_time) : "", t.zeroth_price ?? "",
          fmtDateTime(t.anchor_time), t.anchor_price,
          fmtDateTime(t.candidate_time), t.z,
          fmtDateTime(t.detected_at), t.confirm_price,
          fmtDateTime(t.entry_time), t.entry_price, t.sl, t.tp,
          open ? "Still running" : fmtDateTime(t.exit_time), open ? "" : t.exit_price,
          fmtDuration(t.duration_ms), open ? "" : t.gain_pct.toFixed(2), open ? "" : t.gain_dollar.toFixed(2),
          t.result,
        ].map(csvEscape).join(","));
      }

      lines.push("");
      lines.push("Summary");
      lines.push(["Total Trades","Won","Loss","Win Rate %","PnL ($)"].map(csvEscape).join(","));
      lines.push([
        stats.total, stats.won, stats.lost,
        stats.winRate != null ? stats.winRate.toFixed(1) : "—",
        stats.pnlDollar.toFixed(2),
      ].map(csvEscape).join(","));

      const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `swing_backtest_${btPeriod}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } finally {
      setExporting(false);
    }
  }, [sortedTrades, btPeriod, stats]);

  return (
    <div style={{ fontFamily:"'Inter',system-ui,sans-serif", background:"#f5f6f8", minHeight:"100vh", width:"100%", color:"#111827" }}>
      {/* Header */}
      <div style={{ display:"flex", alignItems:"center", gap:10, padding:"13px 24px", borderBottom:"1px solid #e8eaed", flexWrap:"wrap" }}>
        <button onClick={onBack} style={{
          display:"flex", alignItems:"center", gap:5, padding:"5px 12px",
          borderRadius:7, border:"1px solid #e5e7eb", background:"transparent",
          fontSize:12, fontWeight:600, color:"#374151", cursor:"pointer",
        }}>← Back</button>
        <span style={{ color:"#d1d5db", fontSize:14 }}>›</span>
        <span style={{ fontWeight:800, fontSize:17 }}>Swing Backtest Summary</span>
        <span style={{ color:"#9ca3af", fontSize:12 }}>All qualifying coins · Swing Zone Retest</span>
        <div style={{ marginLeft:"auto", position:"relative", width:180 }}>
          <span style={{ position:"absolute", left:12, top:"50%", transform:"translateY(-50%)", color:"#9ca3af", fontSize:12 }}>⌕</span>
          <input placeholder="Search coins…" value={search} onChange={e=>setSearch(e.target.value)} style={{
            width:"100%", padding:"7px 12px 7px 28px", borderRadius:8, border:"1px solid #e5e7eb", fontSize:12,
            color:"#374151", outline:"none", background:"#fff", boxSizing:"border-box",
          }}/>
        </div>
        <button onClick={exportCsv} disabled={exporting || loading || sortedTrades.length === 0} style={{
          display:"flex", alignItems:"center", gap:6,
          padding:"7px 16px", borderRadius:8, border:"none",
          background:"#111827", color:"#fff", fontSize:12, fontWeight:700,
          cursor: (exporting || loading || sortedTrades.length === 0) ? "not-allowed" : "pointer",
          opacity: (exporting || loading || sortedTrades.length === 0) ? 0.6 : 1,
        }}>{exporting ? "Exporting…" : "⭳ Export"}</button>
      </div>

      {/* Controls */}
      <div style={{ padding:"20px 24px" }}>
        <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:16, flexWrap:"wrap" }}>
          <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase" }}>Period</span>
          <div style={{ display:"flex", gap:4 }}>
            {SWING_BT_PERIOD_LABELS.map(([k,l]) => (
              <button key={k} disabled={loading} onClick={()=>{ setBtPeriod(k); setCustomStart(""); setCustomEnd(""); }} style={{
                padding:"5px 14px", borderRadius:7, fontSize:12, fontWeight:700,
                cursor: loading ? "not-allowed" : "pointer",
                border: (btPeriod===k && !customStart && !customEnd) ? "1px solid #111827" : "1px solid #e5e7eb",
                background: (btPeriod===k && !customStart && !customEnd) ? "#111827" : "#fff",
                color: (btPeriod===k && !customStart && !customEnd) ? "#fff" : "#6b7280",
              }}>{l}</button>
            ))}
          </div>
          <div style={{ display:"flex", alignItems:"center", gap:6 }}>
            <input
              type="date" disabled={loading} value={customStart} max={customEnd || undefined}
              onChange={e => setCustomStart(e.target.value)}
              style={{ padding:"4px 8px", borderRadius:6, border:"1px solid #e5e7eb", fontSize:12, color:"#374151" }}
            />
            <span style={{ color:"#9ca3af", fontSize:12 }}>to</span>
            <input
              type="date" disabled={loading} value={customEnd} min={customStart || undefined}
              onChange={e => setCustomEnd(e.target.value)}
              style={{ padding:"4px 8px", borderRadius:6, border:"1px solid #e5e7eb", fontSize:12, color:"#374151" }}
            />
          </div>
          <div style={{ width:1, height:20, background:"#e5e7eb" }}/>
          <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase" }}>RR</span>
          <div style={{ display:"flex", gap:4 }}>
            {SWING_RR_MODES.map(m => (
              <button key={m.key} disabled={loading} onClick={()=>setRrMode(m.key)} title={`${m.sl}% SL / ${m.tp}% TP`} style={{
                padding:"5px 14px", borderRadius:7, fontSize:12, fontWeight:700,
                cursor: loading ? "not-allowed" : "pointer", opacity: loading && rrMode!==m.key ? 0.5 : 1,
                border: rrMode===m.key ? "1.5px solid #f59e0b" : "1px solid #e5e7eb",
                background: rrMode===m.key ? "#fff7ed" : "#fff",
                color: rrMode===m.key ? "#f59e0b" : "#6b7280",
              }}>{m.label}</button>
            ))}
          </div>
          <div style={{ width:1, height:20, background:"#e5e7eb" }}/>
          <span style={{ fontSize:11, color:"#9ca3af", fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase" }}>Capital</span>
          <div style={{ display:"flex", alignItems:"center", gap:4 }}>
            <span style={{ color:"#9ca3af", fontSize:12 }}>$</span>
            <input
              type="number" min="1" step="100" defaultValue={capital}
              onBlur={e => { const v = parseFloat(e.target.value); if (v > 0) setCapital(v); }}
              onKeyDown={e => { if (e.key === "Enter") e.target.blur(); }}
              style={{ width:80, padding:"4px 6px", borderRadius:6, border:"1px solid #e5e7eb", fontSize:12 }}
            />
          </div>
          {loading && <span style={{ fontSize:11, color:"#9ca3af" }}>Loading…</span>}
        </div>

        {error ? (
          <div style={{ padding:48, textAlign:"center", color:"#dc2626", fontSize:14, background:"#fff", borderRadius:14, border:"1px solid #e5e7eb" }}>
            <div style={{ fontSize:22, marginBottom:8 }}>⚠</div>
            Could not load swing backtest: <code style={{ fontSize:12 }}>{error}</code>
          </div>
        ) : (
          <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(220px,1fr))", gap:16 }}>
            <SummaryCard
              label="Won" badge="WIN" badgeBg="#bbf1d3" badgeColor="#166534"
              value={stats.won} valueColor="#16a34a" bg="#e7f8ef" Icon={CheckCircle2}
            />
            <SummaryCard
              label="Loss" badge="LOSS" badgeBg="#fbcfd1" badgeColor="#991b1b"
              value={stats.lost} valueColor="#dc2626" bg="#fdecec" Icon={XCircle}
            />
            <SummaryCard
              label="PnL ($)" badge={stats.pnlDollar >= 0 ? "PROFIT" : "LOSS"}
              badgeBg={stats.pnlDollar >= 0 ? "#bfe3fb" : "#fbcfd1"}
              badgeColor={stats.pnlDollar >= 0 ? "#075985" : "#991b1b"}
              value={`${stats.pnlDollar >= 0 ? "+" : ""}$${stats.pnlDollar.toFixed(2)}`}
              valueColor={stats.pnlDollar >= 0 ? "#0891b2" : "#dc2626"} bg="#e6f6fd" Icon={DollarSign}
            />
            <SummaryCard
              label="Win Rate" badge={stats.winRate >= 50 ? "GOOD" : "LOW"}
              badgeBg={stats.winRate >= 50 ? "#bbf1d3" : "#fbcfd1"}
              badgeColor={stats.winRate >= 50 ? "#166534" : "#991b1b"}
              value={stats.winRate != null ? `${stats.winRate.toFixed(1)}%` : "—"}
              valueColor={stats.winRate >= 50 ? "#16a34a" : "#dc2626"} bg="#fdf1e2" Icon={Award}
            />
          </div>
        )}

        {/* Trades table */}
        {!error && (
          <div style={{ marginTop:20, background:"#fff", borderRadius:14, border:"1px solid #e5e7eb", overflow:"hidden" }}>
            <div style={{ overflowX:"auto" }}>
              {loading ? (
                <div style={{ padding:60, textAlign:"center", color:"#9ca3af", fontSize:13 }}>Loading trades…</div>
              ) : sortedTrades.length === 0 ? (
                <div style={{ padding:60, textAlign:"center", color:"#9ca3af", fontSize:14 }}>No trades in this window.</div>
              ) : (
                <table style={{ width:"100%", borderCollapse:"collapse", fontSize:12 }}>
                  <thead>
                    <tr>
                      {SWING_BT_COLS.map(col => (
                        <TH key={col.k} right={col.r} onClick={()=>toggleSort(col.k)} sorted={sort.k===col.k} dir={sort.dir}>{col.l}</TH>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {sortedTrades.map((t, i) => {
                      const win = t.result === "WIN";
                      const open = t.result === "OPEN";
                      const long = t.direction === "LONG";
                      return (
                        <tr key={i}
                          style={{ borderBottom:"1px solid #f3f4f6", background:i%2===0?"#fff":"#fafafa" }}
                          onMouseEnter={e=>e.currentTarget.style.background="#f5f3ff"}
                          onMouseLeave={e=>e.currentTarget.style.background=i%2===0?"#fff":"#fafafa"}
                        >
                          <td style={{ padding:"10px 14px", fontWeight:700 }}>
                            <span style={{ color:"#f59e0b" }}>{t.symbol.replace("USDT","")}</span>
                            <span style={{ color:"#9ca3af", fontSize:10 }}>/USDT</span>
                          </td>
                          <td style={{ padding:"10px 14px" }}>
                            <span style={{
                              display:"inline-block", padding:"2px 8px", borderRadius:4, fontSize:11,
                              fontWeight:700, background:long?"#dcfce7":"#fee2e2", color:long?"#15803d":"#b91c1c"
                            }}>{SWING_DIRECTION_LABEL[t.direction]}</span>
                          </td>
                          {/* 0 candle — the prior extreme that validated the anchor (point 1) */}
                          <td style={{ padding:"10px 14px", textAlign:"right" }}>
                            <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:2 }}>
                              <span style={{ fontVariantNumeric:"tabular-nums", color:"#9ca3af" }}>{fmtPrice(t.zeroth_price)}</span>
                              <span style={{ fontSize:10, color:"#9ca3af", whiteSpace:"nowrap" }}>{t.zeroth_time ? fmtDateTime(t.zeroth_time) : "—"}</span>
                            </div>
                          </td>
                          {/* 1st candle — anchor (peak/trough) */}
                          <td style={{ padding:"10px 14px", textAlign:"right" }}>
                            <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:2 }}>
                              <span style={{ fontVariantNumeric:"tabular-nums", color:"#9ca3af" }}>{fmtPrice(t.anchor_price)}</span>
                              <span style={{ fontSize:10, color:"#9ca3af", whiteSpace:"nowrap" }}>{fmtDateTime(t.anchor_time)}</span>
                            </div>
                          </td>
                          {/* Z candle — the swing low/high itself */}
                          <td style={{ padding:"10px 14px", textAlign:"right" }}>
                            <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:2 }}>
                              <span style={{ fontVariantNumeric:"tabular-nums" }}>{fmtPrice(t.z)}</span>
                              <span style={{ fontSize:10, color:"#9ca3af", whiteSpace:"nowrap" }}>{fmtDateTime(t.candidate_time)}</span>
                            </div>
                          </td>
                          {/* Confirming candle */}
                          <td style={{ padding:"10px 14px", textAlign:"right" }}>
                            <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:2 }}>
                              <span style={{ fontVariantNumeric:"tabular-nums", color:"#9ca3af" }}>{fmtPrice(t.confirm_price)}</span>
                              <span style={{ fontSize:10, color:"#9ca3af", whiteSpace:"nowrap" }}>{fmtDateTime(t.detected_at)}</span>
                            </div>
                          </td>
                          {/* Entry candle — time+price merged into one column */}
                          <td style={{ padding:"10px 14px", textAlign:"right" }}>
                            <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:2 }}>
                              <span style={{ fontVariantNumeric:"tabular-nums", fontWeight:600 }}>{fmtPrice(t.entry_price)}</span>
                              <span style={{ fontSize:10, color:"#9ca3af", whiteSpace:"nowrap" }}>{fmtDateTime(t.entry_time)}</span>
                            </div>
                          </td>
                          <td style={{ padding:"10px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", color:"#dc2626" }}>{fmtPrice(t.sl)}</td>
                          <td style={{ padding:"10px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums", color:"#16a34a" }}>{fmtPrice(t.tp)}</td>
                          {/* Exit candle — time+price merged into one column */}
                          <td style={{ padding:"10px 14px", textAlign:"right" }}>
                            {open ? <span style={{ color:"#9ca3af" }}>Still running</span> : (
                              <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:2 }}>
                                <span style={{ fontVariantNumeric:"tabular-nums" }}>{fmtPrice(t.exit_price)}</span>
                                <span style={{ fontSize:10, color:"#9ca3af", whiteSpace:"nowrap" }}>{fmtDateTime(t.exit_time)}</span>
                              </div>
                            )}
                          </td>
                          <td style={{ padding:"10px 14px", color:"#6b7280", whiteSpace:"nowrap" }}>{fmtDuration(t.duration_ms)}</td>
                          <td style={{ padding:"10px 14px", textAlign:"right", fontWeight:700, fontVariantNumeric:"tabular-nums",
                            color: open ? "#9ca3af" : t.gain_pct>=0?"#16a34a":"#dc2626"
                          }}>
                            {open ? "—" : `${t.gain_pct>=0?"+":""}${t.gain_pct.toFixed(2)}%`}
                          </td>
                          <td style={{ padding:"10px 14px", textAlign:"right", fontWeight:700, fontVariantNumeric:"tabular-nums",
                            color: open ? "#9ca3af" : t.gain_dollar>=0?"#16a34a":"#dc2626"
                          }}>
                            {open ? "—" : `${t.gain_dollar>=0?"+":""}$${t.gain_dollar.toFixed(2)}`}
                          </td>
                          <td style={{ padding:"10px 14px" }}>
                            <span style={{
                              display:"inline-block", padding:"2px 8px", borderRadius:4, fontSize:11, fontWeight:700,
                              background: open ? "#e0f2fe" : win ? "#dcfce7" : "#fee2e2",
                              color:      open ? "#0369a1" : win ? "#15803d" : "#b91c1c"
                            }}>{t.result}</span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── App router ───────────────────────────────────────────────────────────────
export default function App() {
  const [page, setPage]           = useState("home");
  const [market, setMarket]       = useState("futures");
  const [scanRow, setScanRow]     = useState(null);
  const [detailsRow, setDetailsRow] = useState(null);
  // Where Backtest was opened from, so its Back button returns there instead
  // of always dropping to the scanner (e.g. Details -> Backtest -> Back should
  // land back on Details, not the scanner).
  const [backtestFrom, setBacktestFrom] = useState("scanner");

  const goHome = useCallback(() => setPage("home"), []);

  const goDetails = useCallback(row => {
    setDetailsRow(row);
    setPage("details");
  }, []);

  const goBacktest = useCallback(row => {
    setScanRow(row);
    setBacktestFrom(page);
    setPage("backtest");
  }, [page]);

  const goScreenerBacktest = useCallback(() => {
    setPage("screener-backtest");
  }, []);

  // Returning from Details always lands back on the scanner, which remounts
  // ScannerPage and restarts its auto-refresh polling.
  const goBackFromDetails = useCallback(() => {
    setPage("scanner");
  }, []);

  // Returning from Backtest lands wherever it was opened from.
  const goBackFromBacktest = useCallback(() => {
    setPage(backtestFrom);
  }, [backtestFrom]);

  // ScannerPage stays mounted at all times (just hidden, not unmounted) so
  // its already-loaded CSV data survives navigating to Backtest/Backtest
  // Summary and back — clicking "Back" used to remount it from scratch,
  // throwing away everything it had already fetched and forcing a full
  // reload every single time.
  return (
    <>
      {page === "home" && <HomePage onOpenScanner={() => setPage("scanner")} onOpenSwing={() => setPage("swing-strategy")}/>}
      <div style={{ display: page === "scanner" ? "block" : "none" }}>
        <ScannerPage onBacktest={goBacktest} onScreenerBacktest={goScreenerBacktest} onHome={goHome}/>
      </div>
      {page === "backtest" && <BacktestPage scanRow={scanRow} onBack={goBackFromBacktest}/>}
      {page === "details" && <DetailsPage row={detailsRow} market={market} onBack={goBackFromDetails} onBacktest={goBacktest}/>}
      {page === "screener-backtest" && <ScreenerBacktestPage onBack={goBackFromDetails}/>}
      {page === "swing-strategy" && <SwingStrategyPage onHome={goHome} onBacktest={() => setPage("swing-backtest")}/>}
      {page === "swing-backtest" && <SwingBacktestPage onBack={() => setPage("swing-strategy")}/>}
    </>
  );
}
