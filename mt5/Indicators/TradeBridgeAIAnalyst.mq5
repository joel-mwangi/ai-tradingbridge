//+------------------------------------------------------------------+
//| TradeBridgeAIAnalyst.mq5                                         |
//| Read-only chart analysis: candle patterns, swings, S/R zones.    |
//| This indicator never sends, modifies, or closes orders.           |
//+------------------------------------------------------------------+
#property strict
#property version   "0.1.0"
#property description "TradeBridge rule-based chart analyst (analysis only)"
#property indicator_chart_window
#property indicator_buffers 0
#property indicator_plots   0

input int    InpLookbackBars          = 250;   // Bars used for swing structure and zones
input int    InpSwingStrength         = 3;     // Closed bars required on each side of a swing
input int    InpPatternBars            = 45;    // Closed bars scanned for candle patterns
input int    InpStructureMarkerBars    = 70;    // Recent bars eligible for HH/HL/LH/LL tags
input int    InpMaxZonesPerSide        = 3;     // Maximum support/resistance zones per side
input int    InpProjectionBars         = 35;    // Zone projection into the chart's future area
input int    InpAtrPeriod              = 14;    // ATR period used to scale zones/labels
input double InpZoneAtrMultiplier      = 0.12;  // Half-width of zones as a fraction of ATR
input double InpMergeAtrMultiplier     = 0.35;  // Merge nearby swing levels within ATR fraction
input int    InpLabelOffsetPoints       = 35;    // Minimum candle-label offset, in points
input int    InpPanelX                 = 12;    // Panel X offset
input int    InpPanelY                 = 22;    // Panel Y offset
input bool   InpShowCandlePatterns     = true;  // Label closed-candle patterns
input bool   InpShowStructureMarkers   = true;  // Label confirmed swing structure
input bool   InpShowSupportResistance  = true;  // Draw swing-based price zones
input bool   InpShowStatusPanel        = true;  // Show rule-based analysis summary

#define TB_PREFIX "TB_AI_"

struct PriceLevel
  {
   double   price;
   datetime last_touch;
   int      touches;
  };

datetime g_last_bar_time = 0;

//+------------------------------------------------------------------+
//| Remove only chart objects owned by this indicator.                |
//+------------------------------------------------------------------+
void DeleteOwnedObjects()
  {
   const int total = ObjectsTotal(0, 0, -1);
   for(int i = total - 1; i >= 0; --i)
     {
      const string name = ObjectName(0, i, 0, -1);
      if(StringFind(name, TB_PREFIX) == 0)
         ObjectDelete(0, name);
     }
  }

//+------------------------------------------------------------------+
//| Is the bar a confirmed swing high? Arrays use series indexing.     |
//+------------------------------------------------------------------+
bool IsSwingHigh(const int index,
                 const double &high[],
                 const int rates_total,
                 const int strength)
  {
   if(index <= strength || index + strength >= rates_total)
      return false;

   for(int offset = 1; offset <= strength; ++offset)
     {
      if(high[index] <= high[index - offset] ||
         high[index] <= high[index + offset])
         return false;
     }
   return true;
  }

//+------------------------------------------------------------------+
//| Is the bar a confirmed swing low?                                  |
//+------------------------------------------------------------------+
bool IsSwingLow(const int index,
                const double &low[],
                const int rates_total,
                const int strength)
  {
   if(index <= strength || index + strength >= rates_total)
      return false;

   for(int offset = 1; offset <= strength; ++offset)
     {
      if(low[index] >= low[index - offset] ||
         low[index] >= low[index + offset])
         return false;
     }
   return true;
  }

//+------------------------------------------------------------------+
//| Local ATR estimate, independent of external indicator handles.    |
//+------------------------------------------------------------------+
double AtrAt(const int shift,
             const double &high[],
             const double &low[],
             const double &close[],
             const int rates_total,
             const int period)
  {
   if(period < 1 || shift < 0 || shift + period >= rates_total)
      return 0.0;

   double total = 0.0;
   int samples = 0;
   for(int i = shift; i < shift + period; ++i)
     {
      const double previous_close = close[i + 1];
      const double range1 = high[i] - low[i];
      const double range2 = MathAbs(high[i] - previous_close);
      const double range3 = MathAbs(low[i] - previous_close);
      total += MathMax(range1, MathMax(range2, range3));
      ++samples;
     }

   if(samples == 0)
      return 0.0;
   return total / (double)samples;
  }

