---
name: time-series
display_name: Time Series Forecasting (Seasonality, ARIMA, Prophet)
description: >
  Use when the user wants to forecast a time series, decompose trend and
  seasonality, check stationarity, or build ARIMA/SARIMA/Prophet forecasting
  models. Trigger phrases: "forecast next quarter's sales", "detect
  seasonality in this data", "build an ARIMA model", "forecast this time
  series with Prophet", "is this series stationary". NOT for generic tabular
  regression/classification without temporal ordering (see
  supervised-learning) or general feature construction (see
  feature-engineering).
type: workflow
domain: specialized
level: intermediate
lifecycle: stable
risk_level: low
evidence_level: established-practice
last_verified: 2026-10-27
capabilities:
  - seasonality-trend-decomposition
  - stationarity-testing-adf
  - sarima-order-selection
  - prophet-seasonality-holiday-modeling
  - walk-forward-backtesting
requires:
conflicts:
related:
  - supervised-learning
  - feature-engineering
  - model-evaluation
inputs: A timestamped numeric series (e.g. a pandas DataFrame with a date column) to be forecast.
outputs: A fitted SARIMA and/or Prophet forecaster, decomposition components, and walk-forward backtest error metrics.
version_constraints:
  - "statsmodels>=0.11: `statsmodels.tsa.seasonal.STL(robust=True)` and `adfuller` (executed against statsmodels 0.14.6 on 2026-10-27; the 0.11 minimum for STL is a model-knowledge estimate, not verified)"
  - "pmdarima>=2.1: `auto_arima(seasonal=True, m=7)` and `predict(n_periods=..., return_conf_int=True)` (executed against pmdarima 2.1.1 with numpy 2.4.4 on 2026-10-27; older releases not tested)"
  - "prophet>=1.0: `Prophet(yearly_seasonality=auto)`, `add_country_holidays`, `make_future_dataframe` (executed against prophet 1.5.0 + holidays 0.107 on 2026-10-27; the 1.0 minimum, when the package was renamed from fbprophet, is not verified)"
---

## Overview
Covers the forecasting-specific pipeline: getting a series onto a clean
fixed-frequency index, decomposing trend/seasonality, testing stationarity,
and fitting classical (SARIMA) and modern (Prophet) forecasters with
walk-forward backtesting. Generic regression mechanics (train/test split,
metric definitions) live in `supervised-learning`/`model-evaluation` — this
skill covers what's specifically different about sequential, autocorrelated
data.

## Workflow
1. **Load, index, and regularize frequency.** Forecasting models assume a
   fixed time step; gaps or irregular timestamps break autocorrelation.
   ```python
   import pandas as pd

   MAX_GAP = 3   # longest run of missing days you are willing to interpolate

   df = pd.read_csv("sales.csv", parse_dates=["date"])
   df = df.set_index("date").sort_index()
   df = df.asfreq("D")                      # enforce daily frequency; missing days become NaN

   # interpolate(limit=3) alone would also fill the first 3 days of a *longer* gap,
   # so measure each gap's length and fill only the short ones.
   missing = df["sales"].isna()
   gap_len = missing.groupby((~missing).cumsum()).transform("sum")
   fill = missing & (gap_len <= MAX_GAP)
   df.loc[fill, "sales"] = df["sales"].interpolate(limit_area="inside")[fill]

   # Any NaN left makes STL return all-NaN components and makes dropna() splice across gaps.
   if df["sales"].isna().any():
       first = df.index[df["sales"].isna()][0].date()
       raise ValueError(f"{int(df['sales'].isna().sum())} missing days remain (first: {first}); "
                        f"gaps longer than {MAX_GAP} days need an explicit decision "
                        "(impute with domain knowledge, truncate history, or use a NaN-tolerant model)")
   ```
2. **Decompose trend and seasonality.** STL is more robust than classical
   decomposition to outliers and non-constant seasonal amplitude.
   ```python
   from statsmodels.tsa.seasonal import STL

   stl = STL(df["sales"], period=7, robust=True)
   res = stl.fit()
   trend, seasonal, resid = res.trend, res.seasonal, res.resid
   ```
3. **Test stationarity before fitting ARIMA.** ARIMA assumes stationarity;
   fitting on a trending series without differencing biases forecasts. The
   ADF test's `p < 0.05` cutoff is a convention, not a proof — treat the
   resulting `d` as a starting point, not a final answer: some series need
   `d=2`, and a single ADF read can disagree with a KPSS test, so let
   `auto_arima`'s own order search (step 4) confirm or override it rather
   than hard-coding `d` from this test alone.
   ```python
   from statsmodels.tsa.stattools import adfuller

   stat, pvalue, *_ = adfuller(df["sales"].dropna())
   print(f"ADF stat={stat:.3f}, p-value={pvalue:.4f}")
   d = 0 if pvalue < 0.05 else 1   # starting guess; let auto_arima confirm/override d
   ```
