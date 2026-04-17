"""
Visualization tools for vol surface calibration results.
"""

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
from typing import Optional, Dict

from data.market_data import OptionChain
from calibration.result import CalibrationResult


class VolSurfacePlotter:

    STYLE = {
        "figure.facecolor": "#0d1117",
        "axes.facecolor": "#161b22",
        "axes.edgecolor": "#30363d",
        "axes.labelcolor": "#e6edf3",
        "xtick.color": "#8b949e",
        "ytick.color": "#8b949e",
        "text.color": "#e6edf3",
        "grid.color": "#21262d",
        "grid.linewidth": 0.6,
        "font.family": "monospace",
    }

    def __init__(self, dark_mode: bool = True):
        self.dark_mode = dark_mode
        if dark_mode:
            plt.rcParams.update(self.STYLE)

    def plot_smile_fit(self, chain: OptionChain, result: CalibrationResult,
                       maturities_to_plot: Optional[list] = None,
                       save_path: Optional[str] = None) -> plt.Figure:
        """Plot market vs model implied vol smiles per maturity."""

        quotes = chain.filter(min_vol=0.01, max_vol=2.0,
                              min_moneyness=0.7, max_moneyness=1.3,
                              min_T=7/365).quotes

        all_T = sorted(set(round(q.T, 4) for q in quotes))
        if maturities_to_plot:
            plot_T = [t for t in all_T if any(abs(t - mt) < 0.01 for mt in maturities_to_plot)]
        else:
            plot_T = all_T[:6]

        ncols = min(3, len(plot_T))
        nrows = (len(plot_T) + ncols - 1) // ncols
        fig, axes = plt.subplots(nrows, ncols, figsize=(5 * ncols, 4 * nrows))
        axes = np.array(axes).flatten() if len(plot_T) > 1 else [axes]

        colors = {"market": "#58a6ff", "model": "#f85149"}

        for ax, T in zip(axes, plot_T):
            slice_q = [q for q in quotes if abs(q.T - T) < 1e-4]
            if not slice_q:
                continue

            spot = chain.spot
            r = chain.risk_free_rate
            q_div = chain.dividend_yield

            # Market points
            strikes = np.array([q.strike for q in slice_q])
            mkt_vols = np.array([q.implied_vol for q in slice_q])
            moneyness = strikes / spot

            ax.scatter(moneyness, mkt_vols * 100, s=20, color=colors["market"],
                       label="Market", zorder=5, alpha=0.9)

            # Model smile
            K_grid = np.linspace(strikes.min() * 0.97, strikes.max() * 1.03, 80)
            m_grid = K_grid / spot

            model_vols = self._compute_model_smile(result, chain, K_grid, T)
            if model_vols is not None:
                valid = ~np.isnan(model_vols)
                ax.plot(m_grid[valid], model_vols[valid] * 100,
                        color=colors["model"], lw=2, label=f"{result.model_name}")

            ax.set_title(f"T = {T:.3f}y ({int(T*365)}d)", fontsize=9)
            ax.set_xlabel("Moneyness K/S", fontsize=8)
            ax.set_ylabel("Impl. Vol (%)", fontsize=8)
            ax.legend(fontsize=7)
            ax.grid(True, alpha=0.3)

        for ax in axes[len(plot_T):]:
            ax.set_visible(False)

        fig.suptitle(
            f"{chain.ticker} — {result.model_name} Calibration  "
            f"(RMSE={result.rmse*100:.3f}%)",
            fontsize=12, fontweight="bold", y=1.01,
        )
        plt.tight_layout()

        if save_path:
            fig.savefig(save_path, dpi=150, bbox_inches="tight")
            print(f"Saved: {save_path}")

        return fig

    def plot_surface_3d(self, chain: OptionChain, result: CalibrationResult,
                        save_path: Optional[str] = None) -> plt.Figure:
        """3D implied vol surface: market scatter + model mesh."""

        quotes = chain.filter(min_vol=0.01, max_vol=2.0,
                              min_moneyness=0.75, max_moneyness=1.25,
                              min_T=7/365).quotes

        fig = plt.figure(figsize=(14, 7))
        gs = gridspec.GridSpec(1, 2, figure=fig)
        ax1 = fig.add_subplot(gs[0], projection="3d")
        ax2 = fig.add_subplot(gs[1], projection="3d")

        spot = chain.spot
        T_arr = np.array([q.T for q in quotes])
        K_arr = np.array([q.strike / spot for q in quotes])
        V_arr = np.array([q.implied_vol * 100 for q in quotes])

        # Market scatter
        sc = ax1.scatter(K_arr, T_arr, V_arr, c=V_arr, cmap="plasma", s=8, alpha=0.8)
        ax1.set_title("Market Implied Vol", fontsize=10)
        ax1.set_xlabel("K/S", fontsize=8)
        ax1.set_ylabel("T (years)", fontsize=8)
        ax1.set_zlabel("IV (%)", fontsize=8)
        plt.colorbar(sc, ax=ax1, shrink=0.5, label="IV (%)")

        # Model surface mesh
        K_grid = np.linspace(0.75, 1.25, 30) * spot
        T_grid = np.linspace(max(7/365, T_arr.min()), min(2.0, T_arr.max()), 20)
        KK, TT = np.meshgrid(K_grid / spot, T_grid)
        VV = np.full_like(KK, np.nan)

        for i, T in enumerate(T_grid):
            mvols = self._compute_model_smile(result, chain, K_grid, T)
            if mvols is not None:
                VV[i, :] = mvols * 100

        surf = ax2.plot_surface(KK, TT, VV, cmap="viridis", alpha=0.85, linewidth=0.2)
        ax2.set_title(f"{result.model_name} Model Surface", fontsize=10)
        ax2.set_xlabel("K/S", fontsize=8)
        ax2.set_ylabel("T (years)", fontsize=8)
        ax2.set_zlabel("IV (%)", fontsize=8)
        plt.colorbar(surf, ax=ax2, shrink=0.5, label="IV (%)")

        fig.suptitle(
            f"{chain.ticker} — Implied Volatility Surface  "
            f"[{result.model_name}, RMSE={result.rmse*100:.3f}%]",
            fontsize=12, fontweight="bold",
        )
        plt.tight_layout()

        if save_path:
            fig.savefig(save_path, dpi=150, bbox_inches="tight")
            print(f"Saved: {save_path}")

        return fig

    def plot_error_distribution(self, result: CalibrationResult,
                                save_path: Optional[str] = None) -> plt.Figure:
        """Histogram of model vs market vol errors."""
        fig, axes = plt.subplots(1, 2, figsize=(12, 4))

        errors_pct = result.errors * 100
        valid = ~np.isnan(errors_pct)

        axes[0].hist(errors_pct[valid], bins=40, color="#58a6ff", edgecolor="#0d1117",
                     alpha=0.85, density=True)
        axes[0].axvline(0, color="#f85149", lw=2, linestyle="--", label="Zero error")
        axes[0].axvline(np.mean(errors_pct[valid]), color="#ffa657", lw=1.5,
                        linestyle=":", label=f"Mean={np.mean(errors_pct[valid]):.3f}%")
        axes[0].set_xlabel("Vol Error (%)", fontsize=9)
        axes[0].set_ylabel("Density", fontsize=9)
        axes[0].set_title(f"{result.model_name} — Error Distribution", fontsize=10)
        axes[0].legend(fontsize=8)
        axes[0].grid(True, alpha=0.3)

        axes[1].scatter(result.market_vols[valid] * 100, result.model_vols[valid] * 100,
                        alpha=0.5, s=10, color="#7ee787")
        lim_min = min(result.market_vols[valid].min(), result.model_vols[valid].min()) * 100 * 0.95
        lim_max = max(result.market_vols[valid].max(), result.model_vols[valid].max()) * 100 * 1.05
        axes[1].plot([lim_min, lim_max], [lim_min, lim_max], "r--", lw=1.5, label="Perfect fit")
        axes[1].set_xlabel("Market IV (%)", fontsize=9)
        axes[1].set_ylabel("Model IV (%)", fontsize=9)
        axes[1].set_title("Model vs Market Implied Vols", fontsize=10)
        axes[1].legend(fontsize=8)
        axes[1].grid(True, alpha=0.3)

        plt.tight_layout()

        if save_path:
            fig.savefig(save_path, dpi=150, bbox_inches="tight")

        return fig

    def plot_comparison(self, chain: OptionChain,
                        results: Dict[str, CalibrationResult],
                        T_target: float = 0.25,
                        save_path: Optional[str] = None) -> plt.Figure:
        """Compare multiple models on the same smile."""
        quotes = chain.filter(min_vol=0.01, max_vol=2.0,
                              min_moneyness=0.7, max_moneyness=1.3,
                              min_T=7/365).quotes

        all_T = sorted(set(round(q.T, 4) for q in quotes))
        T = min(all_T, key=lambda t: abs(t - T_target))

        slice_q = [q for q in quotes if abs(q.T - T) < 1e-4]
        spot = chain.spot
        strikes = np.array([q.strike for q in slice_q])
        K_grid = np.linspace(strikes.min() * 0.97, strikes.max() * 1.03, 100)

        fig, ax = plt.subplots(figsize=(10, 5))
        mkt_vols = np.array([q.implied_vol for q in slice_q])
        ax.scatter(strikes / spot, mkt_vols * 100, s=30, color="white",
                   zorder=10, label="Market", edgecolors="#444", linewidths=0.5)

        palette = ["#58a6ff", "#f85149", "#56d364", "#ffa657"]
        for (name, result), color in zip(results.items(), palette):
            mvols = self._compute_model_smile(result, chain, K_grid, T)
            if mvols is not None:
                valid = ~np.isnan(mvols)
                ax.plot(K_grid[valid] / spot, mvols[valid] * 100, lw=2.5,
                        color=color, label=f"{name} (RMSE={result.rmse*100:.3f}%)")

        ax.set_xlabel("Moneyness K/S", fontsize=10)
        ax.set_ylabel("Implied Vol (%)", fontsize=10)
        ax.set_title(f"{chain.ticker} — Model Comparison  T={T:.3f}y ({int(T*365)}d)", fontsize=11)
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)
        plt.tight_layout()

        if save_path:
            fig.savefig(save_path, dpi=150, bbox_inches="tight")

        return fig

    def _compute_model_smile(self, result: CalibrationResult, chain: OptionChain,
                             strikes: np.ndarray, T: float) -> Optional[np.ndarray]:
        """Helper: compute model implied vols for a given slice."""
        spot = chain.spot
        r = chain.risk_free_rate
        q_div = chain.dividend_yield
        F = spot * np.exp((r - q_div) * T)

        try:
            if result.model_name == "Heston":
                from models.heston import HestonModel
                model = HestonModel(result.params)
                return np.array([model.implied_vol(spot, K, T, r, q_div) for K in strikes])
            elif result.model_name == "SABR":
                from models.sabr import SABRModel
                slice_params = result.extra.get("slice_params", {})
                if slice_params:
                    best_T = min(slice_params.keys(), key=lambda t: abs(t - T))
                    params = slice_params[best_T]
                else:
                    params = result.params
                model = SABRModel(params=params)
                return np.array([model.implied_vol(F, K, T) for K in strikes])
        except Exception:
            return None
