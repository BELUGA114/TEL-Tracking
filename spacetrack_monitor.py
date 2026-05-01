#!/usr/bin/env python3
"""
Space-Track.org TLE 轨道监控脚本

脚本严格遵守 Space-Track API 使用规范

功能：
- 监控单颗或多颗卫星的 TLE 更新
- 自动检测轨道变化（基于哈希比对）
- 输出轨道参数变化（近地点 / 远地点等）
- 附带一个极其简化的再入时间估算（仅供参考）

"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import os
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from enum import Enum, auto
from typing import Optional

import requests
import yaml

# CelesTrak 拉取模块（可选，主源为 celestrak 时启用）
try:
    import celestrak_fetcher as ct
    _CT_MODULE_OK = True
except ImportError:
    ct = None           # type: ignore
    _CT_MODULE_OK = False

# 初始化日志系统（必须在配置加载之前）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

load_dotenv()

# 密钥（来自 .env）
USERNAME = os.getenv("SPACETRACK_USER")
PASSWORD = os.getenv("SPACETRACK_PASS")

# 业务配置（来自 config.yaml）

def _load_config(path: str = "config.yaml") -> dict:
    """加载 YAML 配置文件,文件不存在时返回空 dict(全部使用默认值)"""
    # 支持从任意目录运行脚本,自动定位到脚本所在目录的 config.yaml
    if not os.path.isabs(path):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(script_dir, path)
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        logging.getLogger(__name__).debug("已加载配置文件:%s", path)
        return cfg
    except FileNotFoundError:
        # 配置文件不存在时全部使用默认值,方便快速测试
        logging.getLogger(__name__).warning(
            "未找到 %s,所有参数使用默认值", path
        )
        return {}
    except (yaml.YAMLError, OSError) as e:
        logging.getLogger(__name__).error("配置文件加载失败: %s", e)
        raise SystemExit(1)

_cfg = _load_config()

# 用户可配置项（config.yaml 中可覆盖，括号内为默认值）
# 目标
NORAD_IDS: list[int] = _cfg.get("targets", {}).get("norad_ids", [25544])
SCHEDULED_MINUTE: int = _cfg.get("schedule", {}).get("minute", 12)  # 每小时请求的分钟数（建议 12 或 48，避开整点/半点高峰）
# 文件路径
DATA_FILE: str = _cfg.get("files", {}).get("data_file", "tle_data.jsonl")    # 轨道数据文件（带轮转保护）
CACHE_FILE: str = _cfg.get("files", {}).get("cache",    "tle_cache.json")   # 临时缓存，自动覆盖
LOG_FILE: str = _cfg.get("files", {}).get("run_log",  "tle_log.jsonl")  # 运行日志（带轮转保护）
# 预警阈值
REENTRY_WARNING_KM: int  = _cfg.get("alerts", {}).get("reentry_warning_km",   200)  # 近地点低于此值时发出再入预警
ONLY_PRINT_ON_UPDATE: bool = _cfg.get("alerts", {}).get("only_print_on_update", True)  # 仅在 TLE 变化时打印输出
# 重试和速率限制配置
LOGIN_MAX_FAILURES: int = _cfg.get("retry", {}).get("login_max_failures",  5)  # 登录最大失败次数
LOGIN_PAUSE_SECONDS: int = _cfg.get("retry", {}).get("login_pause_seconds", 1800)  # 登录失败后等待时间（秒）
REQUEST_MAX_RETRIES: int = _cfg.get("retry", {}).get("request_max_retries", 3)  # 请求最大重试次数
REQUEST_RETRY_BASE: int = _cfg.get("retry", {}).get("request_retry_base",  5)  # 指数退避基数（秒）：5, 10, 20 ...
# xpropagator 残差分析配置
_xprop_cfg = _cfg.get("xpropagator", {})
XPROP_ENABLED: bool = _xprop_cfg.get("enabled", True)
XPROP_HOST: str = _xprop_cfg.get("host", "localhost")
XPROP_PORT: int = _xprop_cfg.get("port", 50051)
XPROP_MANEUVER_THRESHOLD_KM: float = _xprop_cfg.get("maneuver_threshold_km", 5.0)
# 降级策略配置（当 xpropagator 不可用时使用）
FALLBACK_MANEUVER_THRESHOLD_KM: float = _cfg.get("alerts", {}).get("fallback_maneuver_threshold_km", 5.0)
# 双源配置
_ds_cfg = _cfg.get("data_source", {})
PRIMARY_SOURCE: str    = _ds_cfg.get("primary",                   "spacetrack")
FALLBACK_SOURCE: str   = _ds_cfg.get("fallback",                  "none")
FALLBACK_THRESHOLD: int = _ds_cfg.get("fallback_threshold",        3)
CELESTRAK_INTERVAL: int = _ds_cfg.get("celestrak_interval_seconds", 7200)
USE_SUPPLEMENTAL: bool  = _ds_cfg.get("use_supplemental",          False)

# 以下参数涉及 API 合规，不暴露在 config.yaml 中，避免用户误改导致封号
MIN_REQUEST_INTERVAL: int = 3600   # 两次请求最小间隔（秒），勿修改
SESSION_MAX_AGE: int = 5400   # 会话最长有效期（秒），勿修改

# 日志文件最大大小（字节），超过后自动轮转（10 MB）
MAX_LOG_SIZE: int = _cfg.get("files", {}).get(
    "max_log_size_mb", 10
) * 1024 * 1024

# 安全的回退时间值（用于排序）
_EPOCH_MIN = datetime(2000, 1, 1, tzinfo=timezone.utc)

# Space-Track API 地址
BASE_URL = "https://www.space-track.org"
LOGIN_URL = f"{BASE_URL}/ajaxauth/login"
LOGOUT_URL = f"{BASE_URL}/ajaxauth/logout"

# User-Agent（可选，用于标识应用身份）
# 如果用户在 config.yaml 中配置了 user_agent，则使用该值；否则不设置 UA
SPACE_TRACK_USER_AGENT: Optional[str] = _cfg.get("user_agent") or None

# 批量查询 URL：获取最近 1 小时内发布的所有 TLE
# 这是 Space-Track 官方推荐的查询方式，符合 API 使用规范
#   decay_date/null-val          - 排除已衰减的卫星
#   CREATION_DATE/%3Enow-0.042   - 最近 1 小时发布的 TLE（0.042天 ≈ 1小时）
#   format/json                  - JSON 格式输出
BULK_TLE_URL = (
    f"{BASE_URL}/basicspacedata/query/class/gp"
    "/decay_date/null-val"
    "/CREATION_DATE/%3Enow-0.042"
    "/format/json"
)

# 确保 xpropagator_client 模块的 logger 也能输出 INFO 级别日志
logging.getLogger("xpropagator_client").setLevel(logging.INFO)

log = logging.getLogger(__name__)

# xpropagator 客户端（插件式，找不到模块时自动禁用）
try:
    from xpropagator_client import classify_change_xprop, is_service_alive
    _XPROP_MODULE_OK = True
except ImportError:
    _XPROP_MODULE_OK = False
    classify_change_xprop = None   # type: ignore 忽略
    is_service_alive = None  # type: ignore 忽略

# 实际是否可用 = 配置开启 + 模块导入成功
XPROP_ACTIVE: bool = XPROP_ENABLED and _XPROP_MODULE_OK


def rotate_file_if_needed(filepath: str, max_size: int = MAX_LOG_SIZE) -> None:
    """如果文件超过 max_size，将其重命名为 .bak 实现轮转"""
    try:
        if os.path.exists(filepath) and os.path.getsize(filepath) > max_size:
            backup = filepath + ".bak"
            # 如果备份已存在，先删除旧备份
            if os.path.exists(backup):
                os.remove(backup)
            os.rename(filepath, backup)
            log.info("日志文件 %s 已轮转（>%d MB）", filepath, max_size // (1024 * 1024))
    except OSError as e:
        log.error("日志轮转失败: %s", e)


def parse_datetime_utc(value: object) -> Optional[datetime]:
    """将 Space-Track 返回的 ISO 时间字符串转换为 UTC datetime 对象"""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # 如果没有时区信息，假设为 UTC
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# 本地缓存管理

class LocalCache:
    """持久化缓存，保存上次请求时间和全量原始 TLE 数据"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict = {"last_fetch_ts": None, "raw_records": [], "pending": False}
        if path:
            self._load()

    def _load(self) -> None:
        """从 JSON 文件加载缓存数据"""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("缓存格式错误")
            self._data["last_fetch_ts"] = raw.get("last_fetch_ts", None)
            raw_records = raw.get("raw_records", [])
            if not isinstance(raw_records, list):
                log.warning("缓存 raw_records 字段类型异常，已重置")
                raw_records = []
            self._data["raw_records"] = raw_records
            # 加载待处理标记（用于断点恢复）
            self._data["pending"] = raw.get("pending", False)
            log.info("已加载本地缓存：%s", self._path)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            log.warning("缓存加载失败（将从头开始）: %s", e)

    def _save(self) -> None:
        """将缓存数据保存到 JSON 文件（覆盖模式）"""
        if not self._path:
            return
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log.error("缓存写入失败: %s", e)

    @property
    def last_fetch_ts(self) -> Optional[datetime]:
        """获取上次请求的时间戳"""
        ts = parse_datetime_utc(self._data.get("last_fetch_ts"))
        if ts is None:
            raw = self._data.get("last_fetch_ts")
            if raw:
                log.warning("缓存时间戳格式异常，已忽略: %s", raw)
        return ts

    def seconds_since_last_fetch(self) -> float:
        """计算距离上次请求的秒数"""
        ts = self.last_fetch_ts
        if ts is None:
            return float("inf")  # 从未请求过，返回无穷大
        now = datetime.now(timezone.utc)
        return (now - ts).total_seconds()

    def mark_fetched(self) -> None:
        """更新请求时间戳（请求成功时使用）"""
        self._data["last_fetch_ts"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def save_raw_records(self, records: list[dict]) -> None:
        """保存全量原始记录（覆盖旧数据），并标记为待处理"""
        self._data["last_fetch_ts"] = datetime.now(timezone.utc).isoformat()
        self._data["raw_records"] = records
        self._data["pending"] = True  # 标记有未处理的数据
        self._save()

    def clear_pending(self) -> None:
        """清除待处理标记（数据处理完成后调用）"""
        self._data["pending"] = False
        self._save()

    @property
    def has_pending_data(self) -> bool:
        """检查是否有待处理的全量数据（用于断点恢复）"""
        return self._data.get("pending", False)

    def get_raw_records(self) -> list[dict]:
        """获取缓存中的全量原始记录"""
        return self._data.get("raw_records", [])


# 调度器

def next_scheduled_time(minute: int = SCHEDULED_MINUTE) -> datetime:
    """计算下一个调度时刻（每小时的 :MM 分）"""
    now = datetime.now(timezone.utc)
    target = now.replace(minute=minute, second=0, microsecond=0)
    # 如果当前时间已超过目标时间，则推到下一小时
    if target <= now:
        target += timedelta(hours=1)
    return target


def wait_until(target: datetime) -> None:
    """阻塞等待到指定时刻（每分钟唤醒一次，便于响应 Ctrl-C）"""
    # 只在首次打印等待信息
    first_log = True
    while True:
        secs = (target - datetime.now(timezone.utc)).total_seconds()
        if secs <= 0:
            return
        # 首次或剩余时间少于 10 分钟时打印日志
        if first_log or secs < 600:
            log.info(
                "下次查询：%s UTC（%.0f 分钟后）",
                target.strftime("%H:%M"),
                secs / 60,
            )
            first_log = False
        time.sleep(min(secs, 60))


def compute_next_wake(cache: LocalCache, minute: int = SCHEDULED_MINUTE) -> datetime:
    """
    计算下次唤醒时间，同时满足两个约束：
    1. 下一个调度时刻（每小时的 :MM 分）
    2. 距上次请求满 MIN_REQUEST_INTERVAL（3600秒）
    取两者中较晚的时刻
    """
    sched = next_scheduled_time(minute)

    # 检查速率限制
    secs_since = cache.seconds_since_last_fetch()
    if secs_since < MIN_REQUEST_INTERVAL:
        rate_ok_at = datetime.now(timezone.utc) + timedelta(
            seconds=MIN_REQUEST_INTERVAL - secs_since
        )
        # 如果速率限制时刻明显晚于调度时刻（超过1分钟），才需要推迟到下一个小时
        # 使用1分钟的容差，避免因为时间精度问题导致不必要的推迟
        if (rate_ok_at - sched).total_seconds() > 60:
            while sched <= rate_ok_at:
                sched += timedelta(hours=1)

    return sched


# Space-Track 会话管理

class FetchStatus(Enum):
    """请求状态枚举"""
    RELOGIN = auto()  # 401 错误，需要重新登录
    SKIP = auto()     # 临时错误，本轮跳过


class SpaceTrackSession:
    """封装 Space-Track 登录、重试和会话管理逻辑"""

    def __init__(self) -> None:
        self._session = requests.Session()
        if SPACE_TRACK_USER_AGENT:
            self._session.headers.update({"User-Agent": SPACE_TRACK_USER_AGENT})
        self._login_failures = 0
        self._logged_in_at: Optional[float] = None

    def _check_login_response(self, resp: requests.Response) -> bool:
        if resp.status_code != 200:
            return False
        if "chocolatechip" not in self._session.cookies:
            return False
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("Login") == "Failed":
                return False
        except ValueError:
            pass
        return True

    def login_once(self) -> bool:
        """尝试登录一次，成功返回 True"""
        try:
            resp = self._session.post(
                LOGIN_URL,
                data={"identity": USERNAME, "password": PASSWORD},
                timeout=15,
            )
        except requests.RequestException as e:
            log.error("登录网络错误: %s", e)
            return False

        if self._check_login_response(resp):
            log.debug("登录成功")
            self._login_failures = 0
            self._logged_in_at = time.monotonic()
            return True

        log.error("登录失败 (HTTP %d)", resp.status_code)
        try:
            log.error("响应: %s", resp.json())
        except ValueError:
            log.error("响应: %s", resp.text[:200])
        return False

    def login_with_retry(self) -> bool:
        """带重试的登录，最多尝试 LOGIN_MAX_FAILURES 次"""
        for attempt in range(1, LOGIN_MAX_FAILURES + 1):
            if self.login_once():
                return True
            self._login_failures += 1
            if attempt < LOGIN_MAX_FAILURES:
                wait = REQUEST_RETRY_BASE * (2 ** (attempt - 1))
                log.warning(
                    "登录失败（第 %d/%d 次），%d 秒后重试",
                    attempt, LOGIN_MAX_FAILURES, wait,
                )
                time.sleep(wait)
            else:
                log.error(
                    "连续登录失败 %d 次，放弃本轮（建议等待 %d 分钟后再试）",
                    LOGIN_MAX_FAILURES,
                    LOGIN_PAUSE_SECONDS // 60,
                )
        return False

    def ensure_fresh_session(self) -> bool:
        """确保会话有效，如果超过 SESSION_MAX_AGE 则重新登录"""
        if self._logged_in_at is None:
            return self.login_with_retry()
        age = time.monotonic() - self._logged_in_at
        if age > SESSION_MAX_AGE:
            log.info("会话已存在 %.0f 分钟，主动刷新登录...", age / 60)
            self.logout()
            self._session = requests.Session()
            if SPACE_TRACK_USER_AGENT:
                self._session.headers.update({"User-Agent": SPACE_TRACK_USER_AGENT})
            return self.login_with_retry()
        return True

    def logout(self) -> None:
        try:
            self._session.get(LOGOUT_URL, timeout=10)
        except Exception:
            pass
        self._session.cookies.clear()
        self._logged_in_at = None

    def relogin(self) -> bool:
        self.logout()
        self._session = requests.Session()
        if SPACE_TRACK_USER_AGENT:
            self._session.headers.update({"User-Agent": SPACE_TRACK_USER_AGENT})
        return self.login_with_retry()

    def get(self, url: str) -> "requests.Response | FetchStatus":
        """发送 GET 请求，带重试和错误处理"""
        for attempt in range(1, REQUEST_MAX_RETRIES + 1):
            try:
                resp = self._session.get(url, timeout=30)
                if resp.status_code == 401:
                    return FetchStatus.RELOGIN
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                if resp.status_code != 200:
                    log.warning("非预期状态码 %d", resp.status_code)
                    return FetchStatus.SKIP
                return resp
            except requests.RequestException as e:
                wait = REQUEST_RETRY_BASE * (2 ** (attempt - 1))
                if attempt < REQUEST_MAX_RETRIES:
                    log.warning(
                        "请求错误（第 %d/%d 次）: %s，%d 秒后重试",
                        attempt, REQUEST_MAX_RETRIES, e, wait,
                    )
                    time.sleep(wait)
                else:
                    log.error("请求失败，已重试 %d 次，本轮跳过: %s", REQUEST_MAX_RETRIES, e)
                    return FetchStatus.SKIP
        return FetchStatus.SKIP

    def __enter__(self) -> "SpaceTrackSession":
        return self

    def __exit__(self, *_) -> None:
        self.logout()
        self._session.close()


# 批量拉取和本地筛选

def fetch_bulk_tle(st: SpaceTrackSession) -> "list[dict] | FetchStatus":
    """批量拉取最近 1 小时内发布的所有 TLE（消耗 1 次 gp 配额）"""
    log.info("请求批量 TLE（最近 1 小时发布）...")
    result = st.get(BULK_TLE_URL)
    if isinstance(result, FetchStatus):
        return result
    try:
        data = result.json()
    except ValueError as e:
        log.warning("JSON 解析失败: %s", e)
        return FetchStatus.SKIP
    log.debug("收到 %d 条记录", len(data))
    return data


def fetch_bulk_with_relogin(st: SpaceTrackSession) -> Optional[list[dict]]:
    """
    带重登录保护的批量拉取
    如果遇到 401 错误，重新登录后不会立即重试 gp 请求
    而是返回 None，由主循环在下一个调度周期再试
    """
    result = fetch_bulk_tle(st)
    if result is FetchStatus.RELOGIN:
        # 会话过期，重新登录后不立即重试（避免同一小时内第 2 次 gp 请求）
        log.info("会话过期，重新登录...")
        st.relogin()
        return None
    if isinstance(result, FetchStatus):
        return None
    return result


def _record_sort_key(rec: dict) -> tuple[datetime, int]:
    """记录排序键：优先按 CREATION_DATE，其次按 FILE 号"""
    creation = parse_datetime_utc(rec.get("CREATION_DATE")) or _EPOCH_MIN
    try:
        file_no = int(rec.get("FILE") or 0)
    except (ValueError, TypeError):
        file_no = 0
    return (creation, file_no)


def filter_by_norad(records: list[dict], norad_ids: list[int]) -> dict[int, dict]:
    """
    筛选目标 NORAD ID，每个卫星只返回最新一条（CREATION_DATE 最大）。
    同一小时内多条记录属于“解算修正覆盖”，不是轨道演化序列。
    返回结构：{norad_id: latest_record_with_batch_count}
    """
    # 将目标列表转为集合，提高查找效率
    target_set = set(norad_ids)
    
    # 按 NORAD ID 分组
    grouped: dict[int, list[dict]] = {}
    for rec in records:
        try:
            nid = int(rec.get("NORAD_CAT_ID") or 0)
        except (ValueError, TypeError):
            continue  # 跳过无效记录
        if nid in target_set:
            # 添加到对应卫星的记录列表
            grouped.setdefault(nid, []).append(rec)

    # 对每个卫星，只保留最新的一条记录
    found: dict[int, dict] = {}
    for nid, recs in grouped.items():
        # 按时间排序（从旧到新）
        sorted_recs = sorted(recs, key=_record_sort_key)
        latest = sorted_recs[-1]  # 取最后一条（最新）
        
        # 注入本批次记录数量，供 process_records 打日志用
        latest["_batch_count"] = len(sorted_recs)
        found[nid] = latest
    
    return found


# 轨道数据处理
def classify_change(orbit: dict, prev: Optional[dict]) -> str:
    """
    判断 TLE 变化是真实机动还是解算修正。
    优先策略（当 xpropagator 已启用且在线）：
      残差分析，将旧 TLE 传播到新历元，对比 ECI 位置差
      Δr ≥ XPROP_MANEUVER_THRESHOLD_KM km → maneuver
      Δr <  XPROP_MANEUVER_THRESHOLD_KM km → correction

      降级策略（xpropagator 不可用时）：
      近地点/远地点变化 > 5 km → maneuver，否则 → correction
    """
    if prev is None:
        return "initial"

    # 高精度路径：xpropagator 残差分析
    if XPROP_ACTIVE:
        result = classify_change_xprop(orbit, prev,
            maneuver_threshold_km=XPROP_MANEUVER_THRESHOLD_KM,
            host=XPROP_HOST, port=XPROP_PORT,)
        if result is not None:
            return result
        # RPC 调用失败（服务暂时不可用），降级处理
        log.debug("[%d] xpropagator 本次调用失败，降级到简单阈值", orbit["norad"])

    # 降级路径：简单近地点/远地点阈值
    delta_peri = abs(orbit["periapsis"] - prev["periapsis"])
    delta_apo = abs(orbit["apoapsis"] - prev["apoapsis"])
    if delta_peri > FALLBACK_MANEUVER_THRESHOLD_KM or delta_apo > FALLBACK_MANEUVER_THRESHOLD_KM:
        return "maneuver"
    return "correction"


def format_change_type(change_type: str) -> str:
    # 将变化类型转换为中英文对照格式
    type_map = {
        "initial": "首次记录 (Initial)",
        "correction": "解算修正 (Correction)",
        "maneuver": "真实机动 (Maneuver)",
    }
    return type_map.get(change_type, f"未知 ({change_type})")


def parse_orbit(record: dict) -> dict:
    # 从提取轨道参数并计算哈希值
    name = (record.get("OBJECT_NAME") or "").strip()
    tle1 = str(record.get("TLE_LINE1") or "")
    tle2 = str(record.get("TLE_LINE2") or "")
    # 使用 TLE 两行数据的 SHA256 哈希作为唯一标识
    tle_hash = hashlib.sha256((tle1 + tle2).encode("utf-8")).hexdigest()[:16]
    return {
        "norad": int(record.get("NORAD_CAT_ID") or 0),
        "name": name or "TBA",
        "intl_id": record.get("OBJECT_ID", ""),
        "epoch": record.get("EPOCH", ""),
        "periapsis": float(record.get("PERIAPSIS") or 0),
        "apoapsis": float(record.get("APOAPSIS") or 0),
        "incl": float(record.get("INCLINATION") or 0),
        "period": float(record.get("PERIOD") or 0),
        "ecc": float(record.get("ECCENTRICITY") or 0),
        "bstar": float(record.get("BSTAR") or 0),
        "tle1": tle1,
        "tle2": tle2,
        "tle_hash": tle_hash,
        # TODO (5位编号耗尽预案, ~2026-07-20):
        # 届时 TLE_LINE1/TLE_LINE2 将不再提供。
        # _raw_elements 保存原始根数，供 xpropagator_client.gp_json_to_tle_lines()
        # 在 tle1/tle2 为空时重建 TLE 两行以维持残差分析能力。
        "_raw_elements": {
            "NORAD_CAT_ID": record.get("NORAD_CAT_ID"),
            "OBJECT_ID": record.get("OBJECT_ID"),
            "OBJECT_NAME": record.get("OBJECT_NAME"),
            "EPOCH": record.get("EPOCH"),
            "CLASSIFICATION_TYPE": record.get("CLASSIFICATION_TYPE"),
            "ELEMENT_SET_NO": record.get("ELEMENT_SET_NO"),
            "EPHEMERIS_TYPE": record.get("EPHEMERIS_TYPE"),
            "INCLINATION": record.get("INCLINATION"),
            "RA_OF_ASC_NODE": record.get("RA_OF_ASC_NODE"),
            "ECCENTRICITY": record.get("ECCENTRICITY"),
            "ARG_OF_PERICENTER": record.get("ARG_OF_PERICENTER"),
            "MEAN_ANOMALY": record.get("MEAN_ANOMALY"),
            "MEAN_MOTION": record.get("MEAN_MOTION"),
            "MEAN_MOTION_DOT": record.get("MEAN_MOTION_DOT"),
            "MEAN_MOTION_DDOT": record.get("MEAN_MOTION_DDOT"),
            "BSTAR": record.get("BSTAR"),
            "REV_AT_EPOCH": record.get("REV_AT_EPOCH"),
        },
    }


def estimate_reentry_days(orbit: dict) -> Optional[float]:
    """
    基于 BSTAR 和简化大气模型估算剩余再入天数（仅供参考）
    原理：通过大气阻力引起的平均运动变化率推算轨道衰减速度
    """
    peri, bstar, period = orbit["periapsis"], orbit["bstar"], orbit["period"]
    # 近地点过高或 BSTAR 无效时无法估算
    if peri > 400.0 or bstar <= 0.0 or period <= 0:
        return None
    # 简化的大气密度模型
    rho_area = 2e-10 * math.exp(-(peri - 200.0) / 60.0) * 60000.0
    rho0 = 2.461e-5
    n = 1440.0 / period  # 平均运动（圈/天）
    # 平均运动变化率
    dn_dt = 3.0 * math.pi * (n ** 2) * bstar * (rho_area / rho0)
    if dn_dt <= 1e-12:
        return None
    # 假设再入时平均运动为 16 圈/天
    n_reentry = 16.0
    if n <= n_reentry:
        return 0.0
    return (n - n_reentry) / dn_dt


def format_reentry_estimate(days: float) -> str:
    if days == 0.0:
        return "即将再入"
    if days < 1.0:
        return f"约 {days * 24:.0f} 小时内（粗估）"
    if days < 30.0:
        return f"约 {days:.1f} 天内（粗估）"
    return f"约 {days:.0f} 天（粗估，误差较大）"


def print_orbit(orbit: dict, prev: Optional[dict]) -> None:
    """格式化打印轨道信息"""
    peri, apo = orbit["periapsis"], orbit["apoapsis"]
    delta = ""
    if prev:
        delta = f"  （近地点 {peri - prev['periapsis']:+.1f} km，远地点 {apo - prev['apoapsis']:+.1f} km）"
    log.info(f"""
  ===============================================
    {orbit['name']:<20} NORAD {orbit['norad']}
    国际编号: {orbit['intl_id']}
    历元:     {orbit['epoch']}
    近地点:   {peri:.1f} km    远地点: {apo:.1f} km
    倾角:     {orbit['incl']:.4f}°   周期: {orbit['period']:.3f} min
    离心率:   {orbit['ecc']:.7f}   BSTAR: {orbit['bstar']:.4e}
    TLE Hash: {orbit['tle_hash']}
  ==============================================={delta}
  {orbit['tle1']}
  {orbit['tle2']}""")
    # 再入预警
    if REENTRY_WARNING_KM > 0 and peri < REENTRY_WARNING_KM:
        days = estimate_reentry_days(orbit)
        if days is not None:
            log.info(f"   再入高风险：近地点 {peri:.1f} km，预计 {format_reentry_estimate(days)}，实际误差可达数倍")
        else:
            log.info(f"   再入高风险：近地点 {peri:.1f} km")
            if orbit["bstar"] <= 0:
                log.info("     BSTAR=0，寿命无法估算（可能为初始定轨解，阻力项尚未计算）")
            else:
                log.info("     近地点 > 400 km 或周期无效，不满足估算条件")
    elif peri < 300:
        log.info(f"     注意：近地点 {peri:.1f} km，大气阻力明显，轨道将持续衰减")


def log_record(orbit: dict, change_type: str = "unknown", source: str = "spacetrack") -> None:
    """将轨道数据写入 DATA_FILE（核心业务数据）"""
    if not DATA_FILE:
        return
    rotate_file_if_needed(DATA_FILE)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "change_type": change_type,  # 变化类型：initial/correction/maneuver
        "source": source,            # 数据来源：spacetrack / celestrak / celestrak_sup
        **orbit
    }
    try:
        with open(DATA_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.error("轨道数据写入失败: %s", e)


def write_log_message(message: str) -> None:
    """将运行日志写入 LOG_FILE"""
    if not LOG_FILE:
        return
    rotate_file_if_needed(LOG_FILE)
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "message": message}
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.error("运行日志写入失败: %s", e)


