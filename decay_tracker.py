"""
轨道衰降追踪器 (Decay Tracker) — Phase 1

识别和跟踪轨道衰降趋势：
- 使用 xpropagator 前向传播判断轨道是否持续且显著地下坠
- 挖掘 tle_data.jsonl 中的历史数据，分析 B*/近地点/平均运动趋势
- 多级告警：early_decay → accelerating → critical
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# ── xpropagator 懒加载（可选依赖）───────────────────────────────────────────
try:
    from xpropagator_client import (
        is_grpc_available as _xprop_is_grpc_available,
        propagate_and_check_decay_trend as _xprop_check_trend,
    )
except ImportError:
    _xprop_is_grpc_available = lambda: False       # type: ignore[no-redef]
    _xprop_check_trend = None                       # type: ignore[assignment]

# ── 阈值配置 ──────────────────────────────────────────────────────────────────
DECAY_APOAPSIS_THRESHOLD_KM = 300.0    # 远地点低于此值时启动衰降追踪（回退模式）
DECAY_HISTORY_DAYS = 60                # 历史数据回溯天数
TREND_SHORT_WINDOW_DAYS = 14           # 短期趋势窗口
TREND_LONG_WINDOW_DAYS = 30            # 长期趋势窗口
DECAY_CRITICAL_PERIAPSIS_KM = 200.0    # 近地点低于此值进入危险阶段
BSTAR_ACCELERATION_FACTOR = 3.0        # 短期均值超长期均值 N 倍视为加速
MIN_DATA_POINTS = 5                    # 最小数据点数，低于此值不做统计推断

# ── 物理传播触发参数 ─────────────────────────────────────────────────────────
SENSITIVE_ZONE_KM = 300.0              # 大气敏感区上界（远地点高于此值视为安全）
PERIAPSIS_FAST_TRACK_KM = 200.0        # 近地点低于此值直接进入全面分析
PROPAGATION_FORECAST_DAYS = 30         # 前向传播预报天数
PROPAGATION_STEP_DAYS = 3              # 传播时间步长（天）
FORWARD_TRIGGER_MIN_DECLINE_KM = 10.0  # 30天内远地点累计下降 >= 此值视为显著
FORWARD_TRIGGER_MIN_R2 = 0.5           # 远地点趋势 R^2 >= 此值视为趋势可信
FORWARD_TRIGGER_TERMINAL_APO_KM = 250.0  # 最终远地点低于此值直接触发

# ── 衰降阶段 ──────────────────────────────────────────────────────────────────

class DecayPhase:
    NORMAL = "normal"              # 正常轨道（前向传播未检测到显著衰降趋势）
    EARLY = "early_decay"          # 早期衰减（低于阈值，无明确加速迹象）
    ACCELERATING = "accelerating"  # 加速衰减（B* 持续增大 + 近地点持续下降）
    CRITICAL = "critical"          # 危险（近地点 < 临界值，加速趋势明确）

PHASE_LABELS = {
    DecayPhase.NORMAL: "正常",
    DecayPhase.EARLY: "早期衰减",
    DecayPhase.ACCELERATING: "加速衰减",
    DecayPhase.CRITICAL: "危险",
}


def _should_trigger_decay_analysis(orbit: dict) -> tuple[bool, Optional[dict]]:
    """
    Decide whether to activate full decay analysis using physics-based
    forward propagation with graceful fallback to static threshold.

    Decision ladder (ordered by cost, cheapest first):
      1. periapsis < PERIAPSIS_FAST_TRACK_KM    → True  (definitely decaying)
      2. apoapsis >= SENSITIVE_ZONE_KM           → False (definitely safe)
      3. xprop not available → fall back to static apoapsis < 300 km check
      4. xprop available → forward-propagate and check trend

    Returns:
        tuple of (should_trigger: bool, propagation_detail: Optional[dict])
    """
    apoapsis = float(orbit.get("apoapsis", 0))
    periapsis = float(orbit.get("periapsis", 0))
    norad_id = int(orbit.get("norad", 0))

    # Fast-track: periapsis already deep in atmosphere
    if periapsis < PERIAPSIS_FAST_TRACK_KM:
        log.debug("[%d] 衰降触发: 近地点快车道 (%.1f km < %.0f km)",
                  norad_id, periapsis, PERIAPSIS_FAST_TRACK_KM)
        return True, None

    # Safe: apoapsis above sensitive zone
    if apoapsis >= SENSITIVE_ZONE_KM:
        log.debug("[%d] 衰降跳过: 远地点在敏感区以上 (%.1f km >= %.0f km)",
                  norad_id, apoapsis, SENSITIVE_ZONE_KM)
        return False, None

    # Ambiguous zone: need propagation
    if not _xprop_is_grpc_available() or _xprop_check_trend is None:
        # Fallback to static threshold
        if apoapsis < DECAY_APOAPSIS_THRESHOLD_KM:
            log.debug("[%d] 衰降触发: 回退静态阈值 (xprop 不可用, %.1f km < %.0f km)",
                      norad_id, apoapsis, DECAY_APOAPSIS_THRESHOLD_KM)
            return True, None
        log.debug("[%d] 衰降跳过: 回退静态阈值 (xprop 不可用, %.1f km >= %.0f km)",
                  norad_id, apoapsis, DECAY_APOAPSIS_THRESHOLD_KM)
        return False, None

    # Resolve TLE from orbit dict
    try:
        from xpropagator_client import _resolve_tle, _parse_epoch_utc
    except ImportError:
        log.debug("[%d] 衰降触发: xprop 导入失败，保守触发", norad_id)
        return apoapsis < DECAY_APOAPSIS_THRESHOLD_KM, None

    tle_result = _resolve_tle(orbit)
    if tle_result is None:
        # Cannot extract TLE — conservative: trigger analysis
        log.debug("[%d] 衰降触发: TLE 提取失败，保守触发", norad_id)
        return True, None
    tle1, tle2 = tle_result

    epoch_dt = _parse_epoch_utc(orbit.get("epoch", ""))
    if epoch_dt is None:
        log.debug("[%d] 衰降触发: 历元解析失败，保守触发", norad_id)
        return True, None

    name = str(orbit.get("name", ""))

    log.debug("[%d] 前向传播检查: %d 天窗口, %d 天步长",
              norad_id, PROPAGATION_FORECAST_DAYS, PROPAGATION_STEP_DAYS)
    prop_result = _xprop_check_trend(
        norad_id, name, tle1, tle2, epoch_dt,
        forecast_days=PROPAGATION_FORECAST_DAYS,
        step_days=PROPAGATION_STEP_DAYS,
        min_decline_km=FORWARD_TRIGGER_MIN_DECLINE_KM,
        min_r_squared=FORWARD_TRIGGER_MIN_R2,
        terminal_apo_threshold_km=FORWARD_TRIGGER_TERMINAL_APO_KM,
    )

    if prop_result is None:
        # All propagation failed — conservative: trigger
        log.debug("[%d] 衰降触发: 前向传播全部失败，保守触发", norad_id)
        return True, None

    triggered = prop_result["triggered"]
    log.debug("[%d] 前向传播完成: %d 个有效点, 触发=%s",
              norad_id, prop_result["forecast_points"], triggered)
    return triggered, prop_result


def load_history(norad_id: int, data_file: str,
                days: int = DECAY_HISTORY_DAYS) -> list[dict]:
    """
    从 tle_data.jsonl 中提取指定卫星在回溯窗口内的所有记录，按时间戳升序排列。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    records: list[dict] = []

    if not data_file:
        log.debug("衰降追踪: data_file 未指定，跳过历史数据加载 [NORAD %d]", norad_id)
        return records
    if not os.path.exists(data_file):
        log.info("衰降追踪: data_file 不存在 (%s)，跳过历史数据加载 [NORAD %d]",
                 data_file, norad_id)
        return records

    try:
        with open(data_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("norad") != norad_id:
                    continue
                ts_str = entry.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str)
                except (ValueError, TypeError):
                    continue
                if ts < cutoff:
                    continue
                records.append(entry)
    except OSError as e:
        log.warning("衰降追踪: 读取历史数据失败 [NORAD %d]: %s", norad_id, e)
        return records

    records.sort(key=lambda r: r.get("timestamp", ""))
    return records


