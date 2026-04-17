"""
Strategy Backtester — moteur de simulation de stratégies actions
Stratégies: SMA Crossover, RSI, Momentum, Bollinger Bands
Métriques: CAGR, Sharpe, Sortino, Max Drawdown, Calmar, Win Rate, Profit Factor
"""

from pathlib import Path
from abc import ABC, abstractmethod

import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.io as pio

pio.renderers.default = "browser"

DATA_PATH   = Path("data")
STOCK_PATH  = DATA_PATH / "stocks"
RESULTS_PATH = DATA_PATH / "results"
RESULTS_PATH.mkdir(parents=True, exist_ok=True)


# ── STRATEGIES ────────────────────────────────────────────────────────────────

class Strategy(ABC):
    """Classe de base — retourne une Serie de signaux: 1 (long), -1 (flat/short), 0 (neutre)."""

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        ...

    @property
    def name(self) -> str:
        return self.__class__.__name__


class SMAcrossover(Strategy):
    """Croisement de deux moyennes mobiles simples."""

    def __init__(self, fast: int = 20, slow: int = 50):
        self.fast = fast
        self.slow = slow

    @property
    def name(self):
        return f"SMA {self.fast}/{self.slow}"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        fast_ma = df["Adj Close"].rolling(self.fast).mean()
        slow_ma = df["Adj Close"].rolling(self.slow).mean()
        signal = np.where(fast_ma > slow_ma, 1, -1)
        return pd.Series(signal, index=df.index, dtype=float)


class RSIStrategy(Strategy):
    """Stratégie mean-reversion basée sur le RSI."""

    def __init__(self, period: int = 14, oversold: float = 30, overbought: float = 70):
        self.period     = period
        self.oversold   = oversold
        self.overbought = overbought

    @property
    def name(self):
        return f"RSI({self.period}) {self.oversold}/{self.overbought}"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        delta = df["Adj Close"].diff()
        gain  = delta.where(delta > 0, 0.0).rolling(self.period).mean()
        loss  = (-delta.where(delta < 0, 0.0)).rolling(self.period).mean()
        rsi   = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

        signal = np.where(rsi < self.oversold, 1, np.where(rsi > self.overbought, -1, 0))
        return pd.Series(signal, index=df.index, dtype=float)


class MomentumStrategy(Strategy):
    """Momentum: long si rendement sur lookback positif, flat sinon."""

    def __init__(self, lookback: int = 126):
        self.lookback = lookback

    @property
    def name(self):
        return f"Momentum({self.lookback}j)"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        ret = df["Adj Close"].pct_change(self.lookback)
        signal = np.where(ret > 0, 1, -1)
        return pd.Series(signal, index=df.index, dtype=float)


class BollingerBandsStrategy(Strategy):
    """Retour à la moyenne via les bandes de Bollinger."""

    def __init__(self, window: int = 20, num_std: float = 2.0):
        self.window  = window
        self.num_std = num_std

    @property
    def name(self):
        return f"Bollinger({self.window}, {self.num_std}σ)"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        ma    = df["Adj Close"].rolling(self.window).mean()
        std   = df["Adj Close"].rolling(self.window).std()
        upper = ma + self.num_std * std
        lower = ma - self.num_std * std
        price = df["Adj Close"]

        signal = np.where(price < lower, 1, np.where(price > upper, -1, 0))
        return pd.Series(signal, index=df.index, dtype=float)


# ── PERFORMANCE METRICS ───────────────────────────────────────────────────────