//+------------------------------------------------------------------+
//| Return a descriptive pattern for a closed candle.                  |
//+------------------------------------------------------------------+
string DetectPattern(const int index,
                     const double &open[],
                     const double &high[],
                     const double &low[],
                     const double &close[],
                     const int rates_total)
  {
   if(index < 1 || index + 1 >= rates_total)
      return "";

   const double range = high[index] - low[index];
   if(range <= 0.0)
      return "";

   const double body = MathAbs(close[index] - open[index]);
   const double upper_wick = high[index] - MathMax(open[index], close[index]);
   const double lower_wick = MathMin(open[index], close[index]) - low[index];

   const bool bullish = close[index] > open[index];
   const bool bearish = close[index] < open[index];
   const bool previous_bullish = close[index + 1] > open[index + 1];
   const bool previous_bearish = close[index + 1] < open[index + 1];

   // Current bar is newer than index+1 because time series are reversed.
   if(bullish && previous_bearish &&
      open[index] <= close[index + 1] &&
      close[index] >= open[index + 1])
      return "Bullish engulfing";

   if(bearish && previous_bullish &&
      open[index] >= close[index + 1] &&
      close[index] <= open[index + 1])
      return "Bearish engulfing";

   if(body > 0.0 &&
      lower_wick >= body * 2.0 &&
      upper_wick <= body * 0.75 &&
      body <= range * 0.40)
      return "Hammer";

   if(body > 0.0 &&
      upper_wick >= body * 2.0 &&
      lower_wick <= body * 0.75 &&
      body <= range * 0.40)
      return "Shooting star";

   if(body <= range * 0.10)
      return "Doji";

   return "";
  }

//+------------------------------------------------------------------+
//| Place a text object with a stable indicator-owned name.            |
//+------------------------------------------------------------------+
void DrawTextObject(const string suffix,
                    const datetime when,
                    const double price,
                    const string text,
                    const color text_color,
                    const int font_size)
  {
   const string name = TB_PREFIX + suffix;
   if(!ObjectCreate(0, name, OBJ_TEXT, 0, when, price))
      return;

   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetString(0, name, OBJPROP_FONT, "Arial");
   ObjectSetInteger(0, name, OBJPROP_COLOR, text_color);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, font_size);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_CENTER);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTED, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
//| Draw labels only for closed candles; forming candle is index 0.    |
//+------------------------------------------------------------------+
void DrawPatternLabels(const int rates_total,
                       const datetime &time[],
                       const double &open[],
                       const double &high[],
                       const double &low[],
                       const double &close[],
                       const int max_pattern_bars,
                       const double atr)
  {
   if(!InpShowCandlePatterns)
      return;

   const int bars_to_scan = (int)MathMin(MathMax(1, max_pattern_bars),
                                         rates_total - 2);
   const double offset = MathMax(_Point * MathMax(1, InpLabelOffsetPoints),
                                 atr * 0.12);

   for(int i = 1; i <= bars_to_scan; ++i)
     {
      const string pattern = DetectPattern(i, open, high, low, close, rates_total);
      if(pattern == "")
         continue;

      color label_color = clrGold;
      bool place_above = true;
      string short_label = pattern;

      if(pattern == "Bullish engulfing")
        {
         label_color = clrLimeGreen;
         place_above = false;
         short_label = "Bull engulf";
        }
      else if(pattern == "Bearish engulfing")
        {
         label_color = clrTomato;
         short_label = "Bear engulf";
        }
      else if(pattern == "Hammer")
        {
         label_color = clrLimeGreen;
         place_above = false;
        }
      else if(pattern == "Shooting star")
        {
         label_color = clrTomato;
        }
      else if(pattern == "Doji")
        {
         label_color = clrGold;
         short_label = "Doji";
        }

      const double label_price = place_above ? high[i] + offset
                                             : low[i] - offset;
      const string suffix = "PATTERN_" + IntegerToString(i);
      DrawTextObject(suffix, time[i], label_price, short_label,
                     label_color, 8);
     }
  }

//+------------------------------------------------------------------+
//| Collect nearby swing levels into merged candidate zones.           |
//+------------------------------------------------------------------+
void AddLevel(PriceLevel &levels[],
              const double price,
              const datetime when,
              const double tolerance)
  {
   const int count = ArraySize(levels);
   for(int i = 0; i < count; ++i)
     {
      if(MathAbs(levels[i].price - price) <= tolerance)
        {
         const int next_touches = levels[i].touches + 1;
         levels[i].price = ((levels[i].price * levels[i].touches) + price) /
                           (double)next_touches;
         levels[i].touches = next_touches;
         if(when > levels[i].last_touch)
            levels[i].last_touch = when;
         return;
        }
     }

   if(ArrayResize(levels, count + 1) != count + 1)
      return;
   levels[count].price = price;
   levels[count].last_touch = when;
   levels[count].touches = 1;
  }