def _extract_time_series(records: list[dict]) -> dict:
    """从历史记录中提取 B* / 近地点 / 远地点 / 平均运动时间序列"""
    timestamps: list[datetime] = []
    bstar_series: list[float] = []
    peri_series: list[float] = []
    apo_series: list[float] = []
    mm_series: list[float] = []

    for r in records:
        ts_str = r.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue

        bstar = float(r.get("bstar", 0))
        peri = float(r.get("periapsis", 0))
        apo = float(r.get("apoapsis", 0))
        period = float(r.get("period", 0))

        if peri <= 0 or apo <= 0:
            continue

        timestamps.append(ts)
        bstar_series.append(bstar)
        peri_series.append(peri)
        apo_series.append(apo)
        mm_series.append(1440.0 / period if period > 0 else 0.0)

    return {
        "timestamps": timestamps,
        "bstar": bstar_series,
        "periapsis": peri_series,
        "apoapsis": apo_series,
        "mean_motion": mm_series,
        "count": len(timestamps),
    }


def _linear_trend(timestamps: list[datetime], values: list[float]) -> dict:
    """
    简单线性回归，返回斜率（/天）和 R^2。
    x 轴为相对天数（以第一条记录为 0）。
    """
    n = len(values)
    if n < 3:
        return {"slope": 0.0, "r_squared": 0.0, "valid": False}

    t0 = timestamps[0].timestamp()
    x = [(t.timestamp() - t0) / 86400.0 for t in timestamps]
    y = list(values)

    n_f = float(n)
    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    sum_x2 = sum(xi * xi for xi in x)
    sum_y2 = sum(yi * yi for yi in y)

    denom = n_f * sum_x2 - sum_x * sum_x
    if abs(denom) < 1e-15:
        return {"slope": 0.0, "r_squared": 0.0, "valid": False}

    slope = (n_f * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n_f

    # R^2
    mean_y = sum_y / n_f
    ss_tot = sum_y2 - n_f * mean_y * mean_y
    if ss_tot < 1e-15:
        r_squared = 1.0 if abs(slope) < 1e-15 else 0.0
    else:
        ss_res = sum((yi - (slope * xi + intercept)) ** 2
                     for xi, yi in zip(x, y))
        r_squared = max(0.0, 1.0 - ss_res / ss_tot)

    return {"slope": slope, "r_squared": r_squared, "valid": True}


def _window_stats(series: list[float], timestamps: list[datetime],
                  window_days: int) -> dict:
    """计算时间序列在最近 window_days 天内的统计量"""
    if not series or not timestamps:
        return {"mean": 0.0, "min": 0.0, "max": 0.0, "count": 0, "valid": False}

    latest = timestamps[-1]
    cutoff = latest - timedelta(days=window_days)

    windowed = [v for v, ts in zip(series, timestamps) if ts >= cutoff]

    if len(windowed) < 2:
        return {"mean": 0.0, "min": 0.0, "max": 0.0,
                "count": len(windowed), "valid": False}

    return {
        "mean": sum(windowed) / len(windowed),
        "min": min(windowed),
        "max": max(windowed),
        "count": len(windowed),
        "valid": True,
    }


def _bstar_robust_median(series: list[float], timestamps: list[datetime],
                         window_days: int) -> float:
    """计算 B* 在窗口内的中位数（比均值更抗野值）"""
    if not series or not timestamps:
        return 0.0
    latest = timestamps[-1]
    cutoff = latest - timedelta(days=window_days)
    windowed = sorted(v for v, ts in zip(series, timestamps) if ts >= cutoff)
    if not windowed:
        return 0.0
    n = len(windowed)
    if n % 2 == 1:
        return windowed[n // 2]
    return (windowed[n // 2 - 1] + windowed[n // 2]) / 2.0


def analyze_decay(orbit: dict, data_file: str) -> dict:
    """
    对一颗卫星做完整的衰降分析。

    Args:
        orbit: 当前轨道参数（prev_data 中的 dict）
        data_file: tle_data.jsonl 路径

    Returns:
        dict: phase / alert / trend 子字段
    """
    norad_id = int(orbit.get("norad", 0))
    name = str(orbit.get("name", ""))
    apoapsis = float(orbit.get("apoapsis", 0))
    periapsis = float(orbit.get("periapsis", 0))
    bstar = float(orbit.get("bstar", 0))

    result = {
        "norad_id": norad_id,
        "name": name,
        "phase": DecayPhase.NORMAL,
        "apoapsis": apoapsis,
        "periapsis": periapsis,
        "bstar_current": bstar,
        "alert": None,
        "trend": {},
    }

    # ── Physics-based decay trigger ───────────────────────────────────────────
    should_trigger, prop_detail = _should_trigger_decay_analysis(orbit)
    if not should_trigger:
        result["trend"]["trigger_method"] = (
            "physics_propagation" if prop_detail is not None else "static_threshold"
        )
        return result

    if prop_detail is not None:
        result["trend"].update({
            "trigger_method": "physics_propagation",
            "forecast_apo_series": prop_detail["apo_series"],
            "forecast_peri_series": prop_detail["peri_series"],
            "forecast_times_days": prop_detail["times_days"],
            "forecast_apo_slope_km_per_day": prop_detail["slope_km_per_day"],
            "forecast_apo_r2": prop_detail["r_squared"],
            "forecast_total_apo_decline_km": prop_detail["total_apo_decline_km"],
            "forecast_points": prop_detail["forecast_points"],
        })
    else:
        result["trend"]["trigger_method"] = "static_threshold"

    # 加载历史数据
    records = load_history(norad_id, data_file)
    trigger_info = result["trend"].get("trigger_method", "unknown")

    if len(records) < MIN_DATA_POINTS:
        result["phase"] = DecayPhase.EARLY
        result["alert"] = (
            f"[EARLY] [{norad_id}] {name}: "
            f"近地点 {periapsis:.1f} km / 远地点 {apoapsis:.1f} km，"
            f"进入衰降监视（触发方式: {trigger_info}，"
            f"历史数据 {len(records)} 条不足 {MIN_DATA_POINTS} 条，暂无法判断趋势）"
        )
        return result

    ts_data = _extract_time_series(records)
    if ts_data["count"] < MIN_DATA_POINTS:
        result["phase"] = DecayPhase.EARLY
        result["alert"] = (
            f"[EARLY] [{norad_id}] {name}: "
            f"近地点 {periapsis:.1f} km / 远地点 {apoapsis:.1f} km，"
            f"进入衰降监视（触发方式: {trigger_info}，"
            f"有效数据 {ts_data['count']} 条不足 {MIN_DATA_POINTS} 条）"
        )
        return result

    # B* 趋势 — 使用中位数比较 + 线性回归双重判断
    bstar_short_med = _bstar_robust_median(
        ts_data["bstar"], ts_data["timestamps"], TREND_SHORT_WINDOW_DAYS)
    bstar_long_med = _bstar_robust_median(
        ts_data["bstar"], ts_data["timestamps"], TREND_LONG_WINDOW_DAYS)

    bstar_full_trend = _linear_trend(ts_data["timestamps"], ts_data["bstar"])
    peri_trend = _linear_trend(ts_data["timestamps"], ts_data["periapsis"])
    mm_trend = _linear_trend(ts_data["timestamps"], ts_data["mean_motion"])

    # 加速判断：短期中位数显著高于长期中位数
    bstar_accelerating = False
    bstar_ratio = 1.0
    if bstar_long_med > 1e-15 and bstar_short_med > 1e-15:
        bstar_ratio = bstar_short_med / bstar_long_med
        if bstar_ratio >= BSTAR_ACCELERATION_FACTOR:
            bstar_accelerating = True

    # 近地点下降率显著（斜率 < 0 且 R^2 > 0.3 说明有趋势而非噪声）
    peri_declining = (peri_trend["valid"] and peri_trend["slope"] < 0
                      and peri_trend["r_squared"] > 0.3)

    # 平均运动增加（轨道收缩的独立信号）
    mm_increasing = (mm_trend["valid"] and mm_trend["slope"] > 0
                     and mm_trend["r_squared"] > 0.3)

    # 填充趋势数据（合并到已有的 trigger_method / propagation_forecast）
    result["trend"].update({
        "bstar_short_median": bstar_short_med,
        "bstar_long_median": bstar_long_med,
        "bstar_ratio": bstar_ratio,
        "bstar_full_slope": bstar_full_trend["slope"],
        "bstar_full_r2": bstar_full_trend["r_squared"],
        "peri_slope_km_per_day": peri_trend["slope"],
        "peri_r2": peri_trend["r_squared"],
        "mm_slope_rev_per_day2": mm_trend["slope"],
        "mm_r2": mm_trend["r_squared"],
        "history_days": (
            (ts_data["timestamps"][-1] - ts_data["timestamps"][0]).total_seconds()
            / 86400.0
        ) if ts_data["count"] >= 2 else 0,
        "data_points": ts_data["count"],
    })

    # 阶段判定（按危险程度从高到低判断）
    if periapsis < DECAY_CRITICAL_PERIAPSIS_KM and bstar_accelerating:
        result["phase"] = DecayPhase.CRITICAL
        rate_str = f"{peri_trend['slope']:.3f}" if peri_trend["valid"] else "?"
        result["alert"] = (
            f"[CRITICAL] [{norad_id}] {name}: "
            f"近地点 {periapsis:.1f} km < {DECAY_CRITICAL_PERIAPSIS_KM} km, "
            f"B* 加速增长 ({bstar_short_med:.4e} / {bstar_long_med:.4e} = "
            f"{bstar_ratio:.1f}x), 近地点下降率 {rate_str} km/天"
        )
    elif bstar_accelerating and (peri_declining or mm_increasing):
        result["phase"] = DecayPhase.ACCELERATING
        signals = []
        if peri_declining:
            signals.append(f"近地点下降 {peri_trend['slope']:.2f} km/天")
        if mm_increasing:
            signals.append("平均运动加速")
        result["alert"] = (
            f"[ACCELERATING] [{norad_id}] {name}: "
            f"B* 较长期增长 {bstar_ratio:.1f}x, "
            f"{', '.join(signals)}"
        )
    elif periapsis < DECAY_CRITICAL_PERIAPSIS_KM:
        result["phase"] = DecayPhase.CRITICAL
        result["alert"] = (
            f"[CRITICAL] [{norad_id}] {name}: "
            f"近地点 {periapsis:.1f} km < {DECAY_CRITICAL_PERIAPSIS_KM} km 阈值"
        )
    elif bstar_full_trend["valid"] and bstar_full_trend["slope"] > 0:
        result["phase"] = DecayPhase.EARLY
        result["alert"] = (
            f"[EARLY] [{norad_id}] {name}: "
            f"近地点 {periapsis:.1f} km / 远地点 {apoapsis:.1f} km, "
            f"B* 呈上升趋势 (斜率 {bstar_full_trend['slope']:.4e}/天, "
            f"R^2={bstar_full_trend['r_squared']:.2f})"
        )
    else:
        result["phase"] = DecayPhase.EARLY
        result["alert"] = (
            f"[EARLY] [{norad_id}] {name}: "
            f"近地点 {periapsis:.1f} km / 远地点 {apoapsis:.1f} km, "
            f"进入衰降监视"
        )

    return result


def format_decay_report(analysis: dict) -> str:
    """将衰降分析结果格式化为可读文本"""
    trend = analysis.get("trend", {})
    lines = [
        f"  ┌─ 衰降追踪 [{analysis['norad_id']}] {analysis['name']} "
        f"({PHASE_LABELS.get(analysis['phase'], analysis['phase'])})",
        f"  │ 近地点: {analysis['periapsis']:.1f} km  "
        f"远地点: {analysis['apoapsis']:.1f} km  "
        f"B*: {analysis['bstar_current']:.4e}",
    ]

    if trend:
        dp = trend.get("data_points", 0)
        hd = trend.get("history_days", 0)
        lines.append(f"  │ 数据: {dp} 条 / {hd:.1f} 天")

        if trend.get("bstar_long_median", 0) > 1e-15:
            lines.append(
                f"  │ B* 短期({TREND_SHORT_WINDOW_DAYS}d)中位数: "
                f"{trend['bstar_short_median']:.4e}  "
                f"长期({TREND_LONG_WINDOW_DAYS}d): {trend['bstar_long_median']:.4e}  "
                f"比值: {trend.get('bstar_ratio', 1.0):.2f}x"
            )

        if trend.get("peri_slope_km_per_day", 0) != 0:
            lines.append(
                f"  │ 近地点变化率: {trend['peri_slope_km_per_day']:+.3f} km/天  "
                f"(R^2={trend.get('peri_r2', 0):.3f})"
            )

        if trend.get("mm_slope_rev_per_day2", 0) != 0:
            lines.append(
                f"  │ 平均运动变化率: {trend['mm_slope_rev_per_day2']:+.6f} rev/天^2  "
                f"(R^2={trend.get('mm_r2', 0):.3f})"
            )

    lines.append(f"  └{'─' * 55}")
    return "\n".join(lines)


def export_bstar_csv(norad_id: int, data_file: str,
                     output_dir: str = ".") -> Optional[str]:
    """
    导出指定卫星的 B*/近地点/平均运动时间序列为 CSV，
    方便用户在 Excel/Python 中绘图。
    返回输出文件路径，无数据时返回 None。
    """
    records = load_history(norad_id, data_file)
    if not records:
        return None

    ts_data = _extract_time_series(records)
    if ts_data["count"] == 0:
        return None

    out_path = os.path.join(output_dir, f"decay_{norad_id}_series.csv")
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write("timestamp,bstar,periapsis_km,apoapsis_km,mean_motion_rev_per_day\n")
        for ts, b, p, a, m in zip(
            ts_data["timestamps"], ts_data["bstar"],
            ts_data["periapsis"], ts_data["apoapsis"],
            ts_data["mean_motion"],
        ):
            f.write(f"{ts.isoformat()},{b:.6e},{p:.2f},{a:.2f},{m:.8f}\n")

    log.info("衰降追踪: B* 时间序列已导出 %s (%d 条)", out_path, ts_data["count"])
    return out_path