def compute_metrics(equity: pd.Series, risk_free_rate: float = 0.01) -> dict:
    equity  = equity.dropna()
    returns = equity.pct_change().dropna()

    if len(equity) < 2 or equity.iloc[0] == 0:
        return {}

    n_years = len(equity) / 252
    cagr    = (equity.iloc[-1] / equity.iloc[0]) ** (1 / max(n_years, 1e-9)) - 1

    excess  = returns - risk_free_rate / 252
    sharpe  = np.sqrt(252) * excess.mean() / excess.std() if excess.std() > 0 else 0.0

    downside = returns[returns < 0]
    sortino  = np.sqrt(252) * excess.mean() / downside.std() if len(downside) > 1 else 0.0

    rolling_max = equity.cummax()
    drawdown    = (equity - rolling_max) / rolling_max
    max_dd      = drawdown.min()

    calmar = cagr / abs(max_dd) if max_dd != 0 else 0.0

    gross_profit = returns[returns > 0].sum()
    gross_loss   = abs(returns[returns < 0].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return {
        "Total Return":          (equity.iloc[-1] / equity.iloc[0]) - 1,
        "CAGR":                  cagr,
        "Sharpe Ratio":          sharpe,
        "Sortino Ratio":         sortino,
        "Max Drawdown":          max_dd,
        "Calmar Ratio":          calmar,
        "Win Rate":              (returns > 0).mean(),
        "Profit Factor":         profit_factor,
        "Annualized Volatility": returns.std() * np.sqrt(252),
    }


# ── BACKTESTING ENGINE ────────────────────────────────────────────────────────

class Backtester:
    """
    Moteur de backtesting long-only vectorisé.

    Paramètres
    ----------
    strategy        : instance de Strategy
    initial_capital : capital de départ en euros
    position_pct    : fraction du capital investie lors d'un signal long (0–1)
    commission      : coût par transaction (fraction de la valeur négociée)
    """

    def __init__(
        self,
        strategy: Strategy,
        initial_capital: float = 10_000.0,
        position_pct: float = 0.95,
        commission: float = 0.001,
    ):
        self.strategy        = strategy
        self.initial_capital = initial_capital
        self.position_pct    = position_pct
        self.commission      = commission

    # ── core simulation ───────────────────────────────────────────────────────

    def run(self, ticker: str, filepath: Path) -> dict:
        df = pd.read_csv(filepath, parse_dates=["Date"])
        df = df.sort_values("Date").reset_index(drop=True)

        if "Adj Close" not in df.columns:
            raise ValueError(f"Colonne 'Adj Close' manquante dans {filepath}")

        signals = self.strategy.generate_signals(df)
        df["Signal"] = signals

        cash   = self.initial_capital
        shares = 0.0
        equity_list: list[float] = []
        trades: list[dict]       = []
        prev_target = 0  # 0=flat, 1=long

        for i, row in df.iterrows():
            price  = row["Adj Close"]
            signal = row["Signal"]

            if pd.isna(price):
                equity_list.append(np.nan)
                continue

            # Signal 0 → maintenir la position courante (RSI neutre)
            target = prev_target if signal == 0 else int(max(0, signal))

            if target != prev_target:
                if target == 1 and shares == 0:   # Entrée long
                    invest    = cash * self.position_pct
                    shares    = invest / price
                    cash     -= invest * (1 + self.commission)
                    trades.append({"Date": row["Date"], "Action": "BUY",
                                   "Price": price, "Shares": shares})

                elif target == 0 and shares > 0:   # Sortie
                    proceeds = shares * price * (1 - self.commission)
                    cash    += proceeds
                    trades.append({"Date": row["Date"], "Action": "SELL",
                                   "Price": price, "Shares": shares})
                    shares = 0.0

                prev_target = target

            equity_list.append(cash + shares * price)

        # Liquidation finale si encore en position
        if shares > 0:
            last_price    = df["Adj Close"].dropna().iloc[-1]
            cash         += shares * last_price * (1 - self.commission)
            equity_list[-1] = cash

        df["Equity"]  = equity_list
        df["Returns"] = df["Equity"].pct_change()

        bh_equity = df["Adj Close"] / df["Adj Close"].dropna().iloc[0] * self.initial_capital

        return {
            "ticker":    ticker,
            "strategy":  self.strategy.name,
            "df":        df,
            "trades":    pd.DataFrame(trades),
            "metrics":   compute_metrics(df["Equity"]),
            "bh_metrics": compute_metrics(bh_equity),
            "bh_equity": bh_equity,
        }

    # ── output helpers ────────────────────────────────────────────────────────

    def print_metrics(self, result: dict) -> None:
        ticker   = result["ticker"]
        strategy = result["strategy"]
        m        = result["metrics"]
        bh       = result["bh_metrics"]
        n_trades = len(result["trades"])

        PCT = {"Total Return", "CAGR", "Max Drawdown", "Win Rate", "Annualized Volatility"}
        KEYS = ["Total Return", "CAGR", "Sharpe Ratio", "Sortino Ratio",
                "Max Drawdown", "Calmar Ratio", "Win Rate", "Profit Factor",
                "Annualized Volatility"]

        sep = "=" * 56
        print(f"\n{sep}")
        print(f"  {ticker}  |  {strategy}")
        print(sep)
        print(f"  {'Métrique':<25} {'Stratégie':>12} {'Buy & Hold':>12}")
        print(f"  {'-'*52}")
        for k in KEYS:
            sv = m.get(k, 0)
            bv = bh.get(k, 0)
            if k in PCT:
                print(f"  {k:<25} {sv:>11.1%} {bv:>11.1%}")
            else:
                print(f"  {k:<25} {sv:>12.2f} {bv:>12.2f}")
        print(f"  {'-'*52}")
        print(f"  {'Nb trades':<25} {n_trades:>12}")
        print(f"{sep}\n")

    def plot(self, result: dict) -> None:
        ticker   = result["ticker"]
        df       = result["df"]
        bh       = result["bh_equity"]
        m        = result["metrics"]
        trades   = result["trades"]
        strategy = result["strategy"]

        rolling_max = df["Equity"].cummax()
        drawdown    = (df["Equity"] - rolling_max) / rolling_max * 100

        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            row_heights=[0.7, 0.3],
            vertical_spacing=0.04,
            subplot_titles=[
                f"{ticker} — Courbe de capital ({strategy})",
                "Drawdown (%)",
            ],
        )

        # Equity curves
        fig.add_trace(go.Scatter(
            x=df["Date"], y=df["Equity"],
            name="Stratégie", line=dict(color="#2563EB", width=2)), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df["Date"], y=bh,
            name="Buy & Hold", line=dict(color="#94A3B8", width=1.5, dash="dash")), row=1, col=1)

        # Trade markers
        if not trades.empty:
            eq_map = df.set_index("Date")["Equity"].to_dict()
            buys   = trades[trades["Action"] == "BUY"]
            sells  = trades[trades["Action"] == "SELL"]

            if not buys.empty:
                fig.add_trace(go.Scatter(
                    x=buys["Date"],
                    y=[eq_map.get(d, np.nan) for d in buys["Date"]],
                    mode="markers", name="Achat",
                    marker=dict(symbol="triangle-up", size=9, color="#16A34A")), row=1, col=1)

            if not sells.empty:
                fig.add_trace(go.Scatter(
                    x=sells["Date"],
                    y=[eq_map.get(d, np.nan) for d in sells["Date"]],
                    mode="markers", name="Vente",
                    marker=dict(symbol="triangle-down", size=9, color="#DC2626")), row=1, col=1)

        # Drawdown
        fig.add_trace(go.Scatter(
            x=df["Date"], y=drawdown,
            name="Drawdown", fill="tozeroy",
            line=dict(color="#EF4444", width=1)), row=2, col=1)

        cagr   = m.get("CAGR", 0)
        sharpe = m.get("Sharpe Ratio", 0)
        max_dd = m.get("Max Drawdown", 0)

        fig.update_layout(
            title=dict(
                text=(f"<b>{ticker}</b> | {strategy} | "
                      f"CAGR: {cagr:.1%} | Sharpe: {sharpe:.2f} | "
                      f"Max DD: {max_dd:.1%}"),
                font=dict(size=14),
            ),
            template="plotly_white",
            yaxis_title="Capital (€)",
            yaxis2_title="Drawdown (%)",
            xaxis2_title="Date",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            height=680,
        )
        fig.show()

    def save(self, result: dict) -> Path:
        safe_strat = result["strategy"].replace(" ", "_").replace("/", "-")
        out = RESULTS_PATH / f"{result['ticker']}_{safe_strat}_results.csv"
        result["df"].to_csv(out, index=False)
        return out