//+------------------------------------------------------------------+
//| Sort levels nearest to current price first.                       |
//+------------------------------------------------------------------+
void SortLevelsByDistance(PriceLevel &levels[], const double current_price)
  {
   const int count = ArraySize(levels);
   for(int i = 0; i < count - 1; ++i)
     {
      int best = i;
      for(int j = i + 1; j < count; ++j)
        {
         if(MathAbs(levels[j].price - current_price) <
            MathAbs(levels[best].price - current_price))
            best = j;
        }
      if(best != i)
        {
         PriceLevel temp = levels[i];
         levels[i] = levels[best];
         levels[best] = temp;
        }
     }
  }

//+------------------------------------------------------------------+
//| Draw projected zones without altering chart scale or trading state. |
//+------------------------------------------------------------------+
void DrawZones(PriceLevel &levels[],
               const bool is_support,
               const double current_price,
               const datetime future_time,
               const double zone_half_width,
               const int max_zones,
               int &drawn)
  {
   drawn = 0;
   SortLevelsByDistance(levels, current_price);
   const int total = MathMin(ArraySize(levels), MathMax(0, max_zones));
   for(int i = 0; i < total; ++i)
     {
      const string side = is_support ? "S" : "R";
      const string name = TB_PREFIX + "ZONE_" + side + "_" +
                          IntegerToString(i + 1);
      const double top = levels[i].price + zone_half_width;
      const double bottom = levels[i].price - zone_half_width;

      if(!ObjectCreate(0, name, OBJ_RECTANGLE, 0,
                       levels[i].last_touch, top, future_time, bottom))
         continue;

      ObjectSetInteger(0, name, OBJPROP_COLOR,
                       is_support ? clrSeaGreen : clrIndianRed);
      ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_SOLID);
      ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
      ObjectSetInteger(0, name, OBJPROP_FILL, true);
      ObjectSetInteger(0, name, OBJPROP_BACK, true);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_SELECTED, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);

      const string label_name = TB_PREFIX + "ZONE_LABEL_" + side + "_" +
                                IntegerToString(i + 1);
      const string label = side + IntegerToString(i + 1) + "  " +
                           DoubleToString(levels[i].price, _Digits) +
                           "  (" + IntegerToString(levels[i].touches) + " swings)";
      DrawTextObject(label_name, levels[i].last_touch, top, label,
                     is_support ? clrPaleGreen : clrLightSalmon, 8);
      ++drawn;
     }
  }

//+------------------------------------------------------------------+
//| Draw confirmed HH/LH and HL/LL swing labels in chronological order. |
//+------------------------------------------------------------------+
void DrawStructureMarkers(const int rates_total,
                          const datetime &time[],
                          const double &high[],
                          const double &low[],
                          const double &close[],
                          const int strength,
                          const int max_index,
                          const double atr)
  {
   if(!InpShowStructureMarkers)
      return;

   const int marker_limit = (int)MathMin(max_index,
                                         MathMax(strength + 2,
                                                 InpStructureMarkerBars));
   const double tolerance = MathMax(_Point * 5.0, atr * 0.08);
   const double offset = MathMax(_Point * MathMax(1, InpLabelOffsetPoints),
                                 atr * 0.10);
   bool have_high = false;
   bool have_low = false;
   double previous_high = 0.0;
   double previous_low = 0.0;
   int sequence = 0;

   // With series indexing, descending indexes moves from older to newer bars.
   for(int i = marker_limit - 1; i >= strength + 1; --i)
     {
      if(IsSwingHigh(i, high, rates_total, strength))
        {
         string label = "SH";
         if(have_high)
           {
            if(high[i] > previous_high + tolerance)
               label = "HH";
            else if(high[i] < previous_high - tolerance)
               label = "LH";
            else
               label = "EQH";
           }
         DrawTextObject("STRUCT_H_" + IntegerToString(sequence++),
                        time[i], high[i] + offset, label, clrLightSalmon, 8);
         previous_high = high[i];
         have_high = true;
        }

      if(IsSwingLow(i, low, rates_total, strength))
        {
         string label = "SL";
         if(have_low)
           {
            if(low[i] > previous_low + tolerance)
               label = "HL";
            else if(low[i] < previous_low - tolerance)
               label = "LL";
            else
               label = "EQL";
           }
         DrawTextObject("STRUCT_L_" + IntegerToString(sequence++),
                        time[i], low[i] - offset, label, clrPaleGreen, 8);
         previous_low = low[i];
         have_low = true;
        }
     }
  }

