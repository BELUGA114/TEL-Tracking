"""
CelesTrak 单星查询封装 + 每星频率控制

接口说明：
  fetch_single(norad_id, use_supplemental, user_agent) → dict | None
    成功时返回与 Space-Track GP JSON 字段兼容的 dict（可直接传入 parse_orbit）
    失败时返回 None

频率约束：
  每颗卫星至少间隔 CELESTRAK_MIN_INTERVAL 秒才允许再次请求
  调用方应在外层再做一次全局间隔保护（写在了 spacetrack_monitor 主循环）

5位编号耗尽预案:
  fetch_single 返回结构中已保留 _raw_elements 字段存储原始根数 dict，
  供 xpropagator_client.gp_json_to_tle_lines() 在 tle1/tle2 为空时重建 TLE。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

CELESTRAK_GP_URL     = "https://celestrak.org/NORAD/elements/gp.php"
CELESTRAK_SUP_GP_URL = "https://celestrak.org/NORAD/elements/supplemental/sup-gp.php"

# 频率下限，不允许外部修改
CELESTRAK_MIN_INTERVAL: int = 7200  # 秒

# User-Agent（可选，用于标识应用身份）
# 由调用方通过 fetch_single 的 user_agent 参数传入，不再从环境变量读取
# 这样可以统一从 config.yaml 管理所有数据源的 UA 配置

# 每颗卫星的上次请求时间戳 {norad_id: monotonic_time}
_last_query: dict[int, float] = {}


def seconds_since_last_query(norad_id: int) -> float:
    """返回距该星上次 CelesTrak 查询的秒数，从未查询则返回 inf"""
    t = _last_query.get(norad_id)
    if t is None:
        return float("inf")
    return time.monotonic() - t


def _mark_queried(norad_id: int) -> None:
    _last_query[norad_id] = time.monotonic()


def fetch_single(
    norad_id: int,
    use_supplemental: bool = False,
    timeout: int = 20,
    user_agent: Optional[str] = None,
) -> Optional[dict]:
    """
    向 CelesTrak 查询单颗卫星的 GP 数据（JSON 格式）。
    返回第一条记录（dict），字段与 Space-Track GP JSON 兼容；失败返回 None。
    Args:
        norad_id: NORAD 编号
        use_supplemental: 是否使用 SupGP 接口
        timeout: 请求超时时间（秒）
        user_agent: User-Agent 字符串，可选。由调用方统一从 config.yaml 传入。
    """
    url = CELESTRAK_SUP_GP_URL if use_supplemental else CELESTRAK_GP_URL
    params = {"CATNR": str(norad_id), "FORMAT": "json"}

    for attempt in range(1, 3):  # 最多重试一次
        try:
            headers = {}
            if user_agent:
                headers["User-Agent"] = user_agent
            resp = requests.get(
                url,
                params=params,
                timeout=timeout,
                headers=headers if headers else None,
            )
        except requests.RequestException as e:
            log.warning("[CelesTrak][%d] 请求异常（第 %d 次）: %s", norad_id, attempt, e)
            if attempt == 1:
                time.sleep(5)
            continue

        if resp.status_code == 404:
            log.warning("[CelesTrak][%d] 未找到该卫星（404）", norad_id)
            _mark_queried(norad_id)
            return None

        if resp.status_code != 200:
            log.warning("[CelesTrak][%d] 非预期状态码 %d", norad_id, resp.status_code)
            if attempt == 1:
                time.sleep(5)
            continue

        try:
            data = resp.json()
        except ValueError as e:
            log.warning("[CelesTrak][%d] JSON 解析失败: %s", norad_id, e)
            _mark_queried(norad_id)
            return None

        if not isinstance(data, list) or len(data) == 0:
            log.warning("[CelesTrak][%d] 返回空列表", norad_id)
            _mark_queried(norad_id)
            return None

        record = data[0]

        # 注入来源标识，供 log_record 使用
        record["_source"] = "celestrak_sup" if use_supplemental else "celestrak"
        
        # 保留原始根数，供以下场景使用：
        # 1. 5位编号耗尽后（~2026-07-20），TLE_LINE1/2 不再提供时，用于重建 TLE
        # 2. xpropagator_client.gp_json_to_tle_lines() 在 tle1/tle2 为空时合成 TLE
        record["_raw_elements"] = {
            k: record.get(k)
            for k in ("NORAD_CAT_ID", "OBJECT_ID", "OBJECT_NAME", "EPOCH",
                      "CLASSIFICATION_TYPE", "ELEMENT_SET_NO", "EPHEMERIS_TYPE",
                      "INCLINATION", "RA_OF_ASC_NODE", "ECCENTRICITY",
                      "ARG_OF_PERICENTER", "MEAN_ANOMALY", "MEAN_MOTION",
                      "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT", "BSTAR", "REV_AT_EPOCH")
        }

        _mark_queried(norad_id)
        log.debug("[CelesTrak][%d] 获取成功: %s", norad_id, record.get("OBJECT_NAME", ""))
        return record

    log.error("[CelesTrak][%d] 连续失败，本轮放弃", norad_id)
    return None