"""
Trading Agent — interactive CLI
Usage:
    python main.py
    python main.py --strategy rsi --symbols AAPL MSFT --qty 2 --interval 30
    python main.py --dry-run   (simulate only, never places real orders)
"""

import argparse
import sys
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from trading_agent import config
from trading_agent.broker import AlpacaBroker, BrokerError
from trading_agent.agent import NexoSignalAgent
from trading_agent.runtime import AgentRuntime
from trading_agent.strategy import STRATEGIES
from trading_agent.restrictions import RejectedOrder

console = Console()


def print_banner(mode: str):
    color = "yellow" if mode == "paper" else "red"
    label = "PAPER (simulation)" if mode == "paper" else "LIVE TRADING"
    console.print(Panel(
        f"[bold white]Local Stock Trading Agent[/bold white]\n"
        f"Mode: [bold {color}]{label}[/bold {color}]\n"
        f"[dim]Fund transfers are blocked. No bank withdrawals possible.[/dim]",
        box=box.DOUBLE,
        style="bold",
    ))


def show_status(broker: AlpacaBroker):
    try:
        acc = broker.get_account()
        clock = broker.get_clock()
        positions = broker.get_positions()
    except BrokerError as e:
        console.print(f"[red]Error fetching account: {e}[/red]")
        return

    # Account panel
    market_str = "[green]OPEN[/green]" if clock["is_open"] else "[red]CLOSED[/red]"
    console.print(Panel(
        f"Portfolio Value : [bold green]${float(acc['portfolio_value']):>12,.2f}[/bold green]\n"
        f"Cash            : [bold cyan]${float(acc['cash']):>12,.2f}[/bold cyan]\n"
        f"Buying Power    : [bold cyan]${float(acc['buying_power']):>12,.2f}[/bold cyan]\n"
        f"Market          : {market_str}",
        title="Account Summary",
    ))

    # Positions table
    if positions:
        table = Table(title="Open Positions", box=box.SIMPLE_HEAVY)
        table.add_column("Symbol", style="bold")
        table.add_column("Qty", justify="right")
        table.add_column("Mkt Value", justify="right")
        table.add_column("Unrealized P/L", justify="right")
        for p in positions:
            pl = float(p["unrealized_pl"])
            pl_str = f"[green]+${pl:,.2f}[/green]" if pl >= 0 else f"[red]-${abs(pl):,.2f}[/red]"
            table.add_row(p["symbol"], p["qty"], f"${float(p['market_value']):,.2f}", pl_str)
        console.print(table)
    else:
        console.print("[dim]No open positions.[/dim]")


def show_quote(broker: AlpacaBroker, symbol: str):
    try:
        price = broker.get_last_price(symbol)
        quote = broker.get_quote(symbol)
        bid = float(quote.get("bp", 0))
        ask = float(quote.get("ap", 0))
        console.print(
            f"[bold]{symbol}[/bold]  Last: [green]${price:.2f}[/green]  "
            f"Bid: ${bid:.2f}  Ask: ${ask:.2f}"
        )
    except BrokerError as e:
        console.print(f"[red]{e}[/red]")


def manual_order(broker: AlpacaBroker, side: str, symbol: str, qty: float):
    try:
        order = broker.place_market_order(symbol, side, qty)
        console.print(f"[bold green]Order submitted![/bold green]  "
                      f"id={order['id']}  status={order['status']}")
    except (RejectedOrder, BrokerError) as e:
        console.print(f"[bold red]Order failed:[/bold red] {e}")


def interactive_menu(broker: AlpacaBroker, runtime: AgentRuntime):
    agent = runtime.agent
    console.print("\n[bold]Commands:[/bold] status | quote <SYM> | buy <SYM> <qty> | sell <SYM> <qty> | "
                  "start | stop | log | quit\n")
    while True:
        try:
            raw = console.input("[bold cyan]> [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not raw:
            continue
        parts = raw.split()
        cmd = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            runtime.stop()
            break
        elif cmd == "status":
            show_status(broker)
        elif cmd == "quote" and len(parts) >= 2:
            show_quote(broker, parts[1].upper())
        elif cmd == "buy" and len(parts) >= 3:
            manual_order(broker, "buy", parts[1].upper(), float(parts[2]))
        elif cmd == "sell" and len(parts) >= 3:
            manual_order(broker, "sell", parts[1].upper(), float(parts[2]))
        elif cmd == "start":
            if runtime.is_running:
                console.print("[yellow]Agent is already running.[/yellow]")
            else:
                runtime.start()
                console.print("[green]Agent runtime started in background.[/green]")
        elif cmd == "stop":
            runtime.stop()
            console.print("[yellow]Agent stopping…[/yellow]")
        elif cmd == "log":
            for entry in agent.log[-20:]:
                color = {"info": "white", "warning": "yellow", "error": "red"}.get(entry["level"], "white")
                console.print(f"[dim]{entry['time']}[/dim] [{color}]{entry['msg']}[/{color}]")
        else:
            console.print("[dim]Unknown command.[/dim]")


def main():
    parser = argparse.ArgumentParser(description="Local Stock Trading Agent")
    parser.add_argument("--symbols", nargs="+", default=list(config.SYMBOL_WHITELIST) or ["AAPL", "MSFT"])
    parser.add_argument("--strategy", default="sma_crossover", choices=list(STRATEGIES))
    parser.add_argument("--qty", type=float, default=1, help="Shares per trade")
    parser.add_argument("--interval", type=int, default=60, help="Scan interval in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Simulate only, no real orders")
    parser.add_argument("--auto", action="store_true", help="Start agent loop immediately (no interactive menu)")
    args = parser.parse_args()

    print_banner(config.TRADING_MODE)

    # Validate credentials
    try:
        broker = AlpacaBroker()
        acc = broker.get_account()
        console.print(f"[green]Connected.[/green] Account: {acc.get('id','?')[:8]}…\n")
    except BrokerError as e:
        console.print(f"[bold red]Cannot connect to Alpaca:[/bold red] {e}")
        console.print("Make sure you've created a [bold].env[/bold] file (copy from .env.example).")
        sys.exit(1)

    agent = NexoSignalAgent(
        symbols=args.symbols,
        strategy_name=args.strategy,
        qty_per_trade=args.qty,
        poll_interval_sec=args.interval,
        dry_run=args.dry_run,
    )
    runtime = AgentRuntime(agent)

    show_status(broker)

    if args.auto:
        try:
            runtime.start()
            import time
            while runtime.is_running:
                time.sleep(5)
        except KeyboardInterrupt:
            runtime.stop()
    else:
        interactive_menu(broker, runtime)

    console.print("[dim]Goodbye.[/dim]")


if __name__ == "__main__":
    main()
