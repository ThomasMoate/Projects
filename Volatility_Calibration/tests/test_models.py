"""Unit tests for Heston and SABR models."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest

from models.heston import HestonModel, HestonParams
from models.sabr import SABRModel, SABRParams
from models.utils import bs_price, bs_implied_vol


class TestBlackScholes:
    def test_call_price(self):
        price = bs_price(100, 100, 1.0, 0.05, 0.2)
        assert abs(price - 10.4506) < 0.01  # known value

    def test_put_call_parity(self):
        S, K, T, r, sigma = 100, 95, 0.5, 0.05, 0.25
        call = bs_price(S, K, T, r, sigma, option_type="call")
        put = bs_price(S, K, T, r, sigma, option_type="put")
        parity = call - put - (S - K * np.exp(-r * T))
        assert abs(parity) < 1e-8

    def test_implied_vol_roundtrip(self):
        sigma = 0.25
        price = bs_price(100, 100, 0.5, 0.05, sigma)
        iv = bs_implied_vol(price, 100, 100, 0.5, 0.05)
        assert abs(iv - sigma) < 1e-6


class TestHestonModel:
    def setup_method(self):
        self.params = HestonParams(kappa=2.0, theta=0.04, xi=0.3, rho=-0.7, v0=0.04)
        self.model = HestonModel(self.params)

    def test_feller_condition(self):
        assert self.params.is_valid()

    def test_call_price_positive(self):
        price = self.model.call_price(100, 100, 1.0, 0.05)
        assert price > 0

    def test_put_call_parity(self):
        S, K, T, r = 100, 100, 1.0, 0.05
        call = self.model.call_price(S, K, T, r)
        put = self.model.put_price(S, K, T, r)
        parity = call - put - (S - K * np.exp(-r * T))
        assert abs(parity) < 0.01

    def test_implied_vol_atm(self):
        iv = self.model.implied_vol(100, 100, 1.0, 0.05)
        assert 0.05 < iv < 1.0

    def test_vol_surface_shape(self):
        strikes = np.array([90, 95, 100, 105, 110])
        maturities = np.array([0.25, 0.5, 1.0])
        surface = self.model.vol_surface(100, strikes, maturities, 0.05)
        assert surface.shape == (3, 5)


class TestSABRModel:
    def setup_method(self):
        self.params = SABRParams(alpha=0.2, beta=0.5, rho=-0.3, nu=0.4)
        self.model = SABRModel(self.params)

    def test_params_valid(self):
        assert self.params.is_valid()

    def test_atm_vol_positive(self):
        iv = self.model.implied_vol(100, 100, 1.0)
        assert iv > 0

    def test_smile_shape(self):
        strikes = np.linspace(80, 120, 10)
        vols = self.model.smile(100, strikes, 0.5)
        assert len(vols) == 10
        assert all(v > 0 for v in vols if not np.isnan(v))

    def test_smile_symmetry_near_atm(self):
        F = 100.0
        K_itm = 95.0
        K_otm = 105.0
        v_itm = self.model.implied_vol(F, K_itm, 0.5)
        v_otm = self.model.implied_vol(F, K_otm, 0.5)
        # Smile should be roughly symmetric around ATM (within reason for beta=0.5)
        assert abs(v_itm - v_otm) < 0.05


class TestCalibration:
    def test_heston_synthetic_roundtrip(self):
        """Calibrate on synthetic data and check params are recovered approximately."""
        from data.market_data import MarketDataFetcher
        from calibration.heston_calibrator import HestonCalibrator
        from models.heston import HestonParams

        true_params = HestonParams(kappa=2.0, theta=0.04, xi=0.3, rho=-0.7, v0=0.04)
        chain = MarketDataFetcher.synthetic_chain(
            spot=100.0, risk_free_rate=0.05, true_heston_params=true_params
        )
        calibrator = HestonCalibrator()
        result = calibrator.calibrate(chain, popsize=8, maxiter=100, verbose=False)

        assert result.rmse < 0.005  # < 0.5% RMSE on synthetic data
        assert result.success


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