# 状态恢复

def _iter_jsonl_reversed(path: str, chunk: int = 65536):
    """
    从文件末尾逐块反向读取 JSONL
    """
    with open(path, "rb") as f:
        f.seek(0, 2)
        remaining = f.tell()
        
        if remaining == 0:
            return
        
        buf = b""
        while remaining > 0:
            read_size = min(chunk, remaining)
            remaining -= read_size
            f.seek(remaining)
            buf = f.read(read_size) + buf
            lines = buf.split(b"\n")
            # 最左边的块可能是不完整行，留到下次拼接
            buf = lines[0]
            for line in reversed(lines[1:]):
                line = line.strip()
                if line:
                    yield line.decode("utf-8", errors="replace")
        # 处理文件开头剩余内容
        if buf.strip():
            yield buf.strip().decode("utf-8", errors="replace")


def restore_from_log(norad_ids: list[int]) -> dict[int, dict]:
    """从 DATA_FILE 末尾反向扫描，恢复每个目标的最新轨道状态。"""
    prev_data: dict[int, dict] = {}
    if not DATA_FILE:
        return prev_data
    
    target_set = set(norad_ids)
    seen: set[int] = set()
    
    try:
        for line in _iter_jsonl_reversed(DATA_FILE):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            
            norad = entry.get("norad")
            if norad not in target_set or norad in seen:
                continue
            
            prev_data[norad] = entry
            seen.add(norad)
            
            # 找到所有目标，立即退出
            if len(seen) == len(target_set):
                break
        
        if prev_data:
            log.info("已从轨道数据文件恢复 %d 个目标的历史状态", len(prev_data))
    
    except OSError as e:
        log.warning("轨道数据文件读取失败: %s", e)
    
    return prev_data


