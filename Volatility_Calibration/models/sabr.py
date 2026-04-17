"""
SABR Stochastic Volatility Model (Hagan et al. 2002)

dF = alpha * F^beta * dW1
d(alpha) = nu * alpha * dW2
corr(dW1, dW2) = rho

Parameters:
    alpha : initial volatility (stochastic)
    beta  : CEV exponent in [0, 1]  (0=normal, 1=log-normal)
    rho   : correlation
    nu    : vol of vol

Implied vol is computed via the Hagan 2002 approximation formula.
"""

import numpy as np
from dataclasses import dataclass
from typing import Optional


@dataclass
class SABRParams:
    alpha: float = 0.2
    beta: float = 0.5
    rho: float = -0.3
    nu: float = 0.4

    def to_array(self) -> np.ndarray:
        return np.array([self.alpha, self.beta, self.rho, self.nu])

    @classmethod
    def from_array(cls, x: np.ndarray, beta: Optional[float] = None) -> "SABRParams":
        if beta is not None:
            return cls(alpha=x[0], beta=beta, rho=x[1], nu=x[2])
        return cls(alpha=x[0], beta=x[1], rho=x[2], nu=x[3])

    def is_valid(self) -> bool:
        return (
            self.alpha > 0
            and 0 <= self.beta <= 1
            and -1 < self.rho < 1
            and self.nu > 0
        )

    def __str__(self) -> str:
        return (
            f"SABRParams(alpha={self.alpha:.4f}, beta={self.beta:.4f}, "
            f"rho={self.rho:.4f}, nu={self.nu:.4f})"
        )


class SABRModel:
    """
    SABR model with Hagan 2002 implied vol approximation.
    Beta can be fixed or calibrated.
    """

    def __init__(self, params: Optional[SABRParams] = None, fix_beta: Optional[float] = None):
        self.params = params or SABRParams()
        self.fix_beta = fix_beta  # if set, beta is not calibrated

    @property
    def BOUNDS(self):
        if self.fix_beta is not None:
            return [(1e-4, 2.0), (-0.99, 0.99), (1e-4, 2.0)]   # alpha, rho, nu
        return [(1e-4, 2.0), (0.0, 1.0), (-0.99, 0.99), (1e-4, 2.0)]  # alpha, beta, rho, nu

    def implied_vol(self, F: float, K: float, T: float) -> float:
        """
        Hagan 2002 SABR implied normal/lognormal vol approximation.
        Returns Black-Scholes (lognormal) implied vol.
        """
        p = self.params
        alpha, beta, rho, nu = p.alpha, p.beta, p.rho, p.nu

        if T <= 0:
            return np.nan

        # ATM case
        if abs(F - K) < 1e-10:
            FK_mid = F
            term1 = alpha / (FK_mid ** (1 - beta))
            factor1 = 1 + (
                ((1 - beta)**2 / 24) * alpha**2 / FK_mid**(2 - 2*beta)
                + (rho * beta * nu * alpha) / (4 * FK_mid**(1 - beta))
                + (2 - 3*rho**2) * nu**2 / 24
            ) * T
            return term1 * factor1

        FK = F * K
        FK_beta = FK ** ((1 - beta) / 2)
        log_FK = np.log(F / K)

        z = (nu / alpha) * FK_beta * log_FK
        x_z = np.log(
            (np.sqrt(1 - 2*rho*z + z**2) + z - rho) / (1 - rho)
        )

        if abs(x_z) < 1e-10:
            z_over_xz = 1.0
        else:
            z_over_xz = z / x_z

        # Leading term
        A = alpha / (
            FK_beta * (
                1
                + (1 - beta)**2 / 24 * log_FK**2
                + (1 - beta)**4 / 1920 * log_FK**4
            )
        )

        # Correction term
        B = 1 + (
            (1 - beta)**2 / 24 * alpha**2 / FK_beta**2
            + rho * beta * nu * alpha / (4 * FK_beta)
            + (2 - 3*rho**2) / 24 * nu**2
        ) * T

        return A * z_over_xz * B

    def vol_surface(self, F: float, strikes: np.ndarray, maturities: np.ndarray) -> np.ndarray:
        """Compute implied vol surface for a grid of strikes and maturities."""
        surface = np.zeros((len(maturities), len(strikes)))
        for i, T in enumerate(maturities):
            for j, K in enumerate(strikes):
                try:
                    surface[i, j] = self.implied_vol(F, K, T)
                except Exception:
                    surface[i, j] = np.nan
        return surface

    def smile(self, F: float, strikes: np.ndarray, T: float) -> np.ndarray:
        """Implied vol smile for a single maturity."""
        return np.array([self.implied_vol(F, K, T) for K in strikes])
