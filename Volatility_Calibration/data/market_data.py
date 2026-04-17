"""
Market data fetcher using yfinance.
Retrieves option chains and computes mid-prices, implied vols.
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import List, Optional, Dict
import warnings

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    warnings.warn("yfinance not installed. Use synthetic data instead.")


@dataclass
class OptionQuote:
    strike: float
    expiry: date
    T: float          # time to expiry in years
    mid_price: float
    implied_vol: float
    option_type: str  # "call" or "put"
    bid: float = 0.0
    ask: float = 0.0
    volume: int = 0
    open_interest: int = 0


@dataclass
class OptionChain:
    ticker: str
    spot: float
    fetch_date: date
    risk_free_rate: float
    dividend_yield: float
    quotes: List[OptionQuote] = field(default_factory=list)

    def to_dataframe(self) -> pd.DataFrame:
        rows = []
        for q in self.quotes:
            rows.append({
                "strike": q.strike,
                "expiry": q.expiry,
                "T": q.T,
                "mid_price": q.mid_price,
                "implied_vol": q.implied_vol,
                "option_type": q.option_type,
                "bid": q.bid,
                "ask": q.ask,
                "volume": q.volume,
                "open_interest": q.open_interest,
                "moneyness": q.strike / self.spot,
            })
        return pd.DataFrame(rows)

    def filter(self, min_vol: float = 0.01, max_vol: float = 2.0,
               min_moneyness: float = 0.7, max_moneyness: float = 1.3,
               min_T: float = 7/365, max_T: float = 2.0,
               min_volume: int = 0) -> "OptionChain":
        filtered = [
            q for q in self.quotes
            if (min_vol <= q.implied_vol <= max_vol
                and min_moneyness <= q.strike / self.spot <= max_moneyness
                and min_T <= q.T <= max_T
                and not np.isnan(q.implied_vol)
                and q.volume >= min_volume)
        ]
        return OptionChain(
            ticker=self.ticker,
            spot=self.spot,
            fetch_date=self.fetch_date,
            risk_free_rate=self.risk_free_rate,
            dividend_yield=self.dividend_yield,
            quotes=filtered,
        )


class MarketDataFetcher:

    def __init__(self, risk_free_rate: float = 0.05, dividend_yield: float = 0.0):
        self.risk_free_rate = risk_free_rate
        self.dividend_yield = dividend_yield

    def fetch(self, ticker: str, max_expirations: int = 6) -> OptionChain:
        if not YFINANCE_AVAILABLE:
            raise RuntimeError("yfinance is required. Run: pip install yfinance")

        tk = yf.Ticker(ticker)
        hist = tk.history(period="5d")
        if hist.empty:
            raise ValueError(f"No price data found for {ticker}")

        spot = float(hist["Close"].iloc[-1])
        fetch_date = date.today()

        expirations = tk.options[:max_expirations]
        quotes: List[OptionQuote] = []

        for exp_str in expirations:
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                T = (exp_date - fetch_date).days / 365.0
                if T <= 0:
                    continue

                chain = tk.option_chain(exp_str)
                for df, opt_type in [(chain.calls, "call"), (chain.puts, "put")]:
                    for _, row in df.iterrows():
                        bid = float(row.get("bid", 0) or 0)
                        ask = float(row.get("ask", 0) or 0)
                        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else float(row.get("lastPrice", 0) or 0)
                        iv = float(row.get("impliedVolatility", np.nan) or np.nan)
                        K = float(row["strike"])
                        volume = int(row.get("volume", 0) or 0)
                        oi = int(row.get("openInterest", 0) or 0)

                        if mid <= 0:
                            continue

                        quotes.append(OptionQuote(
                            strike=K,
                            expiry=exp_date,
                            T=T,
                            mid_price=mid,
                            implied_vol=iv,
                            option_type=opt_type,
                            bid=bid,
                            ask=ask,
                            volume=volume,
                            open_interest=oi,
                        ))
            except Exception as e:
                warnings.warn(f"Skipping expiration {exp_str}: {e}")

        return OptionChain(
            ticker=ticker,
            spot=spot,
            fetch_date=fetch_date,
            risk_free_rate=self.risk_free_rate,
            dividend_yield=self.dividend_yield,
            quotes=quotes,
        )

    @staticmethod
    def synthetic_chain(spot: float = 100.0, risk_free_rate: float = 0.05,
                        dividend_yield: float = 0.0,
                        true_heston_params=None) -> OptionChain:
        """
        Generate a synthetic option chain from known Heston parameters.
        Useful for testing calibration accuracy.
        """
        from models.heston import HestonModel, HestonParams
        from models.utils import bs_implied_vol

        if true_heston_params is None:
            true_heston_params = HestonParams(kappa=2.0, theta=0.04, xi=0.3, rho=-0.7, v0=0.04)

        model = HestonModel(true_heston_params)
        maturities = [1/12, 2/12, 3/12, 6/12, 9/12, 1.0]
        moneyness_grid = np.linspace(0.80, 1.20, 9)

        quotes = []
        fetch_date = date.today()

        for T in maturities:
            for m in moneyness_grid:
                K = spot * m
                for opt_type, price_fn in [("call", model.call_price), ("put", model.put_price)]:
                    price = price_fn(spot, K, T, risk_free_rate, dividend_yield)
                    iv = bs_implied_vol(price, spot, K, T, risk_free_rate, dividend_yield, opt_type)
                    if np.isnan(iv) or iv <= 0:
                        continue
                    exp_date = date.fromordinal(fetch_date.toordinal() + int(T * 365))
                    quotes.append(OptionQuote(
                        strike=K,
                        expiry=exp_date,
                        T=T,
                        mid_price=price,
                        implied_vol=iv,
                        option_type=opt_type,
                        volume=100,
                    ))

        return OptionChain(
            ticker="SYNTHETIC",
            spot=spot,
            fetch_date=fetch_date,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            quotes=quotes,
        )