# 数据处理

def process_records(
    raw_records: dict[int, dict],
    prev_data: dict[int, dict],
    last_hash: dict[int, str],
    cache: LocalCache,
) -> None:
    """
    比对 TLE 哈希值，检测变化并记录日志
    每个卫星只处理最新一条记录（解算修正后的最终版）
    无论是否命中目标都更新缓存时间戳，防止速率保护卡死
    """
    # 遍历所有监控目标
    for norad_id in NORAD_IDS:
        # 获取该卫星的最新记录（已包含 _batch_count）
        record = raw_records.get(norad_id)
        if not record:
            # 本批次中没有该卫星的新 TLE
            log.info("[%d] 本批次无数据（过去 1 小时内未发布新 TLE）", norad_id)
            continue
        
        # 提取来源标识（确认 record 不为 None 后读取）
        record_source = record.get("_source", "spacetrack")

        # 提取批次记录数（元数据，不进入 orbit）
        batch_count = record.pop("_batch_count", 1)
        
        # 解析轨道参数并计算 Hash
        orbit = parse_orbit(record)
        prev = prev_data.get(norad_id)
        cur_hash = orbit["tle_hash"]

        # 如果本批次有多条记录，记录日志
        if batch_count > 1:
            log.info("[%d] 本批次共 %d 条解算记录，取最新一条", norad_id, batch_count)

        # 检测 TLE 是否变化（与最新 Hash 比较）
        if cur_hash != last_hash.get(norad_id):
            # 分类变化类型（解算修正 vs 真实机动）
            change_type = classify_change(orbit, prev)
            change_type_cn = format_change_type(change_type)  # 中英文对照
            
            msg = f"[{norad_id}] 检测到 TLE 变化！(hash: {last_hash.get(norad_id, '无')} → {cur_hash}, 类型: {change_type_cn})"
            log.info(msg)
            write_log_message(msg)
            
            # 打印轨道信息（显示与上一次的差异）
            print_orbit(orbit, prev)
            
            # 写入轨道数据文件（附带变化类型）
            log_record(orbit, change_type, source=record_source)
            
            # 更新内存中的状态（供下次比较使用）
            prev_data[norad_id] = orbit  # 更新最新轨道数据
            last_hash[norad_id] = cur_hash  # 更新最新 Hash
        elif not ONLY_PRINT_ON_UPDATE:
            # Hash 相同，但配置为打印所有数据
            print_orbit(orbit, None)  # TLE 未变化，不显示 delta
        else:
            # Hash 相同，且配置为仅打印变化，只记录日志
            log.info("[%d] %s：TLE 未变化（hash %s）", norad_id, orbit['name'], cur_hash)

    # 更新缓存时间戳（全量数据已在 save_raw_records 中保存）
    # 即使没有命中目标，也要更新时间戳，避免下次启动时重复请求
    cache.mark_fetched()

