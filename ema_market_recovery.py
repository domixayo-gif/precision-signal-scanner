"""Ranking helpers for already-qualified market-wide EMA recovery candidates."""
from math import isfinite

def recovery_quality_score(signal):
    """Score only fields emitted by the unchanged EMA signal calculator."""
    try:
        adx = float(signal["adx14"]); plus_di = float(signal["plus_di14"])
        minus_di = float(signal["minus_di14"]); chop = float(signal["chop25"])
        extension = float(signal["extension_atr"]); rsi = float(signal["rsi14"])
        direction = str(signal["direction"]).lower()
        if not all(isfinite(v) for v in (adx, plus_di, minus_di, chop, extension, rsi)): return None
        if direction not in ("call", "put") or extension < 0 or not 0 <= chop <= 100: return None
        adx_points = min(max((adx - 20.0) / 25.0, 0.0), 1.0) * 30.0
        gap = plus_di - minus_di if direction == "call" else minus_di - plus_di
        di_points = min(max(gap / 30.0, 0.0), 1.0) * 25.0
        chop_points = min(max((61.8 - chop) / 25.0, 0.0), 1.0) * 20.0
        extension_points = min(max(1.0 - extension / 0.85, 0.0), 1.0) * 15.0
        center = 60.0 if direction == "call" else 40.0
        rsi_points = min(max(1.0 - abs(rsi - center) / 10.0, 0.0), 1.0) * 10.0
        return round(adx_points + di_points + chop_points + extension_points + rsi_points, 4)
    except (KeyError, TypeError, ValueError, OverflowError): return None

def is_fresh_recovery_candidate(candidate, now, max_age_seconds=90):
    """Return True only when the candidate candle is within the allowed age window."""
    try:
        age = float(now) - int(candidate["candle_time"])
        return -5 <= age <= float(max_age_seconds)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False

def rank_recovery_candidates(candidates, now, max_age_seconds=90):
    """Reject stale candidates and sort by transparent 0-100 quality score."""
    ranked = []
    for candidate in candidates or []:
        try:
            if not is_fresh_recovery_candidate(candidate, now, max_age_seconds): continue
            score = recovery_quality_score(candidate)
            if score is None: continue
            item = dict(candidate); item["_recovery_quality_score"] = score
            ranked.append(item)
        except (KeyError, TypeError, ValueError, OverflowError): continue
    ranked.sort(key=lambda item: (-item["_recovery_quality_score"], int(item.get("candle_time", 0)), str(item.get("asset", ""))))
    return ranked
