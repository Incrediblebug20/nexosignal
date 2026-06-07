from collections import Counter

from trading_agent.storage import list_signal_events, list_trade_events, signal_summary, trade_summary


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
