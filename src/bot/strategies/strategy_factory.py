from typing import Dict, Any
from strategies.bb_smi import BBSmiStrategy
from strategies.rsi_bb import RsiBBStrategy
from strategies.rsi_bb_fee_aware import RsiBBFeeAwareStrategy
from strategies.rsi_bb_fee_aware_v3 import RsiBBFeeAwareV3Strategy
from strategies.rsi_bb_fee_aware_v4 import RsiBBFeeAwareV4AStrategy, RsiBBFeeAwareV4BStrategy
from strategies.rsi_5m_v2 import Rsi5mV2Strategy
from strategies.rsi_bb_5m_v3 import RsiBB5mV3Strategy
from strategies.rsi_only import RsiOnlyStrategy


def _v3_v4_kwargs(config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "rsi_period": config.get("rsi_period", 14),
        "bb_length": config.get("bb_length", 20),
        "bb_std": config.get("bb_std", 2.2),
        "overbought": config.get("overbought", 70),
        "oversold": config.get("oversold", 35),
        "trend_sma_period": config.get("trend_sma_period", 50),
        "bear_dip_rsi": config.get("bear_dip_rsi", 30),
        "bb_tolerance": config.get("bb_tolerance", 0.003),
        "fee_per_trade": config.get("fee_per_trade", 2.5),
        "estimated_trade_notional": config.get("estimated_trade_notional", 1000.0),
        "min_reward_pct": config.get("min_reward_pct", 0.004),
        "fee_reward_multiple": config.get("fee_reward_multiple", 2.0),
        "min_bb_width_pct": config.get("min_bb_width_pct", 0.006),
        "require_confirmation": config.get("require_confirmation", True),
        "exit_rsi": config.get("exit_rsi", 55),
        "ext_hours_oversold": config.get("ext_hours_oversold", 20),
        "ext_hours_bb_std": config.get("ext_hours_bb_std", 3.0),
        "ext_hours_bb_tolerance": config.get("ext_hours_bb_tolerance"),
        "ext_hours_volume_filter_enabled": config.get("ext_hours_volume_filter_enabled", False),
        "ext_hours_volume_lookback": config.get("ext_hours_volume_lookback", 50),
        "ext_hours_volume_multiplier": config.get("ext_hours_volume_multiplier", 2.0),
        "ext_hours_buy_confirmation_bars": config.get("ext_hours_buy_confirmation_bars", 4),
        "ext_hours_sell_confirmation_bars": config.get("ext_hours_sell_confirmation_bars", 4),
        "ema_fast_period": config.get("ema_fast_period", 9),
        "atr_period": config.get("atr_period", 14),
        "atr_baseline_period": config.get("atr_baseline_period", 50),
        "volume_lookback": config.get("volume_lookback", 20),
        "volume_multiplier": config.get("volume_multiplier", 1.5),
        "profit_rsi": config.get("profit_rsi", 70),
        "dynamic_bb_min_std": config.get("dynamic_bb_min_std", 2.0),
        "dynamic_bb_max_std": config.get("dynamic_bb_max_std", 2.5),
    }

