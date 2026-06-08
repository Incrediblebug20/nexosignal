"""
Multi-agent AI research layer.

Three specialized agents analyze market conditions in parallel:

  Gemini  — Google Generative AI: fundamental + technical market research
             for stocks, crypto, ETFs, Bitcoin, and other assets.

  Grok    — xAI (via OpenAI-compatible API): real-time news sentiment
             and short-term price prediction charts.

  Claude  — Anthropic: final trade execution decision using signal
             intelligence engine, enforcing a strict 5:1 risk-reward
             ratio. Only approves trades where reward/risk >= 5.0.

Usage:
    from trading_agent.ai_research import run_multi_agent_research
    result = run_multi_agent_research(symbol, price, bars, base_signal, confidence, indicators)
"""

import json
import logging
import re
import requests
import concurrent.futures
from dataclasses import dataclass
from typing import Optional, Literal

from . import config

logger = logging.getLogger("trading_agent.ai_research")

Signal = Literal["buy", "sell", "hold"]

_SYSTEM_GEMINI = (
    "You are an expert quantitative market analyst. "
    "You specialize in multi-asset research covering equities, crypto, ETFs, and macro. "
    "You respond ONLY with valid JSON — no prose, no markdown fences."
)

_SYSTEM_GROK = (
    "You are Grok, xAI's market intelligence engine with access to real-time news and social sentiment. "
    "You analyze intraday price action, news catalysts, and prediction patterns. "
    "You respond ONLY with valid JSON — no prose, no markdown fences."
)

_SYSTEM_CLAUDE = (
    "You are Claude, the trade execution AI for a professional algorithmic trading system. "
    "Your only job is to validate trade setups and enforce a strict 5:1 risk-reward rule. "
    "RULE: Never approve a trade where reward/risk < 5.0. "
    "You respond ONLY with valid JSON — no prose, no markdown fences."
)


@dataclass
class AIResearchSignal:
    provider: str                        # "gemini" | "grok" | "claude"
    symbol: str
    signal: Signal                       # "buy" | "sell" | "hold"
    confidence: float                    # 0–100
    rationale: str
    price_target: Optional[float] = None
    stop_loss: Optional[float] = None
    risk_reward_ratio: Optional[float] = None
    sentiment: str = "neutral"           # "bullish" | "bearish" | "neutral"
    timeframe: str = "short"            # "intraday" | "short" | "medium"
    news_catalyst: str = ""
    raw_response: str = ""
    error: Optional[str] = None


@dataclass
class MasterDecision:
    signal: Signal
    confidence: float
    approved: bool
    disagreement: bool
    buy_weight: float
    sell_weight: float
    total_weight: float
    rationale: str


@dataclass
class MultiAgentResearch:
    symbol: str
    gemini: Optional[AIResearchSignal]
    grok: Optional[AIResearchSignal]
    claude: Optional[AIResearchSignal]
    consensus_signal: Signal
    consensus_confidence: float
    approved_5to1: bool
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_reward_ratio: Optional[float] = None
    master_decision: Optional[MasterDecision] = None


