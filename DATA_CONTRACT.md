# Feed data contract

## Top-level rules

- `actionable` must remain `false` in the free-data version.
- `generated_at` must include an explicit time-zone offset.
- `data_status` must disclose that the feed is delayed or end-of-day.
- Missing values must be `null`, never silently converted to zero.
- `preliminary_opportunity_score` is a cross-sectional ranking aid, not a
  calibrated probability.
- `trust_rate` is capped because the source is not broker-grade.

## Candidate fields

| Field | Meaning |
|---|---|
| `rank` | Relative order in the current feed |
| `previous_close` | Last completed session close |
| `previous_day_rvol` | Last completed session volume divided by its prior 20-session mean |
| `avg_turnover_20d_cr` | Mean 20-session traded value in crore rupees |
| `atr14_pct` | ATR as a percentage of the last completed close |
| `technical_score` | Prior-day technical component |
| `liquidity_score` | Historical traded-value component |
| `macro_alignment_score` | Simple industry/global proxy alignment |
| `preliminary_opportunity_score` | Combined watchlist rank |
| `trust_rate` | Data completeness/freshness indicator |
| `status` | `PRIMARY_WATCH`, `SECONDARY_WATCH`, or `CONDITIONAL` |
| `reason_codes` | Machine-readable explanation codes |
| `missing_fields` | Inputs unavailable for the candidate |

## GPT-added fields

The Custom GPT may add these only after researching current sources:

- `official_catalyst_score`
- `eic_alignment`
- `governance_risk`
- `filing_risk`
- `preopen_status`
- `research_verdict`
- `research_sources`

It must not overwrite the feed's measured values.


## Version 1.1 additions

- `market_status`, `closure_reason`, and `next_trading_day`
- `previous_high`, `previous_low`, `previous_range_pct`
- `previous_close_location`
- `classical_pivot`, `pivot_resistance_1`, `pivot_support_1`
- `volume_profile.point_of_control`
- `volume_profile.value_area_high`
- `volume_profile.value_area_low`
- `volume_profile.previous_session_vwap`
- `volume_profile.profile_state_at_close`
- `macro.overnight_proxies`

The previous-session volume profile is estimated from free 15-minute bars.
It is not tick-level exchange volume-at-price data. The overnight proxy fields
refer to global futures and FX; NSE cash shares do not trade overnight.
