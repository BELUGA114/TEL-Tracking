"""
xpropagator gRPC 客户端插件式封装

依赖：grpcio grpcio-tools

Proto 存根生成说明：
  如果 api/v1/ 目录中已有 proto 文件和生成的 Python 存根，无需重新生成。
  
  如需从 xpropagator 仓库更新 proto 定义并重新生成：
  
  1. 获取最新 proto 文件（替换现有文件）：
     git clone https://github.com/xpropagation/xpropagator.git _xprop_src
     cp _xprop_src/api/v1/*.proto api/v1/
     cp _xprop_src/api/v1/core/*.proto api/v1/core/
     rm -rf _xprop_src

  2. 重新生成 Python gRPC 存根（PowerShell，在项目根目录执行）：
     python -m grpc_tools.protoc `
         -I . `
         --python_out=. `
         --grpc_python_out=. `
         api/v1/common.proto `
         api/v1/info.proto `
         api/v1/main.proto `
         (Get-ChildItem api/v1/core/*.proto | ForEach-Object { $_.FullName.Replace((Get-Location).Path + "\", "") })

  3. Linux/macOS bash 等价命令：
     python -m grpc_tools.protoc \\
         -I . \\
         --python_out=. \\
         --grpc_python_out=. \\
         api/v1/common.proto \\
         api/v1/info.proto \\
         api/v1/main.proto \\
         api/v1/core/*.proto
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import NamedTuple, Optional

log = logging.getLogger(__name__)

# ── 懒加载 gRPC 存根 ─────────────────────────────────────────────────────────────
# 未安装 grpcio 或未编译 proto 时，主脚本仍可正常运行（降级到简单 5km 规则）
_GRPC_AVAILABLE = False
try:
    import grpc
    from google.protobuf import empty_pb2
    from google.protobuf.timestamp_pb2 import Timestamp
    from api.v1 import main_pb2_grpc as pb2_grpc
    from api.v1.core import prop_pb2              # PropRequest, PropTask, TimeType
    from api.v1 import common_pb2                 # Satellite, EphemerisData
    _GRPC_AVAILABLE = True
    log.debug("xpropagator gRPC 存根加载成功")
except ImportError as _e:
    _GRPC_AVAILABLE = False
    log.debug("xpropagator 存根未就绪（%s），残差分析不可用", _e)


# ── 默认连接参数 ──────────────────────────────────────────────────────────────────
XPROP_HOST: str   = "localhost"
XPROP_PORT: int   = 50051
_CONNECT_TIMEOUT: float = 3.0    # gRPC 连接超时（秒）
_CALL_TIMEOUT:    float = 10.0   # 单次 RPC 调用超时（秒）


class StateVector(NamedTuple):
    """ECI 笛卡尔状态向量（km, km/s）"""
    x:  float
    y:  float
    z:  float
    vx: float
    vy: float
    vz: float


# ── 内部工具函数 ──────────────────────────────────────────────────────────────────

def _parse_epoch_utc(epoch_str: str) -> Optional[datetime]:
    # 解析 EPOCH 字符串为 UTC
    if not epoch_str:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(epoch_str, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    log.debug("xprop: 无法解析历元字符串: %s", epoch_str)
    return None


def _dt_to_pb_timestamp(dt: datetime) -> "Timestamp":
    # 将 Python datetime（带 tzinfo）转换为 protobuf Timestamp
    ts = Timestamp()
    ts.FromDatetime(dt.astimezone(timezone.utc))
    return ts


# ── 核心 API ──────────────────────────────────────────────────────────────────────

def propagate_tle(
    norad_id:    int,
    name:        str,
    tle1:        str,
    tle2:        str,
    target_time: datetime,
    host:        str = XPROP_HOST,
    port:        int = XPROP_PORT,
) -> Optional[StateVector]:
    """
    调用 xpropagator Prop RPC，将 TLE 传播到 target_time（UTC）。
    返回：ECI 状态向量（km / km·s⁻¹），失败时返回 None。
    每次调用都创建新 channel（满足插件式/无状态设计，避免长连接管理）。
    
    注意：使用唯一 norad_id + req_id 组合以绕过服务端缓存机制。
    """
    if not _GRPC_AVAILABLE:
        return None

    try:
        channel = grpc.insecure_channel(
            f"{host}:{port}",
            options=[("grpc.connect_timeout_ms", int(_CONNECT_TIMEOUT * 1000))],
        )
        stub = pb2_grpc.PropagatorStub(channel)

        # 生成唯一标识以绕过服务端缓存
        # 使用时间戳微秒级 + TLE 哈希作为虚拟 norad_id
        virtual_norad = abs(hash((tle1, tle2, target_time.isoformat()))) % 900000 + 100000
        req_id = int(time.time() * 1000000)
        
        request = prop_pb2.PropRequest(
            req_id=req_id,
            time_type=prop_pb2.TimeMse,  # 使用 MSE (Mean Solar Ephemeris) 时间类型
            task=prop_pb2.PropTask(
                time_utc=_dt_to_pb_timestamp(target_time),
                sat=common_pb2.Satellite(  # ← 从 common_pb2 取
                    norad_id=virtual_norad,  # 使用虚拟 NORAD ID 强制创建新实例
                    name=name,
                    tle_ln1=tle1,
                    tle_ln2=tle2,
                ),
            ),
        )
        resp = stub.Prop(request, timeout=_CALL_TIMEOUT)
        r = resp.result
        return StateVector(r.x, r.y, r.z, r.vx, r.vy, r.vz)

    except Exception as exc:
        log.warning("xpropagator RPC 失败 [NORAD %d @ %s]: %s",
                    norad_id, target_time.isoformat(), exc)
        return None
    finally:
        try:
            channel.close()
        except Exception:
            pass


def position_residual_km(sv_a: StateVector, sv_b: StateVector) -> float:
    """计算两个状态向量间的位置残差（欧氏距离，km）"""
    return math.sqrt(
        (sv_a.x - sv_b.x) ** 2
        + (sv_a.y - sv_b.y) ** 2
        + (sv_a.z - sv_b.z) ** 2
    )


def classify_change_xprop(
    orbit:                  dict,
    prev:                   dict,
    maneuver_threshold_km:  float = 5.0,
    host:                   str   = XPROP_HOST,
    port:                   int   = XPROP_PORT,
) -> Optional[str]:
    """
    使用 xpropagator（USSF SGP4/SGP4-XP）做残差分析判断 TLE 变化性质。

    比较时刻选取：新 TLE 的历元（两 TLE 最接近的公共参考时刻）

    残差含义：
      Δr ≥ maneuver_threshold_km  →  "maneuver"   （疑似真实机动）
      Δr <  maneuver_threshold_km  →  "correction" （疑似解算修正/噪声）

    返回 None 表示 xpropagator 服务不可用，主脚本应降级到简单规则。
    """
    if not _GRPC_AVAILABLE:
        return None

    # 以新 TLE 历元作为公共比较时刻
    epoch_dt = _parse_epoch_utc(orbit.get("epoch", ""))
    if epoch_dt is None:
        return None

    norad_id = orbit["norad"]
    name     = orbit.get("name", "")

    # ① 旧 TLE 预报到新历元 → "如果没有机动，卫星应在哪里"
    sv_predicted = propagate_tle(
        norad_id, name,
        prev["tle1"], prev["tle2"],
        epoch_dt, host, port,
    )
    if sv_predicted is None:
        return None

    # ② 新 TLE 在其历元时刻的初始状态（MSE=0 点，即 TLE 定义的参考状态）
    sv_new_epoch = propagate_tle(
        norad_id, name,
        orbit["tle1"], orbit["tle2"],
        epoch_dt, host, port,
    )
    if sv_new_epoch is None:
        return None

    delta_km = position_residual_km(sv_predicted, sv_new_epoch)
    verdict  = "maneuver" if delta_km >= maneuver_threshold_km else "correction"

    log.info(
        "[%d] xprop 残差 @ %s：Δr = %.3f km（阈值 %.1f km）→ %s",
        norad_id,
        epoch_dt.strftime("%Y-%m-%dT%H:%MZ"),
        delta_km,
        maneuver_threshold_km,
        verdict.upper(),
    )
    return verdict


def is_service_alive(host: str = XPROP_HOST, port: int = XPROP_PORT) -> bool:
    """
    探活：检查 xpropagator 服务是否响应（调用 Info RPC）
    可在启动时调用一次，用于日志提示。
    """
    if not _GRPC_AVAILABLE:
        return False
    try:
        channel = grpc.insecure_channel(f"{host}:{port}")
        stub = pb2_grpc.PropagatorStub(channel)
        resp = stub.Info(empty_pb2.Empty(), timeout=_CONNECT_TIMEOUT)
        channel.close()
        log.info("xpropagator 已连接：%s %s", resp.name, resp.version)
        return True
    except Exception as exc:
        log.debug("xpropagator 服务探活失败: %s", exc)
        return False
