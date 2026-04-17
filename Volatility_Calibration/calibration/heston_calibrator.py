"""
Heston model calibration via differential evolution + local polish.

Strategy:
  1. Global search with scipy differential_evolution
  2. Local polish with L-BFGS-B
  3. Objective: weighted RMSE on implied vols
"""

import time
import numpy as np
from typing import List, Optional
from scipy.optimize import differential_evolution, minimize

from models.heston import HestonModel, HestonParams
from models.utils import bs_implied_vol
from data.market_data import OptionChain, OptionQuote
from calibration.result import CalibrationResult


class HestonCalibrator:

    def __init__(self, use_price_error: bool = False, weight_by_vega: bool = True):
        """
        Args:
            use_price_error: if True, minimize price error instead of vol error
            weight_by_vega: weight errors by approximate vega (ATM options get more weight)
        """
        self.use_price_error = use_price_error
        self.weight_by_vega = weight_by_vega
        self._iter_count = 0

    def _weights(self, quotes: List[OptionQuote], spot: float) -> np.ndarray:
        if not self.weight_by_vega:
            return np.ones(len(quotes))
        # Approximate vega weight: higher weight near ATM
        moneyness = np.array([q.strike / spot for q in quotes])
        w = np.exp(-0.5 * ((np.log(moneyness)) / 0.2) ** 2)
        return w / w.sum() * len(w)

    def _objective(self, x: np.ndarray, quotes: List[OptionQuote],
                   spot: float, r: float, q: float, weights: np.ndarray) -> float:
        params = HestonParams.from_array(x)

        # Penalize invalid params
        if not params.is_valid():
            return 1e6 + np.sum(np.maximum(0, -x)) * 100

        model = HestonModel(params)
        errors = []

        for i, quote in enumerate(quotes):
            try:
                if self.use_price_error:
                    if quote.option_type == "call":
                        model_price = model.call_price(spot, quote.strike, quote.T, r, q)
                    else:
                        model_price = model.put_price(spot, quote.strike, quote.T, r, q)
                    err = (model_price - quote.mid_price) / (spot * 0.01)
                else:
                    model_vol = model.implied_vol(spot, quote.strike, quote.T, r, q, quote.option_type)
                    if np.isnan(model_vol) or model_vol <= 0:
                        errors.append(weights[i] * 1.0)
                        continue
                    err = model_vol - quote.implied_vol
                errors.append(weights[i] * err**2)
            except Exception:
                errors.append(weights[i] * 1.0)

        self._iter_count += 1
        return np.sqrt(np.mean(errors)) if errors else 1e6

    def calibrate(self, chain: OptionChain,
                  x0: Optional[np.ndarray] = None,
                  popsize: int = 15,
                  maxiter: int = 300,
                  tol: float = 1e-7,
                  seed: int = 42,
                  verbose: bool = True) -> CalibrationResult:

        quotes = chain.filter(min_vol=0.01, max_vol=2.0,
                              min_moneyness=0.7, max_moneyness=1.3,
                              min_T=7/365).quotes

        if not quotes:
            raise ValueError("No valid quotes after filtering.")

        spot = chain.spot
        r = chain.risk_free_rate
        q = chain.dividend_yield
        weights = self._weights(quotes, spot)

        if verbose:
            print(f"[Heston] Calibrating on {len(quotes)} quotes for {chain.ticker} (S={spot:.2f})")

        self._iter_count = 0
        t0 = time.time()

        # --- Global search ---
        bounds = HestonModel.BOUNDS
        result_de = differential_evolution(
            self._objective,
            bounds=bounds,
            args=(quotes, spot, r, q, weights),
            strategy="best1bin",
            maxiter=maxiter,
            popsize=popsize,
            tol=tol,
            seed=seed,
            polish=False,
            workers=1,
        )

        # --- Local polish ---
        result_local = minimize(
            self._objective,
            x0=result_de.x,
            args=(quotes, spot, r, q, weights),
            method="L-BFGS-B",
            bounds=bounds,
            options={"maxiter": 500, "ftol": 1e-12, "gtol": 1e-10},
        )

        best_x = result_local.x if result_local.fun < result_de.fun else result_de.x
        elapsed = time.time() - t0

        # --- Diagnostics ---
        best_params = HestonParams.from_array(best_x)
        model = HestonModel(best_params)
        market_vols, model_vols = [], []

        for quote in quotes:
            mv = model.implied_vol(spot, quote.strike, quote.T, r, q, quote.option_type)
            market_vols.append(quote.implied_vol)
            model_vols.append(mv if not np.isnan(mv) else quote.implied_vol)

        market_vols = np.array(market_vols)
        model_vols = np.array(model_vols)
        errors = model_vols - market_vols

        rmse = np.sqrt(np.nanmean(errors**2))
        mae = np.nanmean(np.abs(errors))
        max_err = np.nanmax(np.abs(errors))

        result = CalibrationResult(
            model_name="Heston",
            params=best_params,
            success=best_params.is_valid() and rmse < 0.05,
            rmse=rmse,
            mae=mae,
            max_error=max_err,
            n_quotes=len(quotes),
            n_iter=self._iter_count,
            elapsed_seconds=elapsed,
            market_vols=market_vols,
            model_vols=model_vols,
            errors=errors,
        )

        if verbose:
            print(result.summary())

        return result