def cold_start_if_needed(norad_ids: list[int], prev_data: dict[int, dict]) -> None:
    """
    冷启动检查：对 tle_data 中无记录的卫星，通过 CelesTrak 获取初始基准数据
    无论 PRIMARY_SOURCE 为何值均执行，因为冷启动始终优先走 CelesTrak（无需认证）
    """
    missing = [nid for nid in norad_ids if nid not in prev_data]
    if not missing:
        return

    if not _CT_MODULE_OK:
        log.warning("冷启动：以下卫星无历史记录，但 celestrak_fetcher 模块未找到，跳过初始化: %s",
                    missing)
        return

    log.info("冷启动：以下卫星无历史记录，将通过 CelesTrak 获取初始基准: %s", missing)
    for nid in missing:
        record = ct.fetch_single(nid, use_supplemental=USE_SUPPLEMENTAL)
        if record is None:
            log.warning("[冷启动][%d] CelesTrak 查询失败，本轮跳过", nid)
            continue
        orbit = parse_orbit(record)
        log.info("[冷启动][%d] %s 初始基准已入库", nid, orbit["name"])
        log_record(orbit, change_type="initial", source=record.get("_source", "celestrak"))
        prev_data[nid] = orbit
    log.info("冷启动完成")


def run_celestrak_cycle(
    prev_data: dict[int, dict],
    last_hash: dict[int, str],
    consecutive_failures: dict,
) -> bool:
    """
    以 CelesTrak 为主源执行一轮监控。
    返回 True 表示本轮至少有一次成功的网络请求，False 表示全部失败或全部跳过。
    consecutive_failures 为可变 dict，内含 "count" 字段，由调用方维护。
    """
    any_success = False  # 是否有至少一次请求成功
    
    for nid in NORAD_IDS:
        secs = ct.seconds_since_last_query(nid)
        if secs < CELESTRAK_INTERVAL:
            log.debug("[CelesTrak][%d] 距上次查询 %.0f 分钟，跳过本轮", nid, secs / 60)
            continue  # 跳过不算成功也不算失败

        record = ct.fetch_single(nid, use_supplemental=USE_SUPPLEMENTAL)
        if record is None:
            log.warning("[CelesTrak][%d] 查询失败", nid)
            continue

        any_success = True
        orbit = parse_orbit(record)
        cur_hash = orbit["tle_hash"]
        record_source = record.get("_source", "celestrak")

        prev = prev_data.get(nid)
        if cur_hash != last_hash.get(nid):
            change_type = classify_change(orbit, prev)
            change_type_cn = format_change_type(change_type)
            msg = (f"[{nid}] 检测到 TLE 变化！"
                   f"(hash: {last_hash.get(nid, '无')} → {cur_hash}, "
                   f"类型: {change_type_cn}, 来源: {record_source})")
            log.info(msg)
            write_log_message(msg)
            print_orbit(orbit, prev)
            log_record(orbit, change_type, source=record_source)
            prev_data[nid] = orbit
            last_hash[nid] = cur_hash
        elif not ONLY_PRINT_ON_UPDATE:
            print_orbit(orbit, None)
        else:
            log.info("[%d] %s：TLE 未变化（hash %s，来源: %s）",
                     nid, orbit["name"], cur_hash, record_source)

    return any_success

