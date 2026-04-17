"""Calibration result container with diagnostics."""

import numpy as np
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class CalibrationResult:
    model_name: str
    params: Any
    success: bool
    rmse: float              # root mean squared error on implied vols
    mae: float               # mean absolute error
    max_error: float
    n_quotes: int
    n_iter: int
    elapsed_seconds: float
    market_vols: np.ndarray = field(default_factory=lambda: np.array([]))
    model_vols: np.ndarray = field(default_factory=lambda: np.array([]))
    errors: np.ndarray = field(default_factory=lambda: np.array([]))
    extra: Dict = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"{'='*55}",
            f" Calibration Result — {self.model_name}",
            f"{'='*55}",
            f"  Status   : {'SUCCESS' if self.success else 'FAILED'}",
            f"  Params   : {self.params}",
            f"  RMSE     : {self.rmse*100:.4f}%",
            f"  MAE      : {self.mae*100:.4f}%",
            f"  Max err  : {self.max_error*100:.4f}%",
            f"  Quotes   : {self.n_quotes}",
            f"  Iters    : {self.n_iter}",
            f"  Time     : {self.elapsed_seconds:.2f}s",
            f"{'='*55}",
        ]
        return "\n".join(lines)

    def __repr__(self) -> str:
        return self.summary()
