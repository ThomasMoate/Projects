"""
Heston Stochastic Volatility Model

dS = mu*S dt + sqrt(V)*S dW1
dV = kappa*(theta - V) dt + xi*sqrt(V) dW2
corr(dW1, dW2) = rho

Parameters:
    kappa : mean reversion speed
    theta : long-run variance
    xi    : vol of vol (volatility of variance)
    rho   : correlation between asset and variance processes
    v0    : initial variance

Pricing via Heston (1993) two-integral formula with the
Lord-Kahl "little trap" branch-cut fix.
"""

import numpy as np
from scipy.integrate import quad
from dataclasses import dataclass
from typing import Optional


@dataclass
class HestonParams:
    kappa: float = 1.5
    theta: float = 0.04
    xi: float = 0.3
    rho: float = -0.7
    v0: float = 0.04

    def to_array(self) -> np.ndarray:
        return np.array([self.kappa, self.theta, self.xi, self.rho, self.v0])

    @classmethod
    def from_array(cls, x: np.ndarray) -> "HestonParams":
        return cls(kappa=x[0], theta=x[1], xi=x[2], rho=x[3], v0=x[4])

    def is_valid(self) -> bool:
        feller = 2 * self.kappa * self.theta - self.xi**2
        return (
            self.kappa > 0
            and self.theta > 0
            and self.xi > 0
            and -1 < self.rho < 1
            and self.v0 > 0
            and feller > 0
        )

    def __str__(self) -> str:
        feller = 2 * self.kappa * self.theta - self.xi**2
        return (
            f"HestonParams(kappa={self.kappa:.4f}, theta={self.theta:.4f}, "
            f"xi={self.xi:.4f}, rho={self.rho:.4f}, v0={self.v0:.4f}) "
            f"[Feller={feller:.4f}]"
        )


class HestonModel:
    """
    Heston model pricing via the standard two-integral formula (Heston 1993)
    with the Lord-Kahl "little trap" numerical stability fix.

    Call = S*exp(-qT)*P1 - K*exp(-rT)*P2
    where P1, P2 are complementary risk-neutral probabilities.
    """

    BOUNDS = [
        (0.01, 20.0),   # kappa
        (1e-4, 1.0),    # theta
        (0.01, 2.0),    # xi
        (-0.99, 0.99),  # rho
        (1e-4, 1.0),    # v0
    ]

    def __init__(self, params: Optional[HestonParams] = None):
        self.params = params or HestonParams()

    def _char_func(self, phi: float, j: int, S: float, T: float,
                   r: float, q: float) -> complex:
        """
        Characteristic function f_j(phi) = E[exp(i*phi*log(S_T))].

        Uses the "little trap" formulation (Lord & Kahl 2006) to avoid
        branch-cut discontinuities for long maturities.

        j=1: P1 (stock-measure prob),  u=+1/2, b = kappa - rho*xi
        j=2: P2 (risk-neutral prob),   u=-1/2, b = kappa
        """
        p = self.params
        u = 0.5 if j == 1 else -0.5
        b = (p.kappa - p.rho * p.xi) if j == 1 else p.kappa

        iphi = 1j * phi
        d = np.sqrt((p.rho * p.xi * iphi - b) ** 2 - p.xi**2 * (2 * u * iphi - phi**2))

        # Little trap: negate d sign to avoid branch cut
        g = (b - p.rho * p.xi * iphi - d) / (b - p.rho * p.xi * iphi + d)
        exp_neg_dT = np.exp(-d * T)

        C = ((r - q) * iphi * T
             + p.kappa * p.theta / p.xi**2 * (
                 (b - p.rho * p.xi * iphi - d) * T
                 - 2 * np.log((1 - g * exp_neg_dT) / (1 - g))
             ))
        D = ((b - p.rho * p.xi * iphi - d) / p.xi**2
             * (1 - exp_neg_dT) / (1 - g * exp_neg_dT))

        return np.exp(C + D * p.v0 + iphi * np.log(S))

    def _Pj(self, j: int, S: float, K: float, T: float, r: float, q: float) -> float:
        """Compute probability P_j via numerical integration."""
        def integrand(phi):
            cf = self._char_func(phi, j, S, T, r, q)
            return np.real(np.exp(-1j * phi * np.log(K)) * cf / (1j * phi))

        result, _ = quad(integrand, 1e-6, np.inf, limit=200, epsabs=1e-8, epsrel=1e-6)
        return 0.5 + result / np.pi

    def call_price(self, S: float, K: float, T: float, r: float, q: float = 0.0) -> float:
        if T <= 0:
            return max(S * np.exp(-q * T) - K * np.exp(-r * T), 0.0)
        P1 = self._Pj(1, S, K, T, r, q)
        P2 = self._Pj(2, S, K, T, r, q)
        return max(S * np.exp(-q * T) * P1 - K * np.exp(-r * T) * P2, 0.0)

    def put_price(self, S: float, K: float, T: float, r: float, q: float = 0.0) -> float:
        """Put price via put-call parity."""
        call = self.call_price(S, K, T, r, q)
        return call - S * np.exp(-q * T) + K * np.exp(-r * T)

    def implied_vol(self, S: float, K: float, T: float, r: float, q: float = 0.0,
                    option_type: str = "call") -> float:
        """Convert Heston price to Black-Scholes implied volatility."""
        from .utils import bs_implied_vol
        if option_type == "call":
            price = self.call_price(S, K, T, r, q)
        else:
            price = self.put_price(S, K, T, r, q)
        return bs_implied_vol(price, S, K, T, r, q, option_type)

    def vol_surface(self, S: float, strikes: np.ndarray, maturities: np.ndarray,
                    r: float, q: float = 0.0) -> np.ndarray:
        """Compute implied vol surface for a grid of strikes and maturities."""
        surface = np.zeros((len(maturities), len(strikes)))
        for i, T in enumerate(maturities):
            for j, K in enumerate(strikes):
                try:
                    surface[i, j] = self.implied_vol(S, K, T, r, q)
                except Exception:
                    surface[i, j] = np.nan
        return surface
