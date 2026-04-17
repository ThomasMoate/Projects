#!/usr/bin/env python3
"""
Stochastic Volatility Model Calibration
========================================
Calibrates Heston and SABR models on real or synthetic market data.

Usage:
    python main.py --ticker SPY --mode real
    python main.py --mode synthetic
    python main.py --ticker AAPL --mode real --models heston
"""

import sys
import os
import argparse
import warnings
import numpy as np
import matplotlib
matplotlib.use("Agg")  # non-interactive backend; change to "TkAgg" or "Qt5Agg" for interactive

sys.path.insert(0, os.path.dirname(__file__))

from data.market_data import MarketDataFetcher, OptionChain
from calibration.heston_calibrator import HestonCalibrator
from calibration.sabr_calibrator import SABRCalibrator
from visualization.plotter import VolSurfacePlotter

warnings.filterwarnings("ignore")


def parse_args():
    p = argparse.ArgumentParser(description="Stochastic Vol Model Calibrator")
    p.add_argument("--ticker", default="SPY", help="Yahoo Finance ticker (default: SPY)")
    p.add_argument("--mode", choices=["real", "synthetic"], default="synthetic",
                   help="Data source: real (yfinance) or synthetic (Heston-generated)")
    p.add_argument("--models", default="both", choices=["heston", "sabr", "both"],
                   help="Which model(s) to calibrate")
    p.add_argument("--risk-free-rate", type=float, default=0.05, help="Risk-free rate")
    p.add_argument("--dividend-yield", type=float, default=0.013, help="Dividend yield")
    p.add_argument("--output-dir", default="output", help="Directory for saving plots")
    p.add_argument("--no-plots", action="store_true", help="Skip generating plots")
    p.add_argument("--verbose", action="store_true", default=True)
    return p.parse_args()


def load_chain(args) -> OptionChain:
    fetcher = MarketDataFetcher(
        risk_free_rate=args.risk_free_rate,
        dividend_yield=args.dividend_yield,
    )
    if args.mode == "synthetic":
        print("[Data] Generating synthetic option chain from known Heston parameters...")
        chain = fetcher.synthetic_chain(spot=100.0,
                                        risk_free_rate=args.risk_free_rate,
                                        dividend_yield=args.dividend_yield)
        print(f"[Data] Generated {len(chain.quotes)} synthetic quotes (S={chain.spot:.2f})")
    else:
        print(f"[Data] Fetching real option chain for {args.ticker} from Yahoo Finance...")
        chain = fetcher.fetch(args.ticker, max_expirations=6)
        raw_quotes = len(chain.quotes)
        chain = chain.filter()
        print(f"[Data] Fetched {raw_quotes} quotes → {len(chain.quotes)} after filtering "
              f"(S={chain.spot:.2f})")
    return chain


def run_calibration(chain: OptionChain, args):
    results = {}
    os.makedirs(args.output_dir, exist_ok=True)

    if args.models in ("heston", "both"):
        print("\n" + "="*55)
        print(" HESTON MODEL CALIBRATION")
        print("="*55)
        calibrator = HestonCalibrator(weight_by_vega=True)
        result_h = calibrator.calibrate(chain, popsize=12, maxiter=250, verbose=args.verbose)
        results["Heston"] = result_h

    if args.models in ("sabr", "both"):
        print("\n" + "="*55)
        print(" SABR MODEL CALIBRATION")
        print("="*55)
        calibrator = SABRCalibrator(fix_beta=0.5, n_restarts=8)
        result_s = calibrator.calibrate(chain, verbose=args.verbose)
        results["SABR"] = result_s

    return results


def generate_plots(chain: OptionChain, results: dict, args):
    plotter = VolSurfacePlotter(dark_mode=True)
    out = args.output_dir

    for name, result in results.items():
        base = f"{out}/{chain.ticker}_{name.lower()}"

        # Vol smiles per maturity
        plotter.plot_smile_fit(chain, result, save_path=f"{base}_smiles.png")

        # 3D surface
        plotter.plot_surface_3d(chain, result, save_path=f"{base}_surface3d.png")

        # Error distribution
        plotter.plot_error_distribution(result, save_path=f"{base}_errors.png")

    # Model comparison
    if len(results) > 1:
        plotter.plot_comparison(chain, results, T_target=0.25,
                                save_path=f"{out}/{chain.ticker}_comparison.png")

    print(f"\n[Plots] Saved to ./{out}/")


def main():
    args = parse_args()
    print("\n" + "="*55)
    print("  Stochastic Volatility Model Calibration")
    print(f"  Ticker: {args.ticker}  |  Mode: {args.mode}")
    print("="*55 + "\n")

    chain = load_chain(args)
    results = run_calibration(chain, args)

    if not args.no_plots:
        generate_plots(chain, results, args)

    print("\n[Done] Calibration complete.")
    return results


if __name__ == "__main__":
    main()