def _parse_json_response(text: str) -> dict:
    """Strip markdown fences and parse JSON from model output."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _safe_signal(raw: str) -> Signal:
    v = str(raw).lower().strip()
    return v if v in ("buy", "sell", "hold") else "hold"  # type: ignore[return-value]


def _compute_atr(bars: list[dict], period: int = 14) -> float:
    """Approximate ATR from recent bars for stop-loss sizing."""
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, min(period + 1, len(bars))):
        h = float(bars[-i].get("h", bars[-i]["c"]))
        l = float(bars[-i].get("l", bars[-i]["c"]))
        prev_c = float(bars[-(i + 1)]["c"])
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(trs) / len(trs) if trs else 0.0


# ──────────────────────────────────────────────────────────────────────────────
#  Gemini: market research
# ──────────────────────────────────────────────────────────────────────────────

def _gemini_research(symbol: str, price: float, bars: list[dict]) -> AIResearchSignal:
    """Gemini performs fundamental + technical market research."""
    if not config.GEMINI_API_KEY:
        return AIResearchSignal(
            provider="gemini", symbol=symbol, signal="hold", confidence=0,
            rationale="Gemini API key not set (add GEMINI_API_KEY to .env)",
            error="missing_api_key",
        )
    try:
        import google.generativeai as genai  # type: ignore
        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(
            "gemini-1.5-flash",
            system_instruction=_SYSTEM_GEMINI,
        )

        closes = [float(b["c"]) for b in bars[-15:]]
        pct_chg = ((closes[-1] - closes[0]) / closes[0] * 100) if len(closes) >= 2 else 0.0
        vols = [float(b.get("v", 0)) for b in bars[-5:]]
        avg_vol = sum(vols) / len(vols) if vols else 0
        atr = _compute_atr(bars)

        prompt = (
            f"Analyze {symbol} for an intraday/swing trade.\n"
            f"Current price: ${price:.4f}\n"
            f"15-bar price change: {pct_chg:+.2f}%\n"
            f"Recent closes: {[round(c, 4) for c in closes]}\n"
            f"Average volume (5 bars): {avg_vol:,.0f}\n"
            f"ATR (14): {atr:.4f}\n\n"
            "Return JSON with keys: signal, confidence (int 0-100), sentiment, "
            "price_target (float|null), stop_loss (float|null), rationale (string), "
            "timeframe ('intraday'|'short'|'medium')."
        )
        resp = model.generate_content(prompt)
        data = _parse_json_response(resp.text)

        pt = data.get("price_target")
        sl = data.get("stop_loss")
        rr = None
        if pt and sl and price > 0:
            reward = abs(pt - price)
            risk = abs(price - sl)
            rr = round(reward / risk, 2) if risk > 0 else None

        return AIResearchSignal(
            provider="gemini", symbol=symbol,
            signal=_safe_signal(data.get("signal", "hold")),
            confidence=float(data.get("confidence", 50)),
            rationale=str(data.get("rationale", "")),
            price_target=pt, stop_loss=sl, risk_reward_ratio=rr,
            sentiment=str(data.get("sentiment", "neutral")),
            timeframe=str(data.get("timeframe", "short")),
            raw_response=resp.text,
        )

    except ImportError:
        return AIResearchSignal(
            provider="gemini", symbol=symbol, signal="hold", confidence=0,
            rationale="google-generativeai package not installed (pip install google-generativeai)",
            error="package_missing",
        )
    except Exception as exc:
        logger.warning("Gemini research failed for %s: %s", symbol, exc)
        return AIResearchSignal(
            provider="gemini", symbol=symbol, signal="hold", confidence=0,
            rationale=f"Gemini error: {str(exc)[:120]}",
            error=str(exc)[:200],
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Grok: news sentiment + short-term prediction
# ──────────────────────────────────────────────────────────────────────────────

def _grok_research(symbol: str, price: float, bars: list[dict]) -> AIResearchSignal:
    """Grok (xAI) analyzes news sentiment and predicts intraday price movement."""
    if not config.GROK_API_KEY:
        return AIResearchSignal(
            provider="grok", symbol=symbol, signal="hold", confidence=0,
            rationale="Grok API key not set (add GROK_API_KEY to .env — get key at console.x.ai)",
            error="missing_api_key",
        )
    try:
        from openai import OpenAI  # type: ignore
        client = OpenAI(api_key=config.GROK_API_KEY, base_url="https://api.x.ai/v1")

        closes = [float(b["c"]) for b in bars[-5:]]
        pct_chg = ((closes[-1] - closes[0]) / closes[0] * 100) if len(closes) >= 2 else 0.0

        prompt = (
            f"You are Grok with access to real-time market news for {symbol}.\n"
            f"Current price: ${price:.4f} | 5-bar change: {pct_chg:+.2f}%\n\n"
            "Analyze news catalysts and sentiment for an intraday trade.\n"
            "Return JSON with keys: signal, confidence (int 0-100), sentiment, "
            "news_catalyst (string), price_target (float|null), stop_loss (float|null), "
            "rationale (string), timeframe ('intraday')."
        )
        resp = client.chat.completions.create(
            model="grok-beta",
            messages=[
                {"role": "system", "content": _SYSTEM_GROK},
                {"role": "user", "content": prompt},
            ],
            max_tokens=512,
            temperature=0.2,
        )
        text = resp.choices[0].message.content or ""
        data = _parse_json_response(text)

        pt = data.get("price_target")
        sl = data.get("stop_loss")
        rr = None
        if pt and sl and price > 0:
            reward = abs(pt - price)
            risk = abs(price - sl)
            rr = round(reward / risk, 2) if risk > 0 else None

        return AIResearchSignal(
            provider="grok", symbol=symbol,
            signal=_safe_signal(data.get("signal", "hold")),
            confidence=float(data.get("confidence", 50)),
            rationale=str(data.get("rationale", "")),
            price_target=pt, stop_loss=sl, risk_reward_ratio=rr,
            sentiment=str(data.get("sentiment", "neutral")),
            timeframe=str(data.get("timeframe", "intraday")),
            news_catalyst=str(data.get("news_catalyst", "")),
            raw_response=text,
        )

    except ImportError:
        return AIResearchSignal(
            provider="grok", symbol=symbol, signal="hold", confidence=0,
            rationale="openai package not installed (pip install openai) — required for xAI Grok",
            error="package_missing",
        )
    except Exception as exc:
        logger.warning("Grok research failed for %s: %s", symbol, exc)
        return AIResearchSignal(
            provider="grok", symbol=symbol, signal="hold", confidence=0,
            rationale=f"Grok error: {str(exc)[:120]}",
            error=str(exc)[:200],
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Claude: trade execution with 5:1 risk-reward validation
# ──────────────────────────────────────────────────────────────────────────────

def _ollama_research(symbol: str, price: float, bars: list[dict]) -> AIResearchSignal:
    """Optional local Mistral/Ollama analyst for no-cloud dry-run research."""
    if not config.LOCAL_LLM_ENABLED:
        return AIResearchSignal(
            provider="ollama",
            symbol=symbol,
            signal="hold",
            confidence=0,
            rationale="Local LLM disabled (set LOCAL_LLM_ENABLED=true to use Ollama).",
            error="disabled",
        )

    closes = [float(b["c"]) for b in bars[-20:]]
    pct_chg = ((closes[-1] - closes[0]) / closes[0] * 100) if len(closes) >= 2 else 0.0
    prompt = (
        f"Analyze {symbol} for an intraday trade. Current price={price:.4f}. "
        f"20-bar change={pct_chg:+.2f}%. Recent closes={closes[-8:]}. "
        "Return only JSON with: signal, confidence, sentiment, rationale."
    )

    try:
        resp = requests.post(
            f"{config.OLLAMA_BASE_URL.rstrip('/')}/api/generate",
            json={"model": config.OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=20,
        )
        if not resp.ok:
            raise RuntimeError(f"Ollama HTTP {resp.status_code}")
        text = str(resp.json().get("response", "{}"))
        data = _parse_json_response(text)
        return AIResearchSignal(
            provider="ollama",
            symbol=symbol,
            signal=_safe_signal(data.get("signal", "hold")),
            confidence=float(data.get("confidence", 50)),
            rationale=str(data.get("rationale", "")),
            sentiment=str(data.get("sentiment", "neutral")),
            timeframe="intraday",
            raw_response=text,
        )
    except Exception as exc:
        logger.warning("Ollama research failed for %s: %s", symbol, exc)
        return AIResearchSignal(
            provider="ollama",
            symbol=symbol,
            signal="hold",
            confidence=0,
            rationale=f"Ollama error: {str(exc)[:120]}",
            error=str(exc)[:200],
        )


def _claude_execution_decision(
    symbol: str,
    price: float,
    base_signal: Signal,
    base_confidence: float,
    gemini: Optional[AIResearchSignal],
    grok: Optional[AIResearchSignal],
    indicators: dict,
    atr: float,
) -> AIResearchSignal:
    """
    Claude is the final trade executor. It:
    1. Reads Gemini and Grok research outputs
    2. Validates technical indicators from the signal engine
    3. Calculates precise entry / stop-loss / take-profit for a 5:1 ratio
    4. Returns approved=True ONLY when reward/risk >= 5.0
    """
    if not config.ANTHROPIC_API_KEY:
        return AIResearchSignal(
            provider="claude", symbol=symbol, signal="hold", confidence=0,
            rationale="Anthropic API key not set (add ANTHROPIC_API_KEY to .env)",
            error="missing_api_key",
        )
    try:
        import anthropic as anthropic_sdk  # type: ignore
        client = anthropic_sdk.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        def _fmt(sig: Optional[AIResearchSignal]) -> str:
            if not sig or sig.error:
                return "N/A"
            return (
                f"signal={sig.signal.upper()} confidence={sig.confidence:.0f} "
                f"sentiment={sig.sentiment} target={sig.price_target} sl={sig.stop_loss}"
            )

        min_ratio = config.AI_MIN_RISK_REWARD_RATIO

        prompt = (
            f"Symbol: {symbol} | Current price: ${price:.4f} | ATR(14): {atr:.4f}\n"
            f"Base signal (technical): {base_signal.upper()} (confidence {base_confidence:.0f}/100)\n\n"
            f"Gemini market research: {_fmt(gemini)}\n"
            f"Grok news intelligence: {_fmt(grok)}\n\n"
            f"Technical indicators:\n"
            f"  RSI={indicators.get('rsi', 'N/A')} | Trend={indicators.get('trend', 'N/A')} | "
            f"Vol ratio={indicators.get('volume_ratio', 'N/A')} | VWAP={indicators.get('vwap', 'N/A')}\n"
            f"  SMA fast={indicators.get('sma_fast', 'N/A')} | SMA slow={indicators.get('sma_slow', 'N/A')}\n\n"
            f"TASK: Calculate a trade setup with risk:reward >= {min_ratio:.1f}:1.\n"
            f"  • Stop-loss distance = 1.0 × ATR from entry\n"
            f"  • Take-profit distance = {min_ratio:.1f} × ATR from entry\n"
            f"  • Approve ONLY if agent consensus is clear AND ratio >= {min_ratio:.1f}\n\n"
            "Return JSON with keys: signal ('buy'|'sell'|'hold'), confidence (int 0-100), "
            "entry_price (float), stop_loss (float), take_profit (float), "
            "risk_per_unit (float), reward_per_unit (float), risk_reward_ratio (float), "
            "approved (bool), sentiment ('bullish'|'bearish'|'neutral'), "
            "agent_consensus (string), rationale (string)."
        )

        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=700,
            system=_SYSTEM_CLAUDE,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text if msg.content else ""
        data = _parse_json_response(text)

        signal = _safe_signal(data.get("signal", "hold"))
        approved = bool(data.get("approved", False))
        rr = data.get("risk_reward_ratio")

        # Enforce: if ratio < min, reject regardless of model output
        if rr is not None and float(rr) < min_ratio:
            approved = False
        if not approved:
            signal = "hold"

        return AIResearchSignal(
            provider="claude", symbol=symbol,
            signal=signal,
            confidence=float(data.get("confidence", 50)),
            rationale=str(data.get("rationale", "")),
            price_target=data.get("take_profit"),
            stop_loss=data.get("stop_loss"),
            risk_reward_ratio=float(rr) if rr is not None else None,
            sentiment=str(data.get("sentiment", "neutral")),
            timeframe="intraday",
            raw_response=text,
        )

    except ImportError:
        return AIResearchSignal(
            provider="claude", symbol=symbol, signal="hold", confidence=0,
            rationale="anthropic package not installed (pip install anthropic)",
            error="package_missing",
        )
    except Exception as exc:
        logger.warning("Claude execution decision failed for %s: %s", symbol, exc)
        return AIResearchSignal(
            provider="claude", symbol=symbol, signal="hold", confidence=0,
            rationale=f"Claude error: {str(exc)[:120]}",
            error=str(exc)[:200],
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Orchestrator
# ──────────────────────────────────────────────────────────────────────────────

def run_multi_agent_research(
    symbol: str,
    price: float,
    bars: list[dict],
    base_signal: Signal,
    base_confidence: float,
    indicators: dict,
) -> MultiAgentResearch:
    """
    Run Gemini and Grok concurrently, then pass their outputs to Claude
    for a final 5:1 risk-reward validated trade decision.

    All agents fail gracefully when their API key is missing or on error.
    """
    atr = _compute_atr(bars)
    gemini_result: Optional[AIResearchSignal] = None
    grok_result: Optional[AIResearchSignal] = None
    ollama_result: Optional[AIResearchSignal] = None

    # Market, sentiment, and optional local LLM agents run in parallel.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        gem_f = pool.submit(_gemini_research, symbol, price, bars)
        grk_f = pool.submit(_grok_research, symbol, price, bars)
        ollama_f = pool.submit(_ollama_research, symbol, price, bars)
        try:
            gemini_result = gem_f.result(timeout=20)
        except Exception as exc:
            logger.warning("Gemini future error: %s", exc)
        try:
            grok_result = grk_f.result(timeout=20)
        except Exception as exc:
            logger.warning("Grok future error: %s", exc)
        try:
            ollama_result = ollama_f.result(timeout=20)
        except Exception as exc:
            logger.warning("Ollama future error: %s", exc)

    # Claude runs after collecting the other agents' results
    claude_result = _claude_execution_decision(
        symbol=symbol, price=price, base_signal=base_signal,
        base_confidence=base_confidence, gemini=gemini_result,
        grok=grok_result, indicators=indicators, atr=atr,
    )

    # ── Weighted consensus ──────────────────────────────────────────────────
    buy_w = sell_w = total_w = 0.0

    def _accum(sig: Optional[AIResearchSignal], weight_mul: float = 1.0) -> None:
        nonlocal buy_w, sell_w, total_w
        if not sig or sig.error:
            return
        w = sig.confidence * weight_mul
        total_w += w
        if sig.signal == "buy":
            buy_w += w
        elif sig.signal == "sell":
            sell_w += w

    _accum(gemini_result, 1.0)
    _accum(grok_result, 1.0)
    _accum(ollama_result, 0.75)
    _accum(claude_result, 2.0)     # Claude gets 2× weight — it's the executor
    # Base technical signal also votes
    if base_signal != "hold":
        w = base_confidence
        total_w += w
        if base_signal == "buy":
            buy_w += w
        else:
            sell_w += w

    if total_w > 0:
        if buy_w > sell_w and buy_w / total_w >= 0.50:
            consensus: Signal = "buy"
            consensus_conf = round(buy_w / total_w * 100, 1)
        elif sell_w > buy_w and sell_w / total_w >= 0.50:
            consensus = "sell"
            consensus_conf = round(sell_w / total_w * 100, 1)
        else:
            consensus = "hold"
            consensus_conf = round(max(buy_w, sell_w) / total_w * 100, 1)
    else:
        consensus = "hold"
        consensus_conf = 0.0

    # 5:1 approval comes exclusively from Claude
    min_ratio = config.AI_MIN_RISK_REWARD_RATIO
    approved_5to1 = bool(
        claude_result
        and not claude_result.error
        and claude_result.signal != "hold"
        and claude_result.risk_reward_ratio is not None
        and claude_result.risk_reward_ratio >= min_ratio
    )
    active_agents = [
        sig for sig in (gemini_result, grok_result, ollama_result, claude_result)
        if sig and not sig.error and sig.signal != "hold"
    ]
    disagreement = len({sig.signal for sig in active_agents}) > 1
    approved_master = bool(
        approved_5to1
        and consensus != "hold"
        and consensus_conf >= config.MASTER_CONSENSUS_MIN_CONFIDENCE
        and not disagreement
    )
    if disagreement:
        rationale = "Agents disagree; NexoSignal Master Decision forced HOLD."
    elif not approved_5to1:
        rationale = "Executor did not approve the required risk/reward setup."
    elif consensus_conf < config.MASTER_CONSENSUS_MIN_CONFIDENCE:
        rationale = "Consensus confidence is below the master threshold."
    else:
        rationale = "Consensus and Executor approval aligned."
    master = MasterDecision(
        signal=consensus if approved_master else "hold",
        confidence=consensus_conf,
        approved=approved_master,
        disagreement=disagreement,
        buy_weight=round(buy_w, 2),
        sell_weight=round(sell_w, 2),
        total_weight=round(total_w, 2),
        rationale=rationale,
    )

    return MultiAgentResearch(
        symbol=symbol,
        gemini=gemini_result,
        grok=grok_result,
        claude=claude_result,
        consensus_signal=consensus,
        consensus_confidence=consensus_conf,
        approved_5to1=approved_5to1,
        entry_price=price if approved_5to1 else None,
        stop_loss=claude_result.stop_loss if claude_result else None,
        take_profit=claude_result.price_target if claude_result else None,
        risk_reward_ratio=claude_result.risk_reward_ratio if claude_result else None,
        master_decision=master,
    )


# ── NexoSignal Lens: External Intelligence Layer ──────────────────────────


def parse_insider_filings(symbol: str, max_filings: int = 10) -> list[dict]:
    """
    NexoSignal Lens — fetch recent Form 4 insider filings from SEC EDGAR (free).
    Uses the EDGAR full-text search API — no authentication required.

    Returns: list of {insider_name, title, transaction_type, shares, value, filed_at}
    """
    try:
        # Resolve ticker → CIK via EDGAR company tickers JSON (one cached call)
        tickers_resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": "NexoSignal research@nexosignal.ai"},
            timeout=10,
        )
        if not tickers_resp.ok:
            logger.warning("Lens: EDGAR tickers lookup failed %s", tickers_resp.status_code)
            return []
        tickers_data = tickers_resp.json()

        cik: Optional[str] = None
        for entry in tickers_data.values():
            if str(entry.get("ticker", "")).upper() == symbol.upper():
                cik = str(entry["cik_str"]).zfill(10)
                break

        if not cik:
            logger.info("Lens: CIK not found for %s in EDGAR", symbol)
            return []

        # Fetch recent filings for this CIK
        submissions_resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers={"User-Agent": "NexoSignal research@nexosignal.ai"},
            timeout=15,
        )
        if not submissions_resp.ok:
            return []
        submissions = submissions_resp.json()

        recent = submissions.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        filed_dates = recent.get("filingDate", [])
        accession_numbers = recent.get("accessionNumber", [])

        results: list[dict] = []
        for form, filed_date, accession in zip(forms, filed_dates, accession_numbers):
            if form != "4":
                continue
            acc_clean = accession.replace("-", "")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{accession}.txt"
            try:
                raw_resp = requests.get(
                    filing_url,
                    headers={"User-Agent": "NexoSignal research@nexosignal.ai"},
                    timeout=10,
                )
                text = raw_resp.text if raw_resp.ok else ""
            except Exception:
                text = ""

            # Extract key fields from Form 4 XML/text via simple patterns
            insider_name = _extract_tag(text, "rptOwnerName") or "Unknown"
            title = _extract_tag(text, "officerTitle") or ""
            tx_code = _extract_tag(text, "transactionCode") or ""
            tx_type = {"P": "purchase", "S": "sale", "A": "award", "F": "tax_withholding"}.get(tx_code, tx_code)
            shares_str = _extract_tag(text, "transactionShares")
            value_str = _extract_tag(text, "transactionPricePerShare")
            try:
                shares = float(shares_str) if shares_str else None
                price_per = float(value_str) if value_str else None
                value = round(shares * price_per, 2) if shares and price_per else None
            except (ValueError, TypeError):
                shares = value = None

            results.append({
                "insider_name": insider_name,
                "title": title,
                "transaction_type": tx_type,
                "shares": shares,
                "value": value,
                "filed_at": filed_date,
            })

            if len(results) >= max_filings:
                break

        return results

    except Exception as exc:
        logger.warning("Lens: insider filing parse failed for %s: %s", symbol, exc)
        return []


def _extract_tag(text: str, tag: str) -> Optional[str]:
    """Extract the content of an XML tag from Form 4 text."""
    match = re.search(rf"<{tag}[^>]*>([^<]+)</{tag}>", text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def analyze_earnings(
    symbol: str,
    period: str,
    eps_actual: float,
    eps_estimate: float,
    beat_pct: float,
    headline: str = "",
) -> dict:
    """
    NexoSignal Lens — Claude analyzes earnings quality for a single quarter.
    Returns: {lens_summary, lens_quality_score (0-100)}
    Falls back gracefully if Anthropic key is missing.
    """
    if not config.ANTHROPIC_API_KEY:
        beat_label = "beat" if beat_pct > 0 else "miss"
        return {
            "lens_summary": f"{symbol} {period} EPS {beat_label} by {abs(beat_pct):.1f}% (Claude key not set)",
            "lens_quality_score": 50.0,
        }
    try:
        import anthropic as anthropic_sdk  # type: ignore
        client = anthropic_sdk.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        prompt = (
            f"Symbol: {symbol} | Period: {period}\n"
            f"EPS actual: {eps_actual:.4f} | EPS estimate: {eps_estimate:.4f} | Beat: {beat_pct:+.2f}%\n"
            f"Headline: {headline or 'N/A'}\n\n"
            "As NexoSignal Lens, rate earnings quality 0-100 and write a 2-sentence summary.\n"
            "Return JSON: {lens_quality_score (int 0-100), lens_summary (string)}"
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system="You analyze earnings quality for algorithmic trading. Respond only with valid JSON.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text if msg.content else "{}"
        data = _parse_json_response(text)
        return {
            "lens_summary": str(data.get("lens_summary", "")),
            "lens_quality_score": float(data.get("lens_quality_score", 50)),
        }
    except Exception as exc:
        logger.warning("Lens: earnings analysis failed for %s: %s", symbol, exc)
        return {"lens_summary": f"Analysis error: {str(exc)[:80]}", "lens_quality_score": 50.0}


def analyze_annual_report(
    symbol: str,
    fiscal_year: str,
    revenue_yoy: float,
    filing_excerpt: str = "",
) -> dict:
    """
    NexoSignal Lens — Claude evaluates a 10-K filing excerpt for moat and red flags.
    Returns: {lens_summary, moat_score (0-100), red_flag_count}
    """
    if not config.ANTHROPIC_API_KEY:
        return {
            "lens_summary": f"{symbol} {fiscal_year} revenue YoY {revenue_yoy:+.1f}% (Claude key not set)",
            "moat_score": 50.0,
            "red_flag_count": 0,
        }
    try:
        import anthropic as anthropic_sdk  # type: ignore
        client = anthropic_sdk.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        excerpt = (filing_excerpt[:1500] + "…") if len(filing_excerpt) > 1500 else filing_excerpt
        prompt = (
            f"Symbol: {symbol} | Fiscal year: {fiscal_year} | Revenue YoY: {revenue_yoy:+.1f}%\n"
            f"10-K excerpt:\n{excerpt or 'Not provided'}\n\n"
            "Rate competitive moat 0-100 and count red flags (e.g., going concern, declining margins, litigation).\n"
            "Return JSON: {moat_score (int 0-100), red_flag_count (int), lens_summary (string 2 sentences)}"
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            system="You analyze annual reports for algorithmic trading. Respond only with valid JSON.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text if msg.content else "{}"
        data = _parse_json_response(text)
        return {
            "lens_summary": str(data.get("lens_summary", "")),
            "moat_score": float(data.get("moat_score", 50)),
            "red_flag_count": int(data.get("red_flag_count", 0)),
        }
    except Exception as exc:
        logger.warning("Lens: annual report analysis failed for %s: %s", symbol, exc)
        return {"lens_summary": f"Analysis error: {str(exc)[:80]}", "moat_score": 50.0, "red_flag_count": 0}


def score_sentiment(text: str) -> dict:
    """
    NexoSignal Lens — FinBERT sentiment scoring with keyword fallback.
    FinBERT is tried first; if transformers is not installed the fallback
    uses a weighted keyword dictionary (no API call, no cost).

    Returns: {label: 'positive'|'negative'|'neutral', score: float 0–1}
    """
    # ── Try FinBERT (requires: pip install transformers torch) ────────────
    try:
        from transformers import pipeline  # type: ignore
        _pipe = pipeline(
            "sentiment-analysis",
            model=config.FINBERT_MODEL_PATH,
            truncation=True,
            max_length=512,
        )
        result = _pipe(text[:512])[0]
        return {"label": result["label"].lower(), "score": round(result["score"], 4)}
    except Exception:
        pass  # fall through to keyword fallback

    # ── Keyword fallback ──────────────────────────────────────────────────
    POSITIVE = {"beat", "surged", "rallied", "upgrade", "outperform", "record", "growth",
                "profit", "raised", "strong", "buy", "bullish", "dividend", "expansion"}
    NEGATIVE = {"miss", "plunged", "downgrade", "underperform", "loss", "cut", "weak",
                "decline", "sell", "bearish", "layoff", "lawsuit", "bankruptcy", "recall"}

    words = set(re.findall(r"\b\w+\b", text.lower()))
    pos = len(words & POSITIVE)
    neg = len(words & NEGATIVE)
    total = pos + neg + 1  # avoid zero division

    if pos > neg:
        label = "positive"
        score = min(1.0, 0.5 + (pos - neg) / total * 0.5)
    elif neg > pos:
        label = "negative"
        score = min(1.0, 0.5 + (neg - pos) / total * 0.5)
    else:
        label = "neutral"
        score = 0.5

    return {"label": label, "score": round(score, 4)}