//+------------------------------------------------------------------+
//| Get broad structure from the two latest confirmed highs and lows.  |
//+------------------------------------------------------------------+
string GetStructure(const int rates_total,
                    const double &high[],
                    const double &low[],
                    const int strength,
                    const int max_index,
                    const double atr)
  {
   bool have_latest_high = false;
   bool have_older_high = false;
   bool have_latest_low = false;
   bool have_older_low = false;
   double latest_high = 0.0, older_high = 0.0;
   double latest_low = 0.0, older_low = 0.0;

   // Scan recent to old. The first value found is the latest swing.
   for(int i = strength + 1; i < max_index; ++i)
     {
      if(!have_older_high && IsSwingHigh(i, high, rates_total, strength))
        {
         if(!have_latest_high)
           {
            latest_high = high[i];
            have_latest_high = true;
           }
         else
           {
            older_high = high[i];
            have_older_high = true;
           }
        }

      if(!have_older_low && IsSwingLow(i, low, rates_total, strength))
        {
         if(!have_latest_low)
           {
            latest_low = low[i];
            have_latest_low = true;
           }
         else
           {
            older_low = low[i];
            have_older_low = true;
           }
        }

      if(have_latest_high && have_older_high &&
         have_latest_low && have_older_low)
         break;
     }

   if(!(have_latest_high && have_older_high &&
        have_latest_low && have_older_low))
      return "Insufficient confirmed swings";

   const double tolerance = MathMax(_Point * 5.0, atr * 0.08);
   const bool higher_highs = latest_high > older_high + tolerance;
   const bool lower_highs = latest_high < older_high - tolerance;
   const bool higher_lows = latest_low > older_low + tolerance;
   const bool lower_lows = latest_low < older_low - tolerance;

   if(higher_highs && higher_lows)
      return "Bullish structure (HH + HL)";
   if(lower_highs && lower_lows)
      return "Bearish structure (LH + LL)";
   return "Mixed / range structure";
  }

//+------------------------------------------------------------------+
//| Create/update the fixed status panel.                             |
//+------------------------------------------------------------------+
void SetPanelLine(const int row,
                  const string text,
                  const color text_color,
                  const int font_size)
  {
   const string name = TB_PREFIX + "PANEL_LINE_" + IntegerToString(row);
   if(!ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0))
      return;
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, InpPanelX + 12);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, InpPanelY + 10 + row * 19);
   ObjectSetInteger(0, name, OBJPROP_COLOR, text_color);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, font_size);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTED, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   ObjectSetString(0, name, OBJPROP_FONT, "Consolas");
   ObjectSetString(0, name, OBJPROP_TEXT, text);
  }

void DrawStatusPanel(const string structure,
                     const string latest_pattern,
                     const int support_count,
                     const int resistance_count)
  {
   if(!InpShowStatusPanel)
      return;

   const string panel = TB_PREFIX + "PANEL_BG";
   if(ObjectCreate(0, panel, OBJ_RECTANGLE_LABEL, 0, 0, 0))
     {
      ObjectSetInteger(0, panel, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, panel, OBJPROP_XDISTANCE, InpPanelX);
      ObjectSetInteger(0, panel, OBJPROP_YDISTANCE, InpPanelY);
      ObjectSetInteger(0, panel, OBJPROP_XSIZE, 440);
      ObjectSetInteger(0, panel, OBJPROP_YSIZE, 134);
      ObjectSetInteger(0, panel, OBJPROP_BGCOLOR, clrBlack);
      ObjectSetInteger(0, panel, OBJPROP_COLOR, clrSlateGray);
      ObjectSetInteger(0, panel, OBJPROP_BORDER_TYPE, BORDER_FLAT);
      ObjectSetInteger(0, panel, OBJPROP_BACK, false);
      ObjectSetInteger(0, panel, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, panel, OBJPROP_SELECTED, false);
      ObjectSetInteger(0, panel, OBJPROP_HIDDEN, true);
     }

   SetPanelLine(0, "TRADEBRIDGE CHART ANALYST  |  ANALYSIS ONLY",
                clrWhite, 10);
   SetPanelLine(1, "Market: " + _Symbol + "  |  Timeframe: " +
                EnumToString((ENUM_TIMEFRAMES)_Period), clrLightSteelBlue, 9);
   SetPanelLine(2, "Structure: " + structure, clrWhite, 9);
   SetPanelLine(3, "Latest closed candle: " + latest_pattern, clrWhite, 9);
   SetPanelLine(4, "Zones: S " + IntegerToString(support_count) +
                " / R " + IntegerToString(resistance_count) +
                "  |  Rules-based, not a prediction", clrSilver, 8);
   SetPanelLine(5, "No orders are sent. Forming candle is not classified.",
                clrGold, 8);
  }

