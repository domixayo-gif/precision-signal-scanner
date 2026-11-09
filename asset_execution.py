"""Resolve an enabled IQ Option turbo active from fresh broker init data."""


def _root(data):
    if not isinstance(data, dict):
        return {}
    result = data.get("result")
    if not isinstance(result, dict):
        result = data.get("data")
    return result if isinstance(result, dict) else data


def _truth(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def find_enabled_turbo_active_id(init_info, broker_asset):
    """Return the broker active ID only when the exact turbo asset is enabled.

    broker_asset must retain the exact broker name (including suffixes such
    as -op or -OTC). Binary-market availability is deliberately not accepted.
    """
    wanted = str(broker_asset or "").strip().upper()
    if not wanted:
        return None

    turbo = _root(init_info).get("turbo")
    if not isinstance(turbo, dict):
        return None
    actives = turbo.get("actives")
    if isinstance(actives, dict):
        items = actives.items()
    elif isinstance(actives, list):
        items = ((active.get("id", active.get("active_id")), active)
                 for active in actives if isinstance(active, dict))
    else:
        return None

    for active_id, active in items:
        if not isinstance(active, dict):
            continue
        raw_name = str(active.get("name", "")).strip()
        exact_name = raw_name.rsplit(".", 1)[-1].strip().upper()
        if exact_name != wanted:
            continue
        if not _truth(active.get("enabled")) or _truth(active.get("is_suspended")):
            return None
        try:
            return int(active_id)
        except (TypeError, ValueError):
            try:
                return int(active.get("id", active.get("active_id")))
            except (TypeError, ValueError):
                return None
    return None
