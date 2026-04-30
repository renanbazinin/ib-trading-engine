import json
import os
from typing import Any, Dict


class SimulationConfigError(ValueError):
    pass


def _to_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SimulationConfigError(f"Field '{field_name}' must be an integer")


def _to_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SimulationConfigError(f"Field '{field_name}' must be a number")


def get_default_simulation_config_path() -> str:
    base_dir = os.path.dirname(os.path.dirname(__file__))
    return os.path.join(base_dir, "simulation", "config", "simulation_config.json")


def get_simulation_config_path() -> str:
    configured = os.getenv("SIM_CONFIG_PATH", "").strip()
    if configured:
        return configured
    return get_default_simulation_config_path()


def parse_and_validate_simulation_config(config_data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(config_data, dict):
        raise SimulationConfigError("Simulation config root must be a JSON object")

    defaults_raw = config_data.get("defaults", {})
    export_policy_raw = config_data.get("export_policy", {})
    simulations_raw = config_data.get("simulations")

    if not isinstance(defaults_raw, dict):
        raise SimulationConfigError("Field 'defaults' must be a JSON object")
    if not isinstance(export_policy_raw, dict):
        raise SimulationConfigError("Field 'export_policy' must be a JSON object")
    if not isinstance(simulations_raw, list) or len(simulations_raw) == 0:
        raise SimulationConfigError("Field 'simulations' must be a non-empty JSON array")

    defaults = {
        "starting_balance": _to_float(defaults_raw.get("starting_balance", 10000), "defaults.starting_balance"),
        "trade_quantity": _to_int(defaults_raw.get("trade_quantity", 100), "defaults.trade_quantity"),
        "max_recent_events": _to_int(defaults_raw.get("max_recent_events", 100), "defaults.max_recent_events"),
        "near_band_pct": _to_float(defaults_raw.get("near_band_pct", 0.001), "defaults.near_band_pct"),
    }

    export_policy = {
        "summary_filename": str(export_policy_raw.get("summary_filename", "latest_sims.json")),
        "summary_every_n_updates": _to_int(
            export_policy_raw.get("summary_every_n_updates", 1),
            "export_policy.summary_every_n_updates",
        ),
        "global_recent_limit": _to_int(
            export_policy_raw.get("global_recent_limit", 200),
            "export_policy.global_recent_limit",
        ),
    }

    if export_policy["summary_every_n_updates"] < 1:
        raise SimulationConfigError("export_policy.summary_every_n_updates must be >= 1")
    if export_policy["global_recent_limit"] < 1:
        raise SimulationConfigError("export_policy.global_recent_limit must be >= 1")

    normalized_simulations = []
    seen_ids = set()

    for index, sim in enumerate(simulations_raw):
        if not isinstance(sim, dict):
            raise SimulationConfigError(f"simulations[{index}] must be a JSON object")

        sim_id = str(sim.get("id", "")).strip()
        if not sim_id:
            raise SimulationConfigError(f"simulations[{index}].id is required")
        if sim_id in seen_ids:
            raise SimulationConfigError(f"Duplicate simulation id '{sim_id}'")
        seen_ids.add(sim_id)

        normalized = dict(sim)
        normalized.update(
            {
                "id": sim_id,
                "name": str(sim.get("name", sim_id)),
                "enabled": bool(sim.get("enabled", True)),
                "strategy": str(sim.get("strategy", "RSI_BB_FEE_AWARE_V4B")).upper(),
                "bb_length": _to_int(sim.get("bb_length", 20), f"simulations[{index}].bb_length"),
                "bb_std": _to_float(sim.get("bb_std", 2.0), f"simulations[{index}].bb_std"),
                "smi_fast": _to_int(sim.get("smi_fast", 10), f"simulations[{index}].smi_fast"),
                "smi_slow": _to_int(sim.get("smi_slow", 3), f"simulations[{index}].smi_slow"),
                "smi_sig": _to_int(sim.get("smi_sig", 3), f"simulations[{index}].smi_sig"),
            }
        )

        if "starting_balance" in sim:
            normalized["starting_balance"] = _to_float(
                sim.get("starting_balance"),
                f"simulations[{index}].starting_balance",
            )
        if "trade_quantity" in sim:
            normalized["trade_quantity"] = _to_int(
                sim.get("trade_quantity"),
                f"simulations[{index}].trade_quantity",
            )
        if "max_recent_events" in sim:
            normalized["max_recent_events"] = _to_int(
                sim.get("max_recent_events"),
                f"simulations[{index}].max_recent_events",
            )

        normalized_simulations.append(normalized)

    if not any(sim["enabled"] for sim in normalized_simulations):
        raise SimulationConfigError("At least one simulation must be enabled")

    return {
        "defaults": defaults,
        "export_policy": export_policy,
        "simulations": normalized_simulations,
    }


def load_simulation_config(config_path: str | None = None) -> Dict[str, Any]:
    resolved_path = config_path or get_simulation_config_path()

    if not os.path.exists(resolved_path):
        raise SimulationConfigError(f"Simulation config not found: {resolved_path}")

    try:
        with open(resolved_path, "r", encoding="utf-8") as file_handle:
            raw_config = json.load(file_handle)
    except json.JSONDecodeError as exc:
        raise SimulationConfigError(f"Invalid simulation config JSON: {exc}")

    normalized = parse_and_validate_simulation_config(raw_config)
    normalized["config_path"] = resolved_path
    normalized["raw"] = raw_config
    return normalized