4. **Fit SARIMA with an auto-selected order.** Use `pmdarima` to search
   (p,d,q)(P,D,Q,s) rather than hand-tuning by ACF/PACF plots alone.
   ```python
   import pmdarima as pm

   auto_model = pm.auto_arima(
       df["sales"], seasonal=True, m=7,
       d=None, D=None, trace=False, suppress_warnings=True,
       stepwise=True,
   )
   forecast, conf_int = auto_model.predict(n_periods=14, return_conf_int=True)
   ```
5. **Fit Prophet as a complementary model.** Prophet handles multiple
   seasonalities and holiday effects with less manual differencing.
   ```python
   from prophet import Prophet

   prophet_df = df.reset_index().rename(columns={"date": "ds", "sales": "y"})
   # "auto" (the default) enables yearly seasonality only with >= 2 years of history and
   # weekly with >= 2 weeks; forcing True on a short history fits noise (see Gotchas).
   m = Prophet(yearly_seasonality="auto", weekly_seasonality="auto")
   m.add_country_holidays(country_name="US")
   m.fit(prophet_df)

   future = m.make_future_dataframe(periods=14)
   fcst = m.predict(future)[["ds", "yhat", "yhat_lower", "yhat_upper"]]
   ```
6. **Backtest with walk-forward validation.** Random splits leak future
   information; forecasts must only ever be evaluated against strictly
   later, unseen periods. Compare against a seasonal-naive baseline ("same day
   last week"): as a rule of thumb, a model that doesn't clearly beat it rarely
   justifies its extra complexity.
   ```python
   import numpy as np
   from sklearn.metrics import mean_absolute_error

   y = df["sales"]
   initial, horizon = 90, 14          # first training window, forecast length per fold
   model_mae, naive_mae = [], []
   for cutoff in range(initial, len(y) - horizon + 1, horizon):   # +1 keeps the last full fold
       train, test = y.iloc[:cutoff], y.iloc[cutoff:cutoff + horizon]
       # re-selects the order on past data only; slow (one full search per fold)
       model = pm.auto_arima(train, seasonal=True, m=7, suppress_warnings=True)
       preds = model.predict(n_periods=horizon)
       naive = np.resize(train.iloc[-7:].to_numpy(), horizon)    # repeat the last week
       model_mae.append(mean_absolute_error(test, preds))
       naive_mae.append(mean_absolute_error(test, naive))
   if not model_mae:
       raise ValueError(f"need at least {initial + horizon} observations for one fold, got {len(y)}")
   print(f"MAE over {len(model_mae)} folds: model={np.mean(model_mae):.2f}, "
         f"seasonal-naive={np.mean(naive_mae):.2f}")
   ```

## Gotchas
- **Random train/test splits leak the future.** Unlike i.i.d. tabular data,
  time series must be split chronologically (or walk-forward) — a random
  k-fold split lets the model "see" future values via autocorrelation with
  neighboring points.
- **Fitting ARIMA on a non-stationary series.** Skipping the ADF test and
  fitting on a trending/seasonal-non-differenced series produces confident
  but biased forecasts; always check stationarity and set `d`/`D` deliberately.
- **Prophet needs enough history for the seasonalities you enable.**
  Estimating a seasonal component reliably requires multiple full cycles of
  it in the training data, not a fixed calendar length — as a rough guide,
  yearly seasonality with under roughly 2 years of daily/weekly data is
  often mostly fitting noise, but the actual amount needed depends on data
  frequency and noise level. Disable seasonal components you don't have
  enough cycles to estimate.
- **Lookahead in engineered lag/rolling features.** A rolling mean or lag
  feature computed with `center=True` or using the full series (rather than
  only past values) leaks future information into the "past" feature.
- **Irregular timestamps silently break ACF/PACF and STL.** Always
  `asfreq()` (or resample) to a fixed cadence and explicitly handle
  resulting NaNs before decomposition — don't assume the raw timestamps are
  evenly spaced. A single leftover NaN is not an error in statsmodels' `STL`:
  it silently returns all-NaN trend/seasonal/resid (observed on statsmodels
  0.14.6), which is why step 1 fails loudly instead.
- **MAPE blows up on zero or near-zero actuals.** Daily retail/demand series
  often have zero days; scikit-learn's `mean_absolute_percentage_error`
  divides by a machine-epsilon floor there, so a few zero days dominate the
  score (about 1e15 on a toy series with weekly zero days, observed).
  Use MAE (or MASE / a ratio to the seasonal-naive baseline) unless the
  actuals are bounded well away from zero.

## References
- [statsmodels: Time Series Analysis](https://www.statsmodels.org/stable/tsa.html) — STL, ADF test, SARIMAX reference implementation.
- [Prophet documentation](https://facebook.github.io/prophet/docs/quick_start.html) — official quick-start and seasonality/holiday configuration.
- [pmdarima documentation](https://alkaline-ml.com/pmdarima/) — `auto_arima` order search, the practical alternative to manual ACF/PACF tuning.
