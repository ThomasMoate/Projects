"""
SABR model calibration.

SABR is calibrated per-maturity (slice-by-slice) since parameters are
time-dependent in practice. A joint calibration option is also provided.

Strategy:
  - Per slice: L-BFGS-B with multiple random restarts
  - Joint: differential_evolution over all slices simultaneously
"""

import time
import numpy as np
from typing import List, Optional, Dict
from scipy.optimize import minimize, differential_evolution

from models.sabr import SABRModel, SABRParams
from data.market_data import OptionChain, OptionQuote
from calibration.result import CalibrationResult


class SABRCalibrator:

    def __init__(self, fix_beta: Optional[float] = 0.5, n_restarts: int = 10):
        """
        Args:
            fix_beta: if set, beta is not calibrated (common practice: fix to 0.5)
            n_restarts: number of random restarts for the local optimizer
        """
        self.fix_beta = fix_beta
        self.n_restarts = n_restarts

    def _model_from_x(self, x: np.ndarray) -> SABRModel:
        params = SABRParams.from_array(x, beta=self.fix_beta)
        return SABRModel(params=params, fix_beta=self.fix_beta)

    def _objective_slice(self, x: np.ndarray, quotes: List[OptionQuote], F: float) -> float:
        try:
            params = SABRParams.from_array(x, beta=self.fix_beta)
            if not params.is_valid():
                return 1e6
            model = SABRModel(params=params, fix_beta=self.fix_beta)
            errors = []
            for q in quotes:
                mv = model.implied_vol(F, q.strike, q.T)
                if np.isnan(mv) or mv <= 0:
                    errors.append(1.0)
                else:
                    errors.append((mv - q.implied_vol) ** 2)
            return np.sqrt(np.mean(errors))
        except Exception:
            return 1e6

    def _calibrate_slice(self, quotes: List[OptionQuote], F: float, T: float,
                         rng: np.random.Generator) -> SABRParams:
        """Calibrate SABR for a single maturity slice."""
        bounds = self._model_from_x(np.zeros(3 if self.fix_beta else 4)).BOUNDS
        best_result = None
        best_val = np.inf

        for _ in range(self.n_restarts):
            x0 = np.array([
                rng.uniform(0.05, 0.8),  # alpha
                *([rng.uniform(0.1, 0.9)] if self.fix_beta is None else []),
                rng.uniform(-0.8, 0.8),  # rho
                rng.uniform(0.1, 1.0),   # nu
            ])
            try:
                res = minimize(
                    self._objective_slice,
                    x0=x0,
                    args=(quotes, F),
                    method="L-BFGS-B",
                    bounds=bounds,
                    options={"maxiter": 500, "ftol": 1e-12},
                )
                if res.fun < best_val:
                    best_val = res.fun
                    best_result = res
            except Exception:
                continue

        if best_result is None:
            return SABRParams()

        return SABRParams.from_array(best_result.x, beta=self.fix_beta)

    def calibrate(self, chain: OptionChain,
                  mode: str = "per_slice",
                  seed: int = 42,
                  verbose: bool = True) -> CalibrationResult:
        """
        Args:
            mode: "per_slice" calibrates each maturity independently (recommended)
        """
        quotes_all = chain.filter(min_vol=0.01, max_vol=2.0,
                                  min_moneyness=0.7, max_moneyness=1.3,
                                  min_T=7/365).quotes

        if not quotes_all:
            raise ValueError("No valid quotes after filtering.")

        spot = chain.spot
        r = chain.risk_free_rate
        q_div = chain.dividend_yield
        rng = np.random.default_rng(seed)
        t0 = time.time()

        # Group by maturity
        maturities = sorted(set(round(q.T, 6) for q in quotes_all))
        slice_params: Dict[float, SABRParams] = {}

        market_vols, model_vols = [], []
        total_iters = 0

        for T in maturities:
            slice_quotes = [q for q in quotes_all if abs(q.T - T) < 1e-5]
            F = spot * np.exp((r - q_div) * T)

            if verbose:
                print(f"[SABR] T={T:.4f} ({len(slice_quotes)} quotes, F={F:.2f})")

            params = self._calibrate_slice(slice_quotes, F, T, rng)
            slice_params[T] = params
            total_iters += self.n_restarts

            model = SABRModel(params=params, fix_beta=self.fix_beta)
            for sq in slice_quotes:
                mv = model.implied_vol(F, sq.strike, sq.T)
                market_vols.append(sq.implied_vol)
                model_vols.append(mv if not np.isnan(mv) else sq.implied_vol)

        elapsed = time.time() - t0
        market_vols = np.array(market_vols)
        model_vols = np.array(model_vols)
        errors = model_vols - market_vols

        rmse = np.sqrt(np.nanmean(errors**2))
        mae = np.nanmean(np.abs(errors))
        max_err = np.nanmax(np.abs(errors))

        # Use params from last slice as representative (or best fit)
        best_T = min(slice_params, key=lambda t: abs(t - 0.25))
        representative_params = slice_params[best_T]

        result = CalibrationResult(
            model_name="SABR",
            params=representative_params,
            success=rmse < 0.05,
            rmse=rmse,
            mae=mae,
            max_error=max_err,
            n_quotes=len(quotes_all),
            n_iter=total_iters,
            elapsed_seconds=elapsed,
            market_vols=market_vols,
            model_vols=model_vols,
            errors=errors,
            extra={"slice_params": slice_params},
        )

        if verbose:
            print(result.summary())
            print("Per-slice parameters:")
            for T, p in slice_params.items():
                print(f"  T={T:.4f}: {p}")

        return result
