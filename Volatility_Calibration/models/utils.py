"""Black-Scholes utilities shared across models."""

import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq


def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             q: float = 0.0, option_type: str = "call") -> float:
    if T <= 0 or sigma <= 0:
        intrinsic = max(S * np.exp(-q * T) - K * np.exp(-r * T), 0.0)
        return intrinsic if option_type == "call" else max(K * np.exp(-r * T) - S * np.exp(-q * T), 0.0)

    d1 = (np.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if option_type == "call":
        return S * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    else:
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * np.exp(-q * T) * norm.cdf(-d1)


def bs_implied_vol(price: float, S: float, K: float, T: float, r: float,
                   q: float = 0.0, option_type: str = "call",
                   tol: float = 1e-6, max_iter: int = 200) -> float:
    """Compute BS implied volatility via Brent's method."""
    if T <= 0 or price <= 0:
        return np.nan

    intrinsic = max(S * np.exp(-q * T) - K * np.exp(-r * T), 0.0)
    if option_type == "put":
        intrinsic = max(K * np.exp(-r * T) - S * np.exp(-q * T), 0.0)

    if price <= intrinsic + tol:
        return np.nan

    def objective(sigma):
        return bs_price(S, K, T, r, sigma, q, option_type) - price

    try:
        return brentq(objective, 1e-6, 10.0, xtol=tol, maxiter=max_iter)
    except (ValueError, RuntimeError):
        return np.nan


def log_moneyness(S: float, K: float, T: float, r: float, q: float = 0.0) -> float:
    """Standardized log-moneyness: log(F/K) / sqrt(T)."""
    F = S * np.exp((r - q) * T)
    return np.log(F / K) / np.sqrt(T)
