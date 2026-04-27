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
import sys
import time
import os
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from enum import Enum, auto
from typing import Optional

import requests
import yaml

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


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
        # 如果速率限制时刻晚于调度时刻，需要推迟到下一个小时
        if rate_ok_at > sched:
            mins_since = secs_since / 60
            log.info(
                "速率保护：需等至 %s UTC（距上次请求 %.0f 分钟）",
                rate_ok_at.strftime("%H:%M"),
                mins_since,
            )
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
            log.info("登录成功")
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
    log.info("收到 %d 条记录", len(data))
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
    粗略判断 TLE 变化是解算修正还是真实机动
    判据：近地点/远地点变化幅度
    - 解算误差通常在 2 km 以内
    - 真实机动通常 > 5 km
    """
    if prev is None:
        return "initial"  # 首次记录
    
    delta_peri = abs(orbit["periapsis"] - prev["periapsis"])
    delta_apo = abs(orbit["apoapsis"] - prev["apoapsis"])
    
    # 如果近地点或远地点变化超过 5 km，判定为疑似机动
    if delta_peri > 5.0 or delta_apo > 5.0:
        return "maneuver"  # 疑似真实机动
    
    return "correction"  # 疑似解算修正


def format_change_type(change_type: str) -> str:
    """将变化类型转换为中英文对照格式"""
    type_map = {
        "initial": "首次记录 (Initial)",
        "correction": "解算修正 (Correction)",
        "maneuver": "真实机动 (Maneuver)",
    }
    return type_map.get(change_type, f"未知 ({change_type})")


def parse_orbit(record: dict) -> dict:
    """从 Space-Track 记录中提取轨道参数并计算哈希值"""
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
    print(f"""
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
            print(f"   再入高风险：近地点 {peri:.1f} km，预计 {format_reentry_estimate(days)}，实际误差可达数倍")
        else:
            print(f"   再入高风险：近地点 {peri:.1f} km")
            if orbit["bstar"] <= 0:
                print("     BSTAR=0，寿命无法估算（可能为初始定轨解，阻力项尚未计算）")
            else:
                print("     近地点 > 400 km 或周期无效，不满足估算条件")
    elif peri < 300:
        print(f"     注意：近地点 {peri:.1f} km，大气阻力明显，轨道将持续衰减")


def log_record(orbit: dict, change_type: str = "unknown") -> None:
    """将轨道数据写入 DATA_FILE（核心业务数据）"""
    if not DATA_FILE:
        return
    rotate_file_if_needed(DATA_FILE)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "change_type": change_type,  # 变化类型：initial/correction/maneuver
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

def restore_from_log(norad_ids: list[int]) -> dict[int, dict]:
    """从 DATA_FILE 恢复历史轨道状态"""
    prev_data: dict[int, dict] = {}
    if not DATA_FILE:
        return prev_data
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return prev_data
    seen: set[int] = set()
    # 从后往前读取，获取每个卫星的最新记录
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        norad = entry.get("norad")
        if norad not in norad_ids or norad in seen:
            continue
        prev_data[norad] = entry
        seen.add(norad)
        if len(seen) == len(norad_ids):
            break
    if prev_data:
        log.info("已从轨道数据文件恢复 %d 个目标的历史状态", len(prev_data))
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
            log_record(orbit, change_type)
            
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


# 主程序

