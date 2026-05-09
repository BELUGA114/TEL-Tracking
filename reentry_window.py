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
from typing import Optional, Callable

from xpropagator_client import (
    StateVector,
    _EARTH_MU,
    _EARTH_RE,
)

log = logging.getLogger(__name__)

# ── 物理常数 ──────────────────────────────────────────────────────────────────────
_SGP4_REF_DENSITY = 2.461e-5       # ρ₀ (kg/m³), SGP4 reference density (at sea level)
_ATMOSPHERE_SCALE_HEIGHT = 45.0    # H (km), approximate thermosphere scale height
_REENTRY_ALTITUDE_KM = 80.0        # 再入高度 (km)
_HANDOFF_ALTITUDE_KM = 200.0       # 交接高度 (km)

# ── 积分器参数 ─────────────────────────────────────────────────────────────────────
_INTEGRATOR_DT = 10.0              # RK4 步长 (s)
_MAX_INTEGRATION_DAYS = 30.0       # 最大积分天数（防止跑飞）

# ── 蒙特卡洛参数 ───────────────────────────────────────────────────────────────────
_DEFAULT_MC_SAMPLES = 200
_DEFAULT_SIGMA_V = 0.5             # 默认 σ_v (m/s)，未校准时使用

# ── 参考密度 — 指数大气模型在校准高度处的密度 ────────────────────────────────────
# 取 MSIS 在 200 km 附近的典型值，使阻力衰减率在物理合理范围内（数天量级）
_RHO_AT_HANDOFF = 1.0e-9           # kg/m³ at 200 km (≈ MSIS moderate activity)

# ── B* → 弹道系数转换 ─────────────────────────────────────────────────────────────
# SGP4 的 B* 是针对其内部 Jaachia 多项式大气模型拟合的参数，ρ₀=2.461e-5 是
# 模型内部归一化常数，并非物理海平面密度。直接将 B* 按公式 Cd*A/m=2*B*/ρ₀
# 转换为弹道系数会高估 400-500 倍。此处引入有效尺度因子，将阻力调整到与
# 简单指数大气模型匹配的物理合理范围（200 km 处衰减时标为数天量级）。
# 剩余的偏差由交接点 v0 二分校准吸收。
_BSTAR_DRAG_SCALE = 0.002          # B* 到有效弹道系数的物理尺度校正


# ═══════════════════════════════════════════════════════════════════════════════════
# 物理工具函数
# ═══════════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════════
# RK4 数值积分器
# ═══════════════════════════════════════════════════════════════════════════════════

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

    # 引力加速度 (km/s²)
    mu_over_r3 = _EARTH_MU / (r * r * r)
    ax_g = -mu_over_r3 * x
    ay_g = -mu_over_r3 * y
    az_g = -mu_over_r3 * z

    # 阻力加速度 (km/s²)
    alt = r - _EARTH_RE
    if alt <= _REENTRY_ALTITUDE_KM or ballistic_coeff <= 0 or v < 1e-15:
        ax_d = ay_d = az_d = 0.0
    else:
        rho = atmosphere_density(alt)
        # a_drag (m/s²) = 0.5 * Cd*A/m * ρ * v²   (v in m/s)
        # a_drag (km/s²) = a_drag (m/s²) / 1000
        #                 = 0.5 * Cd*A/m * ρ * (v_km * 1000)² / 1000
        #                 = 500 * Cd*A/m * ρ * v_km²
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


# ═══════════════════════════════════════════════════════════════════════════════════
# 数据类
# ═══════════════════════════════════════════════════════════════════════════════════

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
