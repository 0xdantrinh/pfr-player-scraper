"""Plot market movement over time for betting splits.

Usage:
    python plot_splits.py --league ufl --matchup "STL@HOU"
    python plot_splits.py --league ufl              # list all games
    python plot_splits.py --league ufl --all        # plot all games in one figure
    python plot_splits.py --league ufl --sync-s3    # sync latest from S3 first
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import boto3
from dotenv import load_dotenv

load_dotenv()

try:
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
except ImportError:
    print("Error: matplotlib not installed. Install with: pip install matplotlib")
    exit(1)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
SPLITS_S3_BUCKET = os.getenv("SPLITS_S3_BUCKET")


def parse_odds(odds_str: str | int | None) -> int | None:
    """Convert odds string like '+180' or '-135' to numeric value."""
    if odds_str is None:
        return None
    if isinstance(odds_str, int):
        return odds_str
    try:
        return int(str(odds_str).replace("+", "").replace("−", "-").replace("–", "-"))
    except (ValueError, AttributeError):
        return None


def sync_splits_from_s3(league: str) -> None:
    """Sync splits files from S3 to local splits directory."""
    if not SPLITS_S3_BUCKET:
        print("Warning: SPLITS_S3_BUCKET not configured, skipping S3 sync")
        return

    try:
        s3 = boto3.client("s3", region_name=AWS_REGION)
        splits_dir = Path("splits") / league
        splits_dir.mkdir(parents=True, exist_ok=True)

        # List all objects in the S3 bucket for this league
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=SPLITS_S3_BUCKET,
            Prefix=f"{league}/"
        )

        count = 0
        for page in pages:
            if "Contents" not in page:
                continue
            for obj in page["Contents"]:
                key = obj["Key"]
                # Parse: {league}/{YYYY-MM-DD}/{filename}.json
                parts = key.split("/")
                if len(parts) >= 3:
                    date_part = parts[1]
                    filename = parts[2]
                    local_path = splits_dir / date_part / filename
                    local_path.parent.mkdir(parents=True, exist_ok=True)

                    # Download file
                    s3.download_file(SPLITS_S3_BUCKET, key, str(local_path))
                    count += 1

        print(f"Synced {count} files from S3 for league: {league}")
    except Exception as e:
        print(f"Error syncing from S3: {e}")
        raise


def find_game_files(league: str) -> dict:
    """Find all game files for a league and return as dict."""
    splits_dir = Path("splits") / league
    if not splits_dir.exists():
        print(f"No splits directory found for league: {league}")
        return {}

    games = {}
    for date_dir in splits_dir.iterdir():
        if not date_dir.is_dir():
            continue
        date_str = date_dir.name
        for json_file in date_dir.glob("*.json"):
            with open(json_file) as f:
                data = json.load(f)
            data["_game_date"] = date_str
            matchup = data.get("matchup", json_file.stem)
            games[matchup] = data

    return games


def plot_game(game_data: dict, league: str = "", show: bool = True, save_path: str | None = None) -> None:
    """Plot market movement for a single game.

    Args:
        game_data: Game data dict from JSON
        league: League name for organizing plots by date
        show: Whether to display the plot
        save_path: Optional path to save the plot as PNG
    """
    matchup = game_data.get("matchup", "Game")
    p1_name = game_data.get("participant1_slug", "P1")
    p2_name = game_data.get("participant2_slug", "P2")
    
    # Extract history arrays
    handle_p1 = game_data.get("participant1_handle_pct_history", [])
    handle_p2 = game_data.get("participant2_handle_pct_history", [])
    bets_p1 = game_data.get("participant1_bets_pct_history", [])
    bets_p2 = game_data.get("participant2_bets_pct_history", [])
    odds_p1 = game_data.get("participant1_odds_history", [])
    odds_p2 = game_data.get("participant2_odds_history", [])
    timestamps = game_data.get("captured_at_history", [])
    
    if not handle_p1:
        if save_path is None:
            print(f"No history data for {matchup}")
        return
    
    # Parse timestamps
    try:
        times = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in timestamps]
    except Exception:
        times = list(range(len(handle_p1)))
    
    # Total and spread history arrays (may be absent for older records)
    tot_over_bets    = game_data.get("total_over_bets_pct_history", [])
    tot_under_bets   = game_data.get("total_under_bets_pct_history", [])
    tot_over_handle  = game_data.get("total_over_handle_pct_history", [])
    tot_under_handle = game_data.get("total_under_handle_pct_history", [])
    sp_p1_bets       = game_data.get("spread_participant1_bets_pct_history", [])
    sp_p2_bets       = game_data.get("spread_participant2_bets_pct_history", [])
    sp_p1_handle     = game_data.get("spread_participant1_handle_pct_history", [])
    sp_p2_handle     = game_data.get("spread_participant2_handle_pct_history", [])

    has_total  = any(v is not None for v in tot_over_bets)
    has_spread = any(v is not None for v in sp_p1_bets)

    # Dynamic layout: 3 rows (ML handle, ML bets, ML odds)
    # + 2 rows for total + 2 rows for spread when data present
    extra = (2 if has_total else 0) + (2 if has_spread else 0)
    n_rows = 3 + extra
    fig, axes = plt.subplots(n_rows, 1, figsize=(12, 4 + n_rows * 2.5))
    fig.suptitle(f"Market Movement: {matchup}", fontsize=14, fontweight="bold")
    ax1, ax2, ax3 = axes[0], axes[1], axes[2]
    extra_axes = list(axes[3:])

    def _plot_pct_ax(ax, t, series1, series2, label1, label2, title):
        # Trim times to match each series length to avoid shape mismatch
        s1 = [v for v in (series1 or []) if v is not None]
        s2 = [v for v in (series2 or []) if v is not None]
        t1 = t[:len(s1)]
        t2 = t[:len(s2)]
        if s1:
            ax.plot(t1, s1, marker="o", label=label1, linewidth=2, alpha=0.8)
            for tv, v in zip(t1, s1):
                ax.text(tv, v + 2.5, f"{v}%", fontsize=8, ha="center", va="bottom", color="C0")
        if s2:
            ax.plot(t2, s2, marker="s", label=label2, linewidth=2, alpha=0.8, linestyle="--")
            for tv, v in zip(t2, s2):
                ax.text(tv, v - 2.5, f"{v}%", fontsize=8, ha="center", va="top", color="C1")
        ax.axhline(y=30, color="gray", linestyle="--", linewidth=1.5, alpha=0.5, label="30%")
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.set_ylim(0, 100)
        ax.legend(loc="best", fontsize=9)
        ax.grid(True, alpha=0.3)

    # ── Row 1: Moneyline Handle ───────────────────────────────────────────────
    _plot_pct_ax(ax1, times, handle_p1, handle_p2,
                 f"{p1_name} Handle", f"{p2_name} Handle", "Moneyline — Handle %")
    ax1.set_ylabel("Handle %", fontsize=10)

    # ── Row 2: Moneyline Bets ─────────────────────────────────────────────────
    _plot_pct_ax(ax2, times, bets_p1, bets_p2,
                 f"{p1_name} Bets", f"{p2_name} Bets", "Moneyline — Bets %")
    ax2.set_ylabel("Bets %", fontsize=10)

    # ── Row 3: Moneyline Odds ─────────────────────────────────────────────────
    if odds_p1 or odds_p2:
        odds_p1_num = [parse_odds(x) for x in odds_p1]
        odds_p2_num = [parse_odds(x) for x in odds_p2]
        if any(x is not None for x in odds_p1_num):
            ax3.plot(times, odds_p1_num, marker="o", label=f"{p1_name} Odds", linewidth=2)
        if any(x is not None for x in odds_p2_num):
            ax3.plot(times, odds_p2_num, marker="s", label=f"{p2_name} Odds", linewidth=2, linestyle="--")
        ax3.axhline(y=0, color="gray", linestyle="--", alpha=0.5)
        ax3.set_ylabel("Odds", fontsize=10)
        ax3.set_title("Moneyline — Line Movement", fontsize=11, fontweight="bold")
        ax3.legend(loc="best", fontsize=9)
        ax3.grid(True, alpha=0.3)

    # ── Rows 4-5: Total (if present) ─────────────────────────────────────────
    if has_total and len(extra_axes) >= 2:
        t_times = times[:len(tot_over_bets)]
        _plot_pct_ax(extra_axes[0], t_times,
                     tot_over_handle, tot_under_handle,
                     "Over Handle", "Under Handle", "Total — Handle %")
        extra_axes[0].set_ylabel("Handle %", fontsize=10)
        _plot_pct_ax(extra_axes[1], t_times,
                     tot_over_bets, tot_under_bets,
                     "Over Bets", "Under Bets", "Total — Bets %")
        extra_axes[1].set_ylabel("Bets %", fontsize=10)
        extra_axes = extra_axes[2:]

    # ── Rows 6-7: Spread/Runline/Puckline (if present) ───────────────────────
    if has_spread and len(extra_axes) >= 2:
        s_times = times[:len(sp_p1_bets)]
        _plot_pct_ax(extra_axes[0], s_times,
                     sp_p1_handle, sp_p2_handle,
                     f"{p1_name} Handle", f"{p2_name} Handle", "Spread/Runline — Handle %")
        extra_axes[0].set_ylabel("Handle %", fontsize=10)
        _plot_pct_ax(extra_axes[1], s_times,
                     sp_p1_bets, sp_p2_bets,
                     f"{p1_name} Bets", f"{p2_name} Bets", "Spread/Runline — Bets %")
        extra_axes[1].set_ylabel("Bets %", fontsize=10)

    # Format x-axis on bottom subplot
    bottom_ax = axes[-1]
    if isinstance(times[0], datetime):
        bottom_ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        bottom_ax.tick_params(axis="x", rotation=45)
    bottom_ax.set_xlabel("Capture Time", fontsize=10)

    plt.tight_layout(rect=[0, 0, 1, 0.97])

    if save_path or league:
        if not save_path and league:
            game_date = game_data.get("_game_date", "")
            save_path = f"plots/{league}/{game_date}/{matchup}.png"
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        plt.savefig(save_path, dpi=100, bbox_inches="tight")
        plt.close()
    elif show:
        plt.show()
    else:
        plt.close()


def main():
    parser = argparse.ArgumentParser(description="Plot betting splits market movement")
    parser.add_argument("--league", default="ufl", help="League (ufl, ufc, nfl, etc.)")
    parser.add_argument("--matchup", help="Specific matchup to plot (e.g., 'STL@HOU')")
    parser.add_argument("--all", action="store_true", help="Plot all games (one per window)")
    parser.add_argument("--sync-s3", action="store_true", help="Sync latest splits from S3 before plotting")
    args = parser.parse_args()

    if args.sync_s3:
        sync_splits_from_s3(args.league)

    games = find_game_files(args.league)
    
    if not games:
        print(f"No games found for league: {args.league}")
        return
    
    if args.matchup:
        # Find matching game
        matching = [m for m in games.keys() if args.matchup.lower() in m.lower()]
        if not matching:
            print(f"No games matching '{args.matchup}'")
            print(f"Available games: {list(games.keys())}")
            return
        game = games[matching[0]]
        plot_game(game, league=args.league, show=True)
    elif args.all:
        # Plot all games
        for matchup, game_data in games.items():
            plot_game(game_data, league=args.league, show=False)
        plt.show()
    else:
        # List available games
        print(f"Available games for {args.league}:")
        for matchup in sorted(games.keys()):
            game = games[matchup]
            p1_odds = game.get("participant1_odds")
            p2_odds = game.get("participant2_odds")
            mr_vegas = game.get("mr_vegas_flag")
            flag_str = " [MR VEGAS]" if mr_vegas else ""
            print(f"  {matchup} ({p1_odds} / {p2_odds}){flag_str}")
        print(f"\nTo plot a game, use: python plot_splits.py --league {args.league} --matchup <name>")


if __name__ == "__main__":
    main()
