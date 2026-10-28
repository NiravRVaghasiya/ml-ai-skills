"""Executes the python blocks of time-series/SKILL.md.

Real libraries throughout: pandas, statsmodels (STL, adfuller), pmdarima
(auto_arima), prophet (its wheel bundles the compiled Stan model, so no
download), scikit-learn. The only input the skill expects from the user — a
``sales.csv`` with ``date``/``sales`` columns — is a small synthetic daily series
written into the harness's temporary working directory.

What is not run verbatim, and why:

* block 3 (``auto_arima``): ``replace=`` caps the order search
  (``start_p/start_q=0``, ``max_p/max_q/max_P/max_Q=1``). An uncapped seasonal stepwise search measured
  20-50 s per fit on the Windows dev box; the search budget is not under test.
* block 5 (walk-forward): ``pm`` is a recording fake injected via ``namespace=``
  so fold arithmetic, leakage and the metric are checked in milliseconds (one
  real ``auto_arima`` per fold would cost ~20 s each). The real ``auto_arima``
  is exercised by block 3.

Regression tests (each failed against the pre-fix skill, observed 2026-10-27):
a >3-day gap was partially interpolated and left NaNs that make STL return an
all-NaN decomposition silently; Prophet had ``yearly_seasonality=True`` on <2
years of data; the walk-forward loop dropped the last full fold, reported MAPE
~1e15 on a series with zero-sales days, and returned NaN (no error) when the
series was too short for a single fold.
"""
from __future__ import annotations

import logging
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace

from examples import harness

SLUG = "time-series"
# Block indices (document order): 0 load/regularize, 1 STL, 2 ADF, 3 auto_arima,
# 4 Prophet, 5 walk-forward backtest.
CAP_SEARCH = {3: {"stepwise=True,": "stepwise=True, start_p=0, start_q=0, max_p=1, max_q=1, max_P=1, max_Q=1,"}}


def _write_sales(workdir: Path, n=120, drop=None, zero_every=None, periodic=False, seed=0):
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    t = np.arange(n)
    weekly = 10 * np.sin(2 * np.pi * t / 7)
    y = 50 + weekly if periodic else 50 + 0.1 * t + weekly + rng.normal(0, 2, n)
    if zero_every:
        y[::zero_every] = 0.0
    df = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=n, freq="D"), "sales": y})
    if drop:
        start, length = drop
        df = df.drop(df.index[start:start + length])
    df.to_csv(workdir / "sales.csv", index=False)
    return df


class _RecordingArima:
    """Fake fitted pmdarima model: forecasts the last training value (a naive model)."""

    def __init__(self, train):
        self.train = train

    def predict(self, n_periods):
        import numpy as np
        import pandas as pd

        idx = pd.date_range(self.train.index[-1] + pd.Timedelta(days=1), periods=n_periods, freq="D")
        return pd.Series(np.repeat(self.train.iloc[-1], n_periods), index=idx)


def _fake_pm(calls):
    def auto_arima(y, **kwargs):
        calls.append(y)
        return _RecordingArima(y)
    return SimpleNamespace(auto_arima=auto_arima)


@harness.requires("numpy", "pandas", "statsmodels", "pmdarima", "prophet", "sklearn")
class TestTimeSeriesSkill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name in ("cmdstanpy", "prophet"):
            logging.getLogger(name).setLevel(logging.WARNING)

    def _run(self, indices, *, namespace=None, replace=None, **data):
        with tempfile.TemporaryDirectory() as tmp:
            _write_sales(Path(tmp), **data)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # statsmodels convergence chatter
                return harness.run_blocks(SLUG, indices, namespace=namespace, replace=replace, workdir=Path(tmp))

    def test_workflow_blocks_execute_end_to_end(self):
        ns = self._run([0, 1, 2, 3, 4], replace=CAP_SEARCH, n=120, drop=(40, 2))
        df = ns["df"]
        self.assertEqual(df.index.freqstr, "D")
        self.assertEqual(len(df), 120)
        self.assertFalse(df["sales"].isna().any(), "2-day gap must be interpolated")
        self.assertFalse(ns["trend"].isna().any())
        self.assertIn(ns["d"], (0, 1))
        self.assertEqual(len(ns["forecast"]), 14)
        self.assertEqual(ns["conf_int"].shape, (14, 2))
        self.assertEqual(len(ns["fcst"]), 120 + 14)
        self.assertFalse(ns["fcst"]["yhat"].isna().any())

    def test_prophet_does_not_force_yearly_seasonality_on_short_history(self):
        # 120 days < 2 years: forcing yearly seasonality fits noise (the skill's own Gotcha).
        ns = self._run([0, 4], n=120)
        self.assertNotIn("yearly", ns["m"].seasonalities)
        self.assertIn("weekly", ns["m"].seasonalities)

    def test_short_gap_filled_linearly(self):
        ns = self._run([0], n=60, drop=(20, 3), periodic=True)
        s = ns["df"]["sales"]
        self.assertFalse(s.isna().any())
        before, after = s.iloc[19], s.iloc[23]
        self.assertAlmostEqual(s.iloc[21], before + (after - before) * 2 / 4)

    def test_long_gap_is_rejected_not_partially_filled(self):
        with self.assertRaises(ValueError) as cm:
            self._run([0], n=60, drop=(20, 5))
        self.assertIn("missing", str(cm.exception))

    def test_stl_silently_returns_all_nan_when_input_has_nan(self):
        # Library behaviour the step-1 guard defends against (statsmodels 0.14.6).
        import numpy as np
        import pandas as pd
        from statsmodels.tsa.seasonal import STL

        y = pd.Series(np.sin(np.arange(60) * 2 * np.pi / 7) + 5.0,
                      index=pd.date_range("2025-01-01", periods=60, freq="D"))
        y.iloc[10] = np.nan
        res = STL(y, period=7, robust=True).fit()
        self.assertTrue(np.isnan(res.trend).all())

    def test_walk_forward_covers_every_full_fold_without_leakage(self):
        calls = []
        # 188 = 90 initial + 7 folds * 14: the last full fold ends exactly at the series end.
        ns = self._run([0, 5], namespace={"pm": _fake_pm(calls)}, n=188)
        self.assertEqual(len(calls), 7)
        self.assertEqual(len(ns["model_mae"]), 7)
        df = ns["df"]
        for k, train in enumerate(calls):
            cutoff = 90 + 14 * k
            self.assertEqual(len(train), cutoff)
            self.assertLess(train.index.max(), df.index[cutoff])  # train strictly before test
        self.assertEqual(len(calls[-1]) + 14, len(df))

    def test_walk_forward_metric_is_finite_with_zero_sales_days(self):
        import numpy as np

        ns = self._run([0, 5], namespace={"pm": _fake_pm([])}, n=188, zero_every=7)
        self.assertTrue(np.isfinite(ns["model_mae"]).all())
        self.assertLess(max(ns["model_mae"]), 1e3)  # MAPE here was ~1e15

    def test_seasonal_naive_baseline_is_exact_on_periodic_series(self):
        ns = self._run([0, 5], namespace={"pm": _fake_pm([])}, n=132, periodic=True)
        self.assertEqual(len(ns["naive_mae"]), 3)
        for v in ns["naive_mae"]:
            self.assertAlmostEqual(v, 0.0, places=9)

    def test_walk_forward_too_short_series_raises(self):
        with self.assertRaises(ValueError):
            self._run([0, 5], namespace={"pm": _fake_pm([])}, n=100)


if __name__ == "__main__":
    unittest.main()