# ── MULTI-STRATEGY COMPARISON ─────────────────────────────────────────────────

def compare_strategies(
    ticker: str,
    filepath: Path,
    strategies: list[Strategy],
    initial_capital: float = 10_000.0,
    commission: float = 0.001,
) -> None:
    """Lance plusieurs stratégies sur le même ticker et trace une comparaison."""
    fig = go.Figure()

    df_base = pd.read_csv(filepath, parse_dates=["Date"]).sort_values("Date").reset_index(drop=True)
    bh = df_base["Adj Close"] / df_base["Adj Close"].dropna().iloc[0] * initial_capital
    fig.add_trace(go.Scatter(
        x=df_base["Date"], y=bh,
        name="Buy & Hold", line=dict(color="#94A3B8", dash="dash", width=1.5)))

    COLORS = ["#2563EB", "#16A34A", "#D97706", "#9333EA", "#0891B2"]

    for i, strategy in enumerate(strategies):
        engine = Backtester(strategy, initial_capital=initial_capital, commission=commission)
        result = engine.run(ticker, filepath)
        m      = result["metrics"]
        label  = (f"{strategy.name} | CAGR {m.get('CAGR', 0):.1%} "
                  f"| Sharpe {m.get('Sharpe Ratio', 0):.2f}")
        fig.add_trace(go.Scatter(
            x=result["df"]["Date"], y=result["df"]["Equity"],
            name=label, line=dict(color=COLORS[i % len(COLORS)], width=2)))

    fig.update_layout(
        title=f"<b>{ticker}</b> — Comparaison de stratégies",
        xaxis_title="Date",
        yaxis_title="Capital (€)",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=560,
    )
    fig.show()


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    TICKERS = ["AAPL", "MSFT", "GOOGL"]

    # Stratégies disponibles
    strategies = [
        SMAcrossover(fast=20, slow=50),
        SMAcrossover(fast=50, slow=200),
        RSIStrategy(period=14, oversold=30, overbought=70),
        MomentumStrategy(lookback=126),
        BollingerBandsStrategy(window=20, num_std=2.0),
    ]

    # ── Mode 1: backtest une stratégie sur plusieurs tickers ──────────────────
    chosen_strategy = strategies[0]   # SMA 20/50
    engine = Backtester(
        strategy=chosen_strategy,
        initial_capital=10_000,
        commission=0.001,
    )

    for ticker in TICKERS:
        filepath = STOCK_PATH / f"{ticker}.csv"
        if not filepath.exists():
            print(f"[SKIP] Fichier introuvable: {filepath}")
            continue

        result = engine.run(ticker, filepath)
        engine.print_metrics(result)
        engine.plot(result)
        saved = engine.save(result)
        print(f"  → Résultats sauvegardés: {saved}")

    # ── Mode 2: comparaison de stratégies sur un seul ticker ─────────────────
    ticker   = "AAPL"
    filepath = STOCK_PATH / f"{ticker}.csv"
    if filepath.exists():
        compare_strategies(ticker, filepath, strategies, initial_capital=10_000)
