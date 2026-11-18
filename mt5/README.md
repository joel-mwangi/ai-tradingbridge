# TradeBridge MT5 Chart Analyst

This folder contains the first, read-only MT5 chart-analysis companion.

## Current version

`Indicators/TradeBridgeAIAnalyst.mq5` draws chart annotations from broker-provided MT5 candles:

- **Candle patterns:** bullish/bearish engulfing, hammer, shooting star and doji.
- **Market structure:** confirmed swing highs/lows labelled HH, LH, HL, LL, EQH or EQL.
- **Support/resistance:** nearby confirmed swing levels clustered into ATR-scaled price zones.
- **Status panel:** symbol, chart timeframe, broad swing structure, most recent closed-candle label and number of zones.

The indicator deliberately analyses **closed candles**. A forming candle can change shape before it closes, so it is not labelled as a confirmed pattern. Analysis is refreshed on a new bar or when the terminal resets the indicator calculation history.

### What this version does not do

- It does not submit, modify or close orders.
- It does not read or transmit account credentials.
- It does not send chart data to an external AI service.
- It does not make calibrated probability claims or claim that a pattern predicts the next price move.
- It is not yet an LLM/AI interpretation layer. Version 0.1 establishes a transparent, deterministic baseline that can be tested before an AI explanation service is integrated.

The pattern names are rule-based geometric classifications. Support/resistance zones are inferred from confirmed swing points; they are not guaranteed turning points or official broker signals.

## Install on Deriv MT5 desktop

> A web browser terminal cannot install custom MQL5 indicators. Use the Deriv MT5 desktop terminal for this companion.

1. Open Deriv MT5 desktop.
2. Choose **File → Open Data Folder**.
3. Open `MQL5/Indicators`.
4. Copy `TradeBridgeAIAnalyst.mq5` from this repository's `mt5/Indicators` folder into that directory.
5. In MT5, press **F4** to open MetaEditor. Open the copied file and press **F7** to compile it.
6. Return to MT5. In Navigator, right-click **Indicators → Refresh**.
7. Drag **TradeBridge Chart Analyst (Read-only)** onto a forex chart, for example the exact EUR/USD symbol available on your Deriv MT5 server.

Use the exact symbol shown in Market Watch. Broker symbol names can have suffixes or prefixes, so do not assume every account uses the literal `EURUSD` symbol.

## Suggested initial settings

- Timeframe: M15 for a first visual check.
- Lookback: 250 bars.
- Swing strength: 3 bars on each side.
- Pattern scan: 45 closed candles.
- Maximum zones: 3 support and 3 resistance zones.
- Keep the status panel enabled while validating the output.

These are starting defaults, not a claim of optimal trading parameters. Indicator labels and rectangles are prefixed with `TB_AI_`; on removal, the indicator deletes only its own chart objects.

## Validation checklist

Before relying on any annotation:

1. Compile in MetaEditor and inspect the Errors tab. This repository change has not been compiled by MetaEditor in the authoring environment.
2. Attach to a **demo** chart and ensure the panel shows the expected symbol and timeframe.
3. Scroll through the chart and manually inspect a sample of each candle label against the OHLC bars.
4. Check the swing labels against confirmed pivot highs/lows; remember the configured swing strength means a pivot needs later bars to be confirmed.
5. Check each zone's price and width against the chart scale; change timeframe and symbol and confirm annotations recalculate.
6. Remove the indicator and verify only its own `TB_AI_` objects are removed.
7. Test different chart themes. If the labels or zones have insufficient contrast, adjust their colours before use.

## Design direction

Next stages, after this deterministic baseline is compiled and visually validated:

1. Add unit-tested analytical calculations and structured evidence output.
2. Add a separate AI interpretation layer that explains the evidence, alternatives and invalidation conditions without inventing prices.
3. Connect the interpretation layer to TradeBridge through a secure, explicitly configured service.
4. Keep execution disabled unless it is separately designed, tested and explicitly approved.

Official references:

- [MetaQuotes: Custom Indicators](https://www.mql5.com/en/docs/customind)
- [MetaQuotes: OnCalculate event](https://www.mql5.com/en/docs/event_handlers/oncalculate)
- [MetaTrader 5: Expert Advisors and custom indicators](https://www.metatrader5.com/en/terminal/help/algotrading/trade_robots_indicators)