//+------------------------------------------------------------------+
//| Rebuild all owned annotations on a new bar or history reset.       |
//+------------------------------------------------------------------+
void RefreshAnalysis(const int rates_total,
                     const datetime &time[],
                     const double &open[],
                     const double &high[],
                     const double &low[],
                     const double &close[])
  {
   DeleteOwnedObjects();

   const int strength = (int)MathMax(2, InpSwingStrength);
   const int lookback = (int)MathMax(40, InpLookbackBars);
   const int max_index = (int)MathMin(rates_total - strength - 1, lookback);
   if(max_index <= strength + 2)
      return;

   const int atr_period = (int)MathMax(2, InpAtrPeriod);
   const double atr = AtrAt(1, high, low, close, rates_total, atr_period);
   const double safe_atr = atr > 0.0 ? atr : _Point * 50.0;
   const double latest_close = close[1];
   const string latest_pattern = DetectPattern(1, open, high, low, close,
                                              rates_total);
   const string pattern_summary = latest_pattern == "" ? "No named pattern"
                                                        : latest_pattern;
   const string structure = GetStructure(rates_total, high, low, strength,
                                         max_index, safe_atr);

   DrawPatternLabels(rates_total, time, open, high, low, close,
                     InpPatternBars, safe_atr);
   DrawStructureMarkers(rates_total, time, high, low, close, strength,
                        max_index, safe_atr);

   PriceLevel supports[];
   PriceLevel resistances[];
   const double merge_tolerance = MathMax(_Point * 10.0,
                                          safe_atr * MathMax(0.05,
                                                             InpMergeAtrMultiplier));
   const int oldest_scan = (int)MathMin(max_index,
                                        rates_total - strength - 1);

   if(InpShowSupportResistance)
     {
      for(int i = strength + 1; i < oldest_scan; ++i)
        {
         if(IsSwingLow(i, low, rates_total, strength) &&
            low[i] <= latest_close)
            AddLevel(supports, low[i], time[i], merge_tolerance);

         if(IsSwingHigh(i, high, rates_total, strength) &&
            high[i] >= latest_close)
            AddLevel(resistances, high[i], time[i], merge_tolerance);
        }
     }

   SortLevelsByDistance(supports, latest_close);
   SortLevelsByDistance(resistances, latest_close);

   int support_count = 0;
   int resistance_count = 0;
   const int projection_bars = (int)MathMax(5, InpProjectionBars);
   int seconds_per_bar = PeriodSeconds(_Period);
   if(seconds_per_bar <= 0)
      seconds_per_bar = 60;
   const datetime future_time = (datetime)(time[0] +
                                   (long)seconds_per_bar * projection_bars);
   const double zone_half_width = MathMax(_Point * 5.0,
                                          safe_atr * MathMax(0.02,
                                                             InpZoneAtrMultiplier));

   if(InpShowSupportResistance)
     {
      DrawZones(supports, true, latest_close, future_time, zone_half_width,
                InpMaxZonesPerSide, support_count);
      DrawZones(resistances, false, latest_close, future_time, zone_half_width,
                InpMaxZonesPerSide, resistance_count);
     }

   DrawStatusPanel(structure, pattern_summary, support_count, resistance_count);
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
//| Indicator lifecycle. No trading API calls exist in this file.     |
//+------------------------------------------------------------------+
int OnInit()
  {
   IndicatorSetString(INDICATOR_SHORTNAME, "TradeBridge Chart Analyst (Read-only)");
   DeleteOwnedObjects();
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   DeleteOwnedObjects();
  }

int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
  {
   if(rates_total < 50)
      return 0;

   ArraySetAsSeries(time, true);
   ArraySetAsSeries(open, true);
   ArraySetAsSeries(high, true);
   ArraySetAsSeries(low, true);
   ArraySetAsSeries(close, true);

   if(prev_calculated == 0 || time[0] != g_last_bar_time)
     {
      g_last_bar_time = time[0];
      RefreshAnalysis(rates_total, time, open, high, low, close);
     }

   return rates_total;
  }
//+------------------------------------------------------------------+
