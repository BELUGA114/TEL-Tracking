"""
再入概率窗口 (Reentry Probability Window) — Phase 2

在 decay_tracker 判定卫星进入 critical 阶段后启用：
1. 使用 xpropagator 将 TLE 传播到 200 km 交接点
2. 在交接点用二分法校准 mini 积分器初始速度
3. 施加切向速度扰动进行 N=200 次蒙特卡洛模拟
4. 输出再入时间的中位数 + 1σ/3σ 置信窗口
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from xpropagator_client import (
    StateVector,
    _EARTH_MU,
    _EARTH_RE,
    _parse_epoch_utc,
    _resolve_tle,
    propagate_tle,
    XPROP_HOST,
    XPROP_PORT,
)

log = logging.getLogger(__name__)

_SGP4_REF_DENSITY = 2.461e-5       # ρ₀ (kg/m³), SGP4 reference density (at sea level)
_ATMOSPHERE_SCALE_HEIGHT = 45.0    # H (km), approximate thermosphere scale height
_REENTRY_ALTITUDE_KM = 80.0        # 再入高度 (km)
_HANDOFF_ALTITUDE_KM = 200.0       # 交接高度 (km)

_HANDOFF_SEARCH_DAYS = 60
_HANDOFF_COARSE_STEP_HOURS = 6
_HANDOFF_BISECTION_SECONDS = 10.0
_HANDOFF_BISECTION_MAX_ITER = 60

_CALIBRATION_DH_DT_TOLERANCE = 1e-7    # km/s
_CALIBRATION_V0_RANGE = 0.05
_CALIBRATION_MAX_ITER = 60

_INTEGRATOR_DT = 10.0
_MAX_INTEGRATION_DAYS = 30.0

_DEFAULT_MC_SAMPLES = 200
_DEFAULT_SIGMA_V = 0.5

# 取 MSIS 在 200 km 附近的典型值，使阻力衰减率在物理合理范围内（数天量级）
_RHO_AT_HANDOFF = 1.0e-9

# SGP4 的 B* 是其内部归一化常数 ρ₀ 的函数，直接转换为弹道系数会高估 400-500 倍。
# _BSTAR_DRAG_SCALE 将结果校正到与简单指数大气模型匹配的物理合理范围，
# 剩余偏差由交接点 v0 二分校准吸收。
_BSTAR_DRAG_SCALE = 0.002

def bstar_to_ballistic_coefficient(bstar: float) -> float:
    """
    B* 转换为有效弹道系数 Cd*A/m (m²/kg)。

    SGP4 定义: B* = 0.5 * ρ₀_sgp4 * Cd * A / m
    其中 ρ₀_sgp4 = 2.461e-5 是 SGP4 内部归一化常数，并非物理密度。
    直接按 Cd*A/m = 2*B*/ρ₀_sgp4 转换会高估约 400-500 倍。

    此处施加 _BSTAR_DRAG_SCALE 将结果校正到与指数大气模型匹配的物理合理范围。
    """
    if bstar <= 0:
        return 0.0
    return _BSTAR_DRAG_SCALE * 2.0 * bstar / _SGP4_REF_DENSITY


def altitude_from_state(sv: StateVector) -> float:
    """从 ECI 状态向量计算高度 (km): |r| - R_E"""
    r_mag = math.sqrt(sv.x * sv.x + sv.y * sv.y + sv.z * sv.z)
    return r_mag - _EARTH_RE


def atmosphere_density(altitude_km: float) -> float:
    """
    指数大气密度模型。

    ρ(h) = ρ_ref * exp(-(h - h_ref) / H)

    以 200 km 为参考高度，向上下延伸。该模型在物理精度上远不如 NRLMSISE-00，
    但 v0 交接校准和 σ_v 会吸收系统性偏差。
    """
    return _RHO_AT_HANDOFF * math.exp(
        -(altitude_km - _HANDOFF_ALTITUDE_KM) / _ATMOSPHERE_SCALE_HEIGHT
    )


def _rk4_derivatives(state: list[float], ballistic_coeff: float) -> list[float]:
    """
    ECI 状态向量的时间导数。

    state = [x, y, z, vx, vy, vz]  (km, km/s)
    返回 [vx, vy, vz, ax, ay, az]  (km/s, km/s²)

    受力: 点质量引力 + 指数大气阻力
    """
    x, y, z, vx, vy, vz = state

    r = math.sqrt(x * x + y * y + z * z)
    v = math.sqrt(vx * vx + vy * vy + vz * vz)

    mu_over_r3 = _EARTH_MU / (r * r * r)
    ax_g = -mu_over_r3 * x
    ay_g = -mu_over_r3 * y
    az_g = -mu_over_r3 * z

    alt = r - _EARTH_RE
    if alt <= _REENTRY_ALTITUDE_KM or ballistic_coeff <= 0 or v < 1e-15:
        ax_d = ay_d = az_d = 0.0
    else:
        rho = atmosphere_density(alt)
        # 推导: a_drag (km/s²) = 0.5 * Cd*A/m * ρ * (v_km*1000)² / 1000 = 500 * Cd*A/m * ρ * v_km²
        drag_mag = 500.0 * ballistic_coeff * rho * v * v
        ax_d = -drag_mag * (vx / v)
        ay_d = -drag_mag * (vy / v)
        az_d = -drag_mag * (vz / v)

    return [vx, vy, vz, ax_g + ax_d, ay_g + ay_d, az_g + az_d]


def rk4_step(state: list[float], ballistic_coeff: float, dt: float) -> list[float]:
    """单步 RK4 积分，返回新的 6 元素状态向量 [x, y, z, vx, vy, vz]"""
    s = state

    k1 = _rk4_derivatives(s, ballistic_coeff)
    k2 = _rk4_derivatives([s[i] + 0.5 * dt * k1[i] for i in range(6)], ballistic_coeff)
    k3 = _rk4_derivatives([s[i] + 0.5 * dt * k2[i] for i in range(6)], ballistic_coeff)
    k4 = _rk4_derivatives([s[i] + dt * k3[i] for i in range(6)], ballistic_coeff)

    return [
        s[i] + (dt / 6.0) * (k1[i] + 2.0 * k2[i] + 2.0 * k3[i] + k4[i])
        for i in range(6)
    ]


def integrate_to_altitude(
    state_0: list[float],
    ballistic_coeff: float,
    target_altitude_km: float,
    dt: float = _INTEGRATOR_DT,
    max_days: float = _MAX_INTEGRATION_DAYS,
) -> tuple[list[float], float]:
    """
    从初始状态积分到高度穿越目标值。

    Args:
        state_0: 初始状态 [x, y, z, vx, vy, vz] (km, km/s)
        ballistic_coeff: Cd*A/m (m²/kg)
        target_altitude_km: 积分停止高度
        dt: 积分步长 (s)
        max_days: 最大积分天数（防止跑飞）

    Returns:
        (final_state, elapsed_seconds)
        如果超时未到达目标高度，返回超时时刻的状态和已用时间。
    """
    max_seconds = max_days * 86400.0
    state = list(state_0)
    elapsed = 0.0

    alt = altitude_from_state(StateVector(*state))
    direction = -1.0 if alt > target_altitude_km else 1.0

    while elapsed < max_seconds:
        alt = altitude_from_state(StateVector(*state))
        if (direction < 0 and alt <= target_altitude_km) or \
           (direction > 0 and alt >= target_altitude_km):
            break
        state = rk4_step(state, ballistic_coeff, dt)
        elapsed += dt

    return state, elapsed


def find_200km_handoff(
    norad_id: int,
    name: str,
    tle1: str,
    tle2: str,
    epoch_dt: datetime,
    host: str = XPROP_HOST,
    port: int = XPROP_PORT,
) -> Optional[tuple[datetime, StateVector]]:
    """
    使用 xpropagator 找到卫星穿越 200 km 交接高度的时刻。

    算法:
    1. 从历元开始，6 小时间隔粗扫描
    2. 找到穿越 200 km 的时间区间
    3. 二分法收敛到 10 秒精度

    Returns:
        (handoff_datetime, StateVector) 或 None（搜索失败时）
    """
    coarse_step = timedelta(hours=_HANDOFF_COARSE_STEP_HOURS)
    max_duration = timedelta(days=_HANDOFF_SEARCH_DAYS)

    sv_epoch = propagate_tle(norad_id, name, tle1, tle2, epoch_dt, host, port)
    if sv_epoch is None:
        log.warning("[%d] 交接点搜索: 历元传播失败", norad_id)
        return None
    alt_epoch = altitude_from_state(sv_epoch)

    if alt_epoch <= _HANDOFF_ALTITUDE_KM:
        return epoch_dt, sv_epoch

    t_prev = epoch_dt
    sv_prev = sv_epoch
    t_max = epoch_dt + max_duration
    t_curr = epoch_dt + coarse_step

    while t_curr <= t_max:
        sv_curr = propagate_tle(norad_id, name, tle1, tle2, t_curr, host, port)
        if sv_curr is None:
            return None
        alt_curr = altitude_from_state(sv_curr)

        if alt_curr <= _HANDOFF_ALTITUDE_KM:
            return _bisect_handoff(
                norad_id, name, tle1, tle2,
                t_prev, t_curr, host, port,
            )

        t_prev, sv_prev = t_curr, sv_curr
        t_curr += coarse_step

    log.warning("[%d] 交接点搜索: %d 天内未找到 200 km 穿越",
                norad_id, _HANDOFF_SEARCH_DAYS)
    return None


def _bisect_handoff(
    norad_id: int,
    name: str,
    tle1: str,
    tle2: str,
    t_lo: datetime,       # alt > 200 km
    t_hi: datetime,       # alt <= 200 km
    host: str,
    port: int,
) -> Optional[tuple[datetime, StateVector]]:
    """在 [t_lo, t_hi] 区间二分收敛到 10 秒精度。"""
    for _ in range(_HANDOFF_BISECTION_MAX_ITER):
        mid = t_lo + (t_hi - t_lo) / 2
        sv_mid = propagate_tle(norad_id, name, tle1, tle2, mid, host, port)
        if sv_mid is None:
            return None
        alt_mid = altitude_from_state(sv_mid)

        if (t_hi - t_lo).total_seconds() <= _HANDOFF_BISECTION_SECONDS:
            return mid, sv_mid

        if alt_mid > _HANDOFF_ALTITUDE_KM:
            t_lo = mid
        else:
            t_hi = mid

    sv_final = propagate_tle(norad_id, name, tle1, tle2, t_hi, host, port)
    return (t_hi, sv_final) if sv_final else None


def calibrate_mini_integrator(
    handoff_state: StateVector,
    ballistic_coeff: float,
    dt: float = _INTEGRATOR_DT,
) -> float:
    """
    用二分法校准 mini 积分器初始速度 v0。

    使 mini 积分器在 ~0.5 轨道周期内的平均 dh/dt 与 SGP4 在交接点
    处的瞬时径向速率一致，从而保证两条轨迹的衰减趋势匹配。

    Args:
        handoff_state: SGP4 在 200 km 交接点的状态向量
        ballistic_coeff: Cd*A/m (m²/kg)
        dt: 积分步长 (s)

    Returns:
        校准后的 v0 (km/s)
    """
    # dh/dt_sgp4 = (R·V) / |R|，交接点处径向速率，负值表示下降
    r_dot_v = (handoff_state.x * handoff_state.vx
               + handoff_state.y * handoff_state.vy
               + handoff_state.z * handoff_state.vz)
    r_mag = math.sqrt(handoff_state.x**2 + handoff_state.y**2 + handoff_state.z**2)
    dh_dt_sgp4 = r_dot_v / r_mag

    v_mag = math.sqrt(handoff_state.vx**2 + handoff_state.vy**2 + handoff_state.vz**2)
    if v_mag < 1e-15:
        return v_mag

    v_hat = [handoff_state.vx / v_mag, handoff_state.vy / v_mag, handoff_state.vz / v_mag]

    sma = 1.0 / (2.0 / r_mag - v_mag * v_mag / _EARTH_MU)
    if sma <= 0:
        return v_mag
    orbit_period = 2.0 * math.pi * math.sqrt(sma**3 / _EARTH_MU)
    half_orbit_steps = max(1, int(orbit_period / (2.0 * dt)))

    pos = [handoff_state.x, handoff_state.y, handoff_state.z]

    v0_lo = v_mag * (1.0 - _CALIBRATION_V0_RANGE)
    v0_hi = v_mag * (1.0 + _CALIBRATION_V0_RANGE)

    for _ in range(_CALIBRATION_MAX_ITER):
        v0_mid = (v0_lo + v0_hi) / 2.0

        state = [pos[0], pos[1], pos[2],
                 v0_mid * v_hat[0], v0_mid * v_hat[1], v0_mid * v_hat[2]]
        for _ in range(half_orbit_steps):
            state = rk4_step(state, ballistic_coeff, dt)

        r_final = math.sqrt(state[0]**2 + state[1]**2 + state[2]**2)
        dh_dt_mini = (r_final - r_mag) / (half_orbit_steps * dt)

        if abs(dh_dt_mini - dh_dt_sgp4) < _CALIBRATION_DH_DT_TOLERANCE:
            return v0_mid

        # 速度越高离心力越大，高度下降越慢，因此 dh/dt 越不"负"
        if dh_dt_mini > dh_dt_sgp4:
            v0_hi = v0_mid
        else:
            v0_lo = v0_mid

    return (v0_lo + v0_hi) / 2.0


@dataclass
class ReentryResult:
    """单颗卫星的再入概率窗口结果"""
    norad_id: int
    name: str
    handoff_time: datetime                    # 到达 200 km 的时刻
    handoff_state: StateVector                # 交接点状态向量
    calibrated_v0: float                      # 校准后的初始速率 (km/s)
    sigma_v_ms: float                         # 速度扰动标准差 (m/s)
    mc_samples: int                           # 蒙特卡洛样本数
    reentry_times: list[float]                # 每个样本的再入时间（相对 handoff_time 的秒数）
    median_seconds: float                     # 中位数再入时间 (s)
    p16_seconds: float                        # 16th 百分位 (≈ -1σ) (s)
    p84_seconds: float                        # 84th 百分位 (≈ +1σ) (s)
    p0_15_seconds: float                      # 0.15th 百分位 (≈ -3σ) (s)
    p99_85_seconds: float                     # 99.85th 百分位 (≈ +3σ) (s)
    median_time: datetime                     # 中位数再入时刻
    window_1sigma_low: datetime               # 1σ 下限
    window_1sigma_high: datetime              # 1σ 上限
    window_3sigma_low: datetime               # 3σ 下限
    window_3sigma_high: datetime              # 3σ 上限
    fallback: bool = False                    # 是否使用了回退估算


def _percentile(sorted_data: list[float], pct: float) -> float:
    """百分位线性插值（C = 1 类型）。"""
    n = len(sorted_data)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_data[0]
    k = (pct / 100.0) * (n - 1)
    f = int(math.floor(k))
    c = int(math.ceil(k))
    if f == c:
        return sorted_data[f]
    return sorted_data[f] * (c - k) + sorted_data[c] * (k - f)


def run_monte_carlo(
    handoff_state: StateVector,
    calibrated_v0: float,
    ballistic_coeff: float,
    sigma_v_ms: float = _DEFAULT_SIGMA_V,
    n_samples: int = _DEFAULT_MC_SAMPLES,
    seed: Optional[int] = None,
) -> list[float]:
    """切向速度扰动蒙特卡洛模拟，返回各样本的再入时间（距 handoff 的秒数）。"""
    rng = random.Random(seed)
    sigma_v_km = sigma_v_ms * 0.001

    v_mag = math.sqrt(handoff_state.vx**2 + handoff_state.vy**2 + handoff_state.vz**2)
    if v_mag < 1e-15:
        return []
    v_hat = [handoff_state.vx / v_mag, handoff_state.vy / v_mag, handoff_state.vz / v_mag]
    pos = [handoff_state.x, handoff_state.y, handoff_state.z]

    reentry_times = []
    for _ in range(n_samples):
        delta_v = rng.gauss(0.0, sigma_v_km)
        v = calibrated_v0 + delta_v
        state = [pos[0], pos[1], pos[2],
                 v * v_hat[0], v * v_hat[1], v * v_hat[2]]
        final_state, elapsed = integrate_to_altitude(state, ballistic_coeff,
                                                     _REENTRY_ALTITUDE_KM)
        if altitude_from_state(StateVector(*final_state)) <= _REENTRY_ALTITUDE_KM:
            reentry_times.append(elapsed)

    if not reentry_times:
        log.warning("MC: 全部 %d 条轨迹均未在 %d 天内到达再入高度",
                    n_samples, _MAX_INTEGRATION_DAYS)
    return reentry_times


def compute_reentry_window(
    orbit: dict,
    data_file: str = "",
    sigma_v_ms: Optional[float] = None,
    n_samples: int = _DEFAULT_MC_SAMPLES,
    host: str = XPROP_HOST,
    port: int = XPROP_PORT,
) -> Optional[ReentryResult]:
    """编排再入概率窗口完整流程，返回 ReentryResult 或 None（任一步骤失败时）。"""
    norad_id = orbit.get("norad", 0)
    name = orbit.get("name", "")

    tles = _resolve_tle(orbit)
    if tles is None:
        log.warning("[%d] 再入窗口: 无可用 TLE", norad_id)
        return None
    tle1, tle2 = tles

    epoch_dt = _parse_epoch_utc(orbit.get("epoch", ""))
    if epoch_dt is None:
        log.warning("[%d] 再入窗口: 无效历元", norad_id)
        return None

    handoff = find_200km_handoff(norad_id, name, tle1, tle2, epoch_dt, host, port)
    if handoff is None:
        log.warning("[%d] 再入窗口: 未找到 200 km 交接点", norad_id)
        return None
    handoff_time, handoff_state = handoff

    bstar = orbit.get("bstar", 0.0)
    ballistic_coeff = bstar_to_ballistic_coefficient(bstar)

    calibrated_v0 = calibrate_mini_integrator(handoff_state, ballistic_coeff)
    log.info("[%d] 校准 v0 = %.6f km/s", norad_id, calibrated_v0)

    if sigma_v_ms is None:
        sigma_v_ms = _DEFAULT_SIGMA_V

    reentry_times = run_monte_carlo(handoff_state, calibrated_v0, ballistic_coeff,
                                    sigma_v_ms, n_samples)
    if not reentry_times:
        log.warning("[%d] 再入窗口: 无有效再入轨迹", norad_id)
        return None

    sorted_times = sorted(reentry_times)
    median_seconds = _percentile(sorted_times, 50.0)
    p16 = _percentile(sorted_times, 16.0)
    p84 = _percentile(sorted_times, 84.0)
    p0_15 = _percentile(sorted_times, 0.15)
    p99_85 = _percentile(sorted_times, 99.85)

    return ReentryResult(
        norad_id=norad_id,
        name=name,
        handoff_time=handoff_time,
        handoff_state=handoff_state,
        calibrated_v0=calibrated_v0,
        sigma_v_ms=sigma_v_ms,
        mc_samples=n_samples,
        reentry_times=reentry_times,
        median_seconds=median_seconds,
        p16_seconds=p16,
        p84_seconds=p84,
        p0_15_seconds=p0_15,
        p99_85_seconds=p99_85,
        median_time=handoff_time + timedelta(seconds=median_seconds),
        window_1sigma_low=handoff_time + timedelta(seconds=p16),
        window_1sigma_high=handoff_time + timedelta(seconds=p84),
        window_3sigma_low=handoff_time + timedelta(seconds=p0_15),
        window_3sigma_high=handoff_time + timedelta(seconds=p99_85),
        fallback=False,
    )


def format_reentry_window(result: ReentryResult) -> str:
    """格式化再入概率窗口结果为可读字符串。"""
    valid = len(result.reentry_times)
    lines = [
        f"卫星: {result.name} (NORAD {result.norad_id})",
        f"交接时刻: {result.handoff_time.isoformat()}",
        f"校准 v0: {result.calibrated_v0:.6f} km/s",
        f"σ_v: {result.sigma_v_ms:.3f} m/s",
        f"MC 样本: {valid}/{result.mc_samples} 条有效",
        f"再入中位数: {result.median_time.isoformat()} (T+{result.median_seconds:.0f}s)",
        f"1σ 窗口: {result.window_1sigma_low.isoformat()} ~ {result.window_1sigma_high.isoformat()}",
        f"3σ 窗口: {result.window_3sigma_low.isoformat()} ~ {result.window_3sigma_high.isoformat()}",
    ]
    return "\n".join(lines)