def create_strategy(strategy_name: str, config: Dict[str, Any]):
    strategy_name = strategy_name.upper()
    
    if strategy_name == "RSI_BB":
        return RsiBBStrategy(
            rsi_period=config.get("rsi_period", 14),
            bb_length=config.get("bb_length", 20),
            bb_std=config.get("bb_std", 2.2),
            overbought=config.get("overbought", 70),
            oversold=config.get("oversold", 33),
            trend_sma_period=config.get("trend_sma_period", 50),
            bear_dip_rsi=config.get("bear_dip_rsi", 28),
            bb_tolerance=config.get("bb_tolerance", 0.0015),
            ext_hours_oversold=config.get("ext_hours_oversold", 20),
            ext_hours_bb_std=config.get("ext_hours_bb_std", 3.0),
            ext_hours_bb_tolerance=config.get("ext_hours_bb_tolerance"),
            ext_hours_volume_filter_enabled=config.get("ext_hours_volume_filter_enabled", False),
            ext_hours_volume_lookback=config.get("ext_hours_volume_lookback", 50),
            ext_hours_volume_multiplier=config.get("ext_hours_volume_multiplier", 2.0),
            ext_hours_buy_confirmation_bars=config.get("ext_hours_buy_confirmation_bars", 4),
            ext_hours_sell_confirmation_bars=config.get("ext_hours_sell_confirmation_bars", 4),
        )
    elif strategy_name == "RSI_BB_FEE_AWARE":
        return RsiBBFeeAwareStrategy(
            rsi_period=config.get("rsi_period", 14),
            bb_length=config.get("bb_length", 20),
            bb_std=config.get("bb_std", 2.2),
            overbought=config.get("overbought", 70),
            oversold=config.get("oversold", 33),
            trend_sma_period=config.get("trend_sma_period", 50),
            bear_dip_rsi=config.get("bear_dip_rsi", 28),
            bb_tolerance=config.get("bb_tolerance", 0.0015),
            fee_per_trade=config.get("fee_per_trade", 2.5),
            estimated_trade_notional=config.get("estimated_trade_notional", 1000.0),
            min_reward_pct=config.get("min_reward_pct", 0.006),
            fee_reward_multiple=config.get("fee_reward_multiple", 3.0),
            min_bb_width_pct=config.get("min_bb_width_pct", 0.008),
            require_confirmation=config.get("require_confirmation", True),
            exit_rsi=config.get("exit_rsi", 55),
            ext_hours_oversold=config.get("ext_hours_oversold", 20),
            ext_hours_bb_std=config.get("ext_hours_bb_std", 3.0),
            ext_hours_bb_tolerance=config.get("ext_hours_bb_tolerance"),
            ext_hours_volume_filter_enabled=config.get("ext_hours_volume_filter_enabled", False),
            ext_hours_volume_lookback=config.get("ext_hours_volume_lookback", 50),
            ext_hours_volume_multiplier=config.get("ext_hours_volume_multiplier", 2.0),
            ext_hours_buy_confirmation_bars=config.get("ext_hours_buy_confirmation_bars", 4),
            ext_hours_sell_confirmation_bars=config.get("ext_hours_sell_confirmation_bars", 4),
        )
    elif strategy_name == "RSI_BB_FEE_AWARE_V3":
        return RsiBBFeeAwareV3Strategy(**_v3_v4_kwargs(config))
    elif strategy_name == "RSI_BB_FEE_AWARE_V4A":
        kwargs = _v3_v4_kwargs(config)
        kwargs["volume_multiplier"] = config.get("volume_multiplier", 1.25)
        kwargs["dynamic_bb_min_std"] = config.get("dynamic_bb_min_std", 2.1)
        kwargs["dynamic_bb_max_std"] = config.get("dynamic_bb_max_std", 2.3)
        kwargs.update(
            {
                "volume_spike_window_bars": config.get("volume_spike_window_bars", 3),
                "max_stagnation_bars": config.get("max_stagnation_bars", 24),
                "stagnation_min_pnl_pct": config.get("stagnation_min_pnl_pct", 0.0),
            }
        )
        return RsiBBFeeAwareV4AStrategy(**kwargs)
    elif strategy_name == "RSI_BB_FEE_AWARE_V4B":
        kwargs = _v3_v4_kwargs(config)
        kwargs["volume_multiplier"] = config.get("volume_multiplier", 1.25)
        kwargs["dynamic_bb_min_std"] = config.get("dynamic_bb_min_std", 2.1)
        kwargs["dynamic_bb_max_std"] = config.get("dynamic_bb_max_std", 2.3)
        kwargs.update(
            {
                "volume_spike_window_bars": config.get("volume_spike_window_bars", 3),
                "max_stagnation_bars": config.get("max_stagnation_bars", 24),
                "stagnation_min_pnl_pct": config.get("stagnation_min_pnl_pct", 0.0),
                "scale_in_enabled": config.get("scale_in_enabled", True),
                "scale_in_initial_fraction": config.get(
                    "scale_in_initial_fraction",
                    config.get("initial_entry_fraction", 0.4),
                ),
                "scale_in_fraction": config.get("scale_in_fraction", 0.6),
                "scale_in_drop_pct": config.get("scale_in_drop_pct", 0.0125),
            }
        )
        return RsiBBFeeAwareV4BStrategy(**kwargs)
    elif strategy_name == "RSI_ONLY":
        return RsiOnlyStrategy(
            rsi_period=config.get("rsi_period", 14),
            overbought=config.get("overbought", 70),
            oversold=config.get("oversold", 30),
            stop_loss_pct=config.get("stop_loss_pct", 0.03),
        )
    elif strategy_name == "RSI_5M_V2":
        return Rsi5mV2Strategy(
            rsi_period=config.get("rsi_period", 14),
            overbought=config.get("overbought", 75),
            oversold=config.get("oversold", 25),
            trend_timeframe=config.get("trend_timeframe", "1h"),
            trend_ema_period=config.get("trend_ema_period", 200),
            atr_period=config.get("atr_period", 14),
            atr_stop_multiple=config.get("atr_stop_multiple", 2.0),
        )
    elif strategy_name == "RSI_BB_5M_V3":
        return RsiBB5mV3Strategy(
            rsi_period=config.get("rsi_period", 14),
            bb_length=config.get("bb_length", 20),
            bb_std=config.get("bb_std", 2.5),
            bb_tolerance=config.get("bb_tolerance", 0.002),
            bull_oversold=config.get("bull_oversold", 33),
            bear_oversold=config.get("bear_oversold", 25),
            overbought=config.get("overbought", 70),
            trend_timeframe=config.get("trend_timeframe", "1h"),
            trend_ema_period=config.get("trend_ema_period", 200),
            stop_loss_pct=config.get("stop_loss_pct", 0.05),
        )
    else:
        # Default to BB_SMI
        return BBSmiStrategy(
            bb_length=config.get("bb_length", 20),
            bb_std=config.get("bb_std", 2.0),
            smi_fast=config.get("smi_fast", 10),
            smi_slow=config.get("smi_slow", 3),
            smi_sig=config.get("smi_sig", 3),
            near_band_pct=config.get("near_band_pct", 0.001)
        )