def main() -> None:
    """主函数:启动 TLE 监控循环"""
    # 检查凭据（优先检查,避免后续运行时才发现缺失）
    if not USERNAME or not PASSWORD:
        log.error("缺少 Space-Track 凭据!")
        log.error("请在项目根目录创建 .env 文件并设置:")
        log.error("  SPACETRACK_USER=your_email@example.com")
        log.error("  SPACETRACK_PASS=your_password")
        log.error("可参考 .env.example 模板文件")
        raise SystemExit(1)

    log.info("Space-Track 轨道监控")
    log.info("配置文件: config.yaml | 密钥: .env")
    log.info("目标: %s", ", ".join(str(i) for i in NORAD_IDS))
    log.info(
        "调度: 每小时第 %02d 分 | 再入预警: <%d km | 轨道数据: %s | 运行日志: %s | 缓存: %s",
        SCHEDULED_MINUTE, REENTRY_WARNING_KM,
        DATA_FILE or "关闭", LOG_FILE or "关闭", CACHE_FILE or "关闭",
    )
    print()

    # 写入启动日志（确保日志文件被创建）
    write_log_message("程序启动")

    # 加载缓存
    cache = LocalCache(CACHE_FILE)

    # 从轨道数据文件恢复历史轨道状态（必须在断点恢复之前）
    prev_data = restore_from_log(NORAD_IDS)

    # 初始化哈希字典，用于检测 TLE 变化
    last_hash: dict[int, str] = {
        nid: orbit.get("tle_hash", "") for nid, orbit in prev_data.items()
    }

    # 检查是否有待处理的数据（断点恢复）
    if cache.has_pending_data:
        log.info("检测到未处理的全量数据，尝试断点恢复...")
        
        # 从缓存中获取全量原始数据
        all_records = cache.get_raw_records()
        if all_records:
            # 筛选目标卫星
            raw_records = filter_by_norad(all_records, NORAD_IDS)
            found_ids = list(raw_records.keys())
            missing_ids = [nid for nid in NORAD_IDS if nid not in raw_records]
            
            if found_ids:
                log.info("断点恢复：筛选命中 %s", ', '.join(str(i) for i in found_ids))
            if missing_ids:
                log.info("断点恢复：本批次未包含 %s", ', '.join(str(i) for i in missing_ids))
            
            # 处理记录
            process_records(raw_records, prev_data, last_hash, cache)
            
            # 清除待处理标记
            cache.clear_pending()
            log.info("断点恢复完成")
        else:
            log.warning("缓存中有待处理标记，但无实际数据")

    # 打印当前轨道状态
    for norad_id in NORAD_IDS:
        orbit = prev_data.get(norad_id)
        if orbit:
            print_orbit(orbit, None)
    
    # 强制刷新所有输出缓冲区，确保顺序正确
    for handler in logging.root.handlers[:]:
        handler.flush()
    sys.stdout.flush()


    # 主循环
    with SpaceTrackSession() as st:
        first_run = True
        while True:
            # === 确定是否需要等待 ===
            if first_run:
                # 本轮首次迭代：检查距上次请求的时间
                first_run = False
                secs_since = cache.seconds_since_last_fetch()
                if secs_since == float("inf"):
                    # 从未请求过，立即执行首次查询
                    log.info("无历史记录，将立即执行首次查询")
                elif secs_since < MIN_REQUEST_INTERVAL:
                    # 距上次请求不足 1 小时，需要等待以满足速率限制
                    wait_seconds = MIN_REQUEST_INTERVAL - secs_since
                    log.warning(
                        "距上次请求 %.0f 分钟，需等待 %.0f 分钟以满足速率限制",
                        secs_since / 60, wait_seconds / 60
                    )
                    time.sleep(wait_seconds)
                # 如果 secs_since >= MIN_REQUEST_INTERVAL，无需等待，直接执行
            else:
                # 非首次运行：计算下次唤醒时间并等待
                # 同时考虑调度时刻和速率限制，取较晚的时刻
                wake_at = compute_next_wake(cache, SCHEDULED_MINUTE)
                wait_until(wake_at)

            # === 登录并拉取数据 ===
            log.info("[%s] 开始批量拉取...", datetime.now(timezone.utc).strftime("%H:%M:%S"))

            # 确保会话有效（超过 90 分钟会自动重新登录）
            if not st.ensure_fresh_session():
                log.error("登录失败，等待下一个调度周期")
                continue

            # 批量拉取最近 1 小时内发布的所有 TLE
            all_records = fetch_bulk_with_relogin(st)
            if all_records is None:
                log.error("本次拉取失败，等待下一个调度周期")
                continue

            # === 保存全量数据到缓存 ===
            # tle_cache.json 存储 Space-Track 返回的所有原始记录（覆盖旧数据）
            cache.save_raw_records(all_records)

            # === 筛选目标卫星 ===
            # 从全量数据中筛选出 NORAD_IDS 中的卫星
            raw_records = filter_by_norad(all_records, NORAD_IDS)
            found_ids = list(raw_records.keys())
            missing_ids = [nid for nid in NORAD_IDS if nid not in raw_records]

            # 记录筛选结果
            if found_ids:
                log.info("筛选命中：%s", ', '.join(str(i) for i in found_ids))
            if missing_ids:
                log.info("本批次未包含（过去 1 小时内无新 TLE）：%s", ', '.join(str(i) for i in missing_ids))

            # === 处理记录，Hash 比对，写入日志 ===
            process_records(raw_records, prev_data, last_hash, cache)
            
            # 处理完成，清除待处理标记
            cache.clear_pending()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已停止监控")