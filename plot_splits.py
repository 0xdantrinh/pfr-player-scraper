"""Plot market movement over time for betting splits.

Usage:
    python plot_splits.py --league ufl --matchup "STL@HOU"
    python plot_splits.py --league ufl              # list all games
    python plot_splits.py --league ufl --all        # plot all games in one figure
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

try:
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
except ImportError:
    print("Error: matplotlib not installed. Install with: pip install matplotlib")
    exit(1)


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
        for json_file in date_dir.glob("*.json"):
            with open(json_file) as f:
                data = json.load(f)
            matchup = data.get("matchup", json_file.stem)
            games[matchup] = data
    
    return games


def plot_game(game_data: dict, show: bool = True, save_path: str | None = None) -> None:
    """Plot market movement for a single game.
    
    Args:
        game_data: Game data dict from JSON
        show: Whether to display the plot
        save_path: Optional path to save the plot as PNG (e.g., "plots/ufl/STL@HOU.png")
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
    
    # Create figure with subplots
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(12, 10))
    fig.suptitle(f"Market Movement: {matchup}", fontsize=14, fontweight="bold")
    
    # Handle percentage
    ax1.plot(times, handle_p1, marker="o", label=f"{p1_name} Handle", linewidth=2)
    ax1.plot(times, handle_p2, marker="s", label=f"{p2_name} Handle", linewidth=2)
    ax1.set_ylabel("Handle %", fontsize=11)
    ax1.set_title("Money Wagered (Handle %)", fontsize=12, fontweight="bold")
    ax1.legend(loc="best")
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(0, 100)
    
    # Betting tickets percentage
    ax2.plot(times, bets_p1, marker="o", label=f"{p1_name} Bets", linewidth=2)
    ax2.plot(times, bets_p2, marker="s", label=f"{p2_name} Bets", linewidth=2)
    ax2.set_ylabel("Bets %", fontsize=11)
    ax2.set_title("Public Tickets (Bets %)", fontsize=12, fontweight="bold")
    ax2.legend(loc="best")
    ax2.grid(True, alpha=0.3)
    ax2.set_ylim(0, 100)
    
    # Odds movement
    if odds_p1 or odds_p2:
        # Convert odds strings to numeric values
        odds_p1_numeric = [parse_odds(x) for x in odds_p1]
        odds_p2_numeric = [parse_odds(x) for x in odds_p2]
        odds_p1_clean = [x for x in odds_p1_numeric if x is not None]
        odds_p2_clean = [x for x in odds_p2_numeric if x is not None]
        if odds_p1_clean or odds_p2_clean:
            if odds_p1_clean:
                ax3.plot(times, odds_p1_numeric, marker="o", label=f"{p1_name} Odds", linewidth=2)
            if odds_p2_clean:
                ax3.plot(times, odds_p2_numeric, marker="s", label=f"{p2_name} Odds", linewidth=2)
            ax3.axhline(y=0, color="gray", linestyle="--", alpha=0.5)
            ax3.set_ylabel("Odds", fontsize=11)
            ax3.set_xlabel("Capture Time", fontsize=11)
            ax3.set_title("Line Movement", fontsize=12, fontweight="bold")
            ax3.legend(loc="best")
            ax3.grid(True, alpha=0.3)
    
    # Format x-axis
    if isinstance(times[0], datetime):
        ax3.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        ax3.tick_params(axis="x", rotation=45)
    
    plt.tight_layout()
    
    if save_path:
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
    args = parser.parse_args()
    
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
        plot_game(game, show=True)
    elif args.all:
        # Plot all games
        for matchup, game_data in games.items():
            plot_game(game_data, show=False)
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