# 主程序

def main() -> None:
    """主函数：启动 TLE 监控循环（支持双源协同）"""

    # 仅在 Space-Track 为主源或备源时才强制要求凭据
    _st_required = (PRIMARY_SOURCE == "spacetrack" or FALLBACK_SOURCE == "spacetrack")
    if _st_required and (not USERNAME or not PASSWORD):
        log.error("当前配置需要 Space-Track 凭据（主源或备源为 spacetrack），但 .env 中未找到！")
        log.error("请设置 SPACETRACK_USER / SPACETRACK_PASS，或将 data_source.primary 改为 celestrak")
        raise SystemExit(1)

    if PRIMARY_SOURCE == "celestrak" and not _CT_MODULE_OK:
        log.error("data_source.primary=celestrak，但 celestrak_fetcher.py 未找到，请确认文件存在")
        raise SystemExit(1)

    log.info("TEL-Tracking 轨道监控  主源: %s  备源: %s", PRIMARY_SOURCE, FALLBACK_SOURCE)
    log.info("目标: %s", ", ".join(str(i) for i in NORAD_IDS))

    if XPROP_ACTIVE:
        alive = is_service_alive(XPROP_HOST, XPROP_PORT)
        if not alive:
            log.warning("xpropagator 配置已启用但服务未响应（%s:%d），将自动降级", XPROP_HOST, XPROP_PORT)
    else:
        if XPROP_ENABLED and not _XPROP_MODULE_OK:
            log.warning("xpropagator 已启用但模块未找到")
        elif not XPROP_ENABLED:
            log.info("xpropagator 已禁用，使用简单阈值")

    log.info(
        "调度: 每小时第 %02d 分 | 再入预警: <%d km | 数据: %s | 日志: %s",
        SCHEDULED_MINUTE, REENTRY_WARNING_KM, DATA_FILE or "关闭", LOG_FILE or "关闭",
    )
    print()

    write_log_message("程序启动")

    # 加载 Space-Track 缓存（仅 spacetrack 模式使用，celestrak 模式中闲置）
    cache = LocalCache(CACHE_FILE)

    # 从 tle_data 恢复历史状态
    prev_data = restore_from_log(NORAD_IDS)
    last_hash: dict[int, str] = {
        nid: orbit.get("tle_hash", "") for nid, orbit in prev_data.items()
    }

    # 冷启动：对无记录的卫星通过 CelesTrak 填充初始基准（与主源无关）
    cold_start_if_needed(NORAD_IDS, prev_data)
    # 冷启动后更新 last_hash
    for nid, orbit in prev_data.items():
        if nid not in last_hash:
            last_hash[nid] = orbit.get("tle_hash", "")

    # Space-Track 断点恢复（仅当缓存中有待处理数据时）
    if PRIMARY_SOURCE == "spacetrack" and cache.has_pending_data:
        log.info("检测到未处理的全量数据，尝试断点恢复...")
        all_records = cache.get_raw_records()
        if all_records:
            raw_records = filter_by_norad(all_records, NORAD_IDS)
            # 注入来源标识
            for rec in raw_records.values():
                rec.setdefault("_source", "spacetrack")
            found_ids = list(raw_records.keys())
            missing_ids = [nid for nid in NORAD_IDS if nid not in raw_records]
            if found_ids:
                log.info("断点恢复：命中 %s", ', '.join(str(i) for i in found_ids))
            if missing_ids:
                log.info("断点恢复：未包含 %s", ', '.join(str(i) for i in missing_ids))
            process_records(raw_records, prev_data, last_hash, cache)
            cache.clear_pending()
            log.info("断点恢复完成")

    # 打印当前轨道状态
    for nid in NORAD_IDS:
        orbit = prev_data.get(nid)
        if orbit:
            print_orbit(orbit, None)

    # 主循环
    consecutive_failures = {"count": 0}  # 主源连续失败计数
    active_source = PRIMARY_SOURCE       # 当前实际使用的数据源

    if PRIMARY_SOURCE == "spacetrack":
        # Space-Track 主源路径（保持原有逻辑完整）
        with SpaceTrackSession() as st:
            first_run = True
            while True:
                if first_run:
                    first_run = False
                    secs_since = cache.seconds_since_last_fetch()
                    if secs_since == float("inf"):
                        log.info("无历史记录，将立即执行首次查询")
                    elif secs_since < MIN_REQUEST_INTERVAL:
                        wait_seconds = MIN_REQUEST_INTERVAL - secs_since
                        log.warning("距上次请求 %.0f 分钟，需等待 %.0f 分钟", secs_since / 60, wait_seconds / 60)
                        time.sleep(wait_seconds)
                else:
                    wake_at = compute_next_wake(cache, SCHEDULED_MINUTE)
                    wait_until(wake_at)

                log.info("[%s] 开始批量拉取（主源: spacetrack）...",
                         datetime.now(timezone.utc).strftime("%H:%M:%S"))

                # 主源请求
                success = False
                if active_source == "spacetrack":
                    if not st.ensure_fresh_session():
                        log.error("登录失败")
                        consecutive_failures["count"] += 1
                    else:
                        all_records = fetch_bulk_with_relogin(st)
                        if all_records is None:
                            consecutive_failures["count"] += 1
                        else:
                            consecutive_failures["count"] = 0
                            success = True
                            cache.save_raw_records(all_records)
                            raw_records = filter_by_norad(all_records, NORAD_IDS)
                            # 注入来源标识
                            for rec in raw_records.values():
                                rec.setdefault("_source", "spacetrack")
                            found_ids = list(raw_records.keys())
                            missing_ids = [nid for nid in NORAD_IDS if nid not in raw_records]
                            if found_ids:
                                log.info("筛选命中：%s", ', '.join(str(i) for i in found_ids))
                            if missing_ids:
                                log.info("本批次未包含：%s", ', '.join(str(i) for i in missing_ids))
                            process_records(raw_records, prev_data, last_hash, cache)
                            cache.clear_pending()

                elif active_source == "celestrak" and _CT_MODULE_OK:
                    # 备源模式（Space-Track 故障期间）
                    ok = run_celestrak_cycle(prev_data, last_hash, consecutive_failures)
                    if ok:
                        consecutive_failures["count"] = 0
                        # 尝试恢复主源
                        if st.ensure_fresh_session():
                            log.info("主源 Space-Track 已恢复，切回主源")
                            active_source = "spacetrack"
                    else:
                        consecutive_failures["count"] += 1

                # 备源切换判断
                if (consecutive_failures["count"] >= FALLBACK_THRESHOLD
                        and active_source != FALLBACK_SOURCE
                        and FALLBACK_SOURCE != "none"):
                    log.warning(
                        "主源 %s 连续失败 %d 次，切换到备源 %s",
                        PRIMARY_SOURCE, consecutive_failures["count"], FALLBACK_SOURCE,
                    )
                    write_log_message(f"备源切换：{PRIMARY_SOURCE} → {FALLBACK_SOURCE}（连续失败 {consecutive_failures['count']} 次）")
                    active_source = FALLBACK_SOURCE

    else:
        # CelesTrak 主源路径
        log.info("以 CelesTrak 为主源启动，轮询间隔 %d 分钟", CELESTRAK_INTERVAL // 60)
        while True:
            log.info("[%s] 开始 CelesTrak 轮询...",
                     datetime.now(timezone.utc).strftime("%H:%M:%S"))

            ok = run_celestrak_cycle(prev_data, last_hash, consecutive_failures)
            if ok:
                consecutive_failures["count"] = 0
                active_source = "celestrak"
            else:
                consecutive_failures["count"] += 1
                log.warning("CelesTrak 本轮全部失败，连续失败 %d 次", consecutive_failures["count"])

                # 备源切换
                if (consecutive_failures["count"] >= FALLBACK_THRESHOLD
                        and FALLBACK_SOURCE == "spacetrack"
                        and _st_required):
                    if active_source != "spacetrack":
                        log.warning("切换到备源 Space-Track")
                        write_log_message(f"备源切换：celestrak → spacetrack（连续失败 {consecutive_failures['count']} 次）")
                    active_source = "spacetrack"
                    # Space-Track 备源：单次批量拉取
                    with SpaceTrackSession() as st_tmp:
                        if st_tmp.ensure_fresh_session():
                            all_records = fetch_bulk_with_relogin(st_tmp)
                            if all_records:
                                consecutive_failures["count"] = 0
                                active_source = "celestrak"  # 下轮尝试回主源
                                cache.save_raw_records(all_records)
                                raw_records = filter_by_norad(all_records, NORAD_IDS)
                                # 注入来源标识
                                for rec in raw_records.values():
                                    rec.setdefault("_source", "spacetrack")
                                process_records(raw_records, prev_data, last_hash, cache)
                                cache.clear_pending()
                            else:
                                log.error("备源 Space-Track 登录失败或请求失败，请检查凭据是否过期")
                        else:
                            log.error("备源 Space-Track 登录失败，请检查凭据是否过期")

            # 等待下一个轮询周期
            log.info("下次轮询：约 %d 分钟后", CELESTRAK_INTERVAL // 60)
            time.sleep(CELESTRAK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已停止监控")