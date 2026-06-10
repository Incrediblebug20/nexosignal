from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_agent import config
from trading_agent.signal_engine import build_alpha_picks, build_ranked_predictions
from trading_agent.storage import list_signal_events, list_trade_events, signal_summary, trade_summary


def _sample_bars(symbol: str, base: float = 100.0) -> list[dict]:
    bars = []
    for idx in range(80):
        close = base + (idx * 0.08) + (((idx % 7) - 3) * 0.18)
        bars.append({"o": close - 0.12, "h": close + 0.42, "l": close - 0.38, "c": close, "v": 2_000_000 + idx * 5000})
    return bars


def main() -> None:
    signals = list_signal_events(500)
    trades = list_trade_events(500)
    sig_summary = signal_summary()
    tr_summary = trade_summary()

    approved = int(sig_summary.get("approved") or 0)
    total = int(sig_summary.get("total") or 0)
    approval_rate = (approved / total * 100) if total else 0

    print("Signal intelligence")
    print("-------------------")
    print(f"signals:       {total}")
    print(f"approved:      {approved}")
    print(f"approval_rate: {approval_rate:.1f}%")
    print(f"avg_conf:      {float(sig_summary.get('avg_confidence') or 0):.1f}")
    print()

    print("Signals by final action")
    print("-----------------------")
    for action, count in Counter(s["final_signal"] for s in signals).most_common():
        print(f"{action:>8}: {count}")
    print()

    print("Trade capture")
    print("-------------")
    print(f"events:   {int(tr_summary.get('total') or 0)}")
    print(f"accepted: {int(tr_summary.get('accepted') or 0)}")
    print(f"failed:   {int(tr_summary.get('failed') or 0)}")
    print(f"dry_runs: {int(tr_summary.get('dry_runs') or 0)}")
    print()

    symbol_bars = {
        symbol: _sample_bars(symbol, 100 + idx * 35)
        for idx, symbol in enumerate(config.DEFAULT_DASHBOARD_SYMBOLS[:8])
    }

    print("Top 3 daily predictions (dry-run simulation)")
    print("--------------------------------------------")
    for p in build_ranked_predictions(symbol_bars, top_n=3, market_open=True):
        print(
            f"#{p.rank} {p.symbol:>8} {p.predicted_direction:>4} "
            f"conf={p.confluence_score:.0f} rr={p.risk_reward_ratio:.1f}:1 "
            f"entry={p.expected_entry:.2f} stop={p.target_stop_loss:.2f} "
            f"target={p.target_take_profit:.2f} allowed={p.trade_allowed} "
            f"{p.blocked_reason or ''}"
        )
    print()

    print("Top 5 Alpha Picks")
    print("-----------------")
    for p in build_alpha_picks(symbol_bars, top_n=5):
        print(
            f"{p.symbol:>8} score={p.score:.0f} conf={p.confidence_score:.0f} "
            f"atr={p.atr:.2f} rvol={p.relative_volume:.2f} "
            f"action={p.action_recommendation} {p.rejection_reason or ''}"
        )
    print()

    print("Recent approved signals")
    print("-----------------------")
    for s in [x for x in signals if x["approved"]][:10]:
        print(
            f"{str(s['created_at'])[:19]} {s['symbol']:>6} "
            f"{s['final_signal']:>4} conf={float(s['confidence']):.0f} "
            f"price={float(s['price']):.2f} reason={s['reason']}"
        )


if __name__ == "__main__":
    main()
