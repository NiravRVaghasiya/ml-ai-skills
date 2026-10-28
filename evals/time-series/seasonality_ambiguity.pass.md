Happy to - before I build anything, a few details change the approach completely:
1. What granularity and horizon: total company sales, or per store/product, and daily, weekly or monthly forecasts for the next quarter?
2. How much history do you have (ideally 2+ years to see yearly seasonality)?
3. Are there known seasonal patterns, holidays or one-off events in the data?
4. Do you have drivers such as promotions, price changes or marketing spend for the forecast period?
5. How will we judge it - which error metric (MAPE, MAE), and on what backtest window?

If you just want a starting point, I'll assume monthly data with yearly seasonality, fit a seasonal-naive baseline and a SARIMA/Prophet model, and compare them with a walk-forward backtest on the last 12 months - but I'd confirm the frequency first.
