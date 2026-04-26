#!/usr/bin/env python3
"""
Space-Track.org TLE 轨道监控脚本（合规版）

合规要求：
  - 每小时仅向 gp 类发送 1 次请求
  - 使用推荐查询拉取最近一小时内发布的所有 TLE，本地筛选目标
  - 请求调度在每小时第 12 分钟，避开整点/半点高峰期
  - 本地缓存上次查询时间戳，重启后不会重复触发请求

功能：
  - 监控单颗或多颗卫星
  - TLE 哈希比对，检测轨道元素变化
  - 近地点过低预警 + 粗略再入时间估算
  - 登录失败有界重试（不无限循环）
  - 网络抖动自动重试
  - 会话生命周期管理（每次请求前检查会话新鲜度）
  - 日志自动保存（JSONL）
  - 缓存持久化（JSON）
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

load_dotenv()

# ╔══════════════════════════════════════════════════════╗
# ║                    用户配置区                         ║
# ╠══════════════════════════════════════════════════════╣

USERNAME = os.getenv("SPACETRACK_USER")
PASSWORD = os.getenv("SPACETRACK_PASS")

# 监控目标：NORAD 编号列表，可填多个
# 常用目标示例：
#   25544  ISS
#   68765  新格伦 BlueBird 7 (2026-085A)
NORAD_IDS: list[int] = [68765, 25544]

# 每小时请求的分钟数（建议 12 或 48，避开整点/半点高峰）
SCHEDULED_MINUTE = 12

LOG_FILE = "tle_log.jsonl"
CACHE_FILE = "tle_cache.json"
REENTRY_WARNING_KM = 200
ONLY_PRINT_ON_UPDATE = True

LOGIN_MAX_FAILURES = 5
LOGIN_PAUSE_SECONDS = 1800  # 30 分钟（仅用于日志提示）

REQUEST_MAX_RETRIES = 3
REQUEST_RETRY_BASE = 5  # 指数退避：5, 10, 20 ...

MIN_REQUEST_INTERVAL = 3600  # 速率保护：两次请求最小间隔（秒）

# Space-Track 会话最长有效期（秒）；保守取 90 分钟
SESSION_MAX_AGE = 5400

# ╚══════════════════════════════════════════════════════╝

BASE_URL = "https://www.space-track.org"
LOGIN_URL = f"{BASE_URL}/ajaxauth/login"
LOGOUT_URL = f"{BASE_URL}/ajaxauth/logout"
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


# ── 本地缓存 ──────────────────────────────────────────────

class LocalCache:
    """持久化缓存：last_fetch_ts + tle_data。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict = {"last_fetch_ts": None, "tle_data": {}}
        if path:
            self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("缓存根节点不是 dict")
            self._data["last_fetch_ts"] = raw.get("last_fetch_ts", None)
            tle_data = raw.get("tle_data", {})
            if not isinstance(tle_data, dict):
                log.warning("   缓存 tle_data 字段类型异常，已重置")
                tle_data = {}
            self._data["tle_data"] = tle_data
            log.info("   已加载本地缓存：%s", self._path)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            log.warning("   缓存加载失败（将从头开始）: %s", e)

    def _save(self) -> None:
        if not self._path:
            return
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log.error("   缓存写入失败: %s", e)

    @property
    def last_fetch_ts(self) -> Optional[datetime]:
        ts = self._data.get("last_fetch_ts")
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            log.warning("   缓存时间戳格式异常，已忽略: %s", ts)
            return None

    def seconds_since_last_fetch(self) -> float:
        ts = self.last_fetch_ts
        if ts is None:
            return float("inf")
        return (datetime.now(timezone.utc) - ts).total_seconds()

    def get_orbit(self, norad_id: int) -> Optional[dict]:
        return self._data["tle_data"].get(str(norad_id))

    def mark_fetched(self) -> None:
        """仅更新时间戳（请求成功但无命中目标时使用）。"""
        self._data["last_fetch_ts"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def update(self, orbits: dict[int, dict]) -> None:
        """更新时间戳和 TLE 数据。"""
        self._data["last_fetch_ts"] = datetime.now(timezone.utc).isoformat()
        for norad_id, orbit in orbits.items():
            self._data["tle_data"][str(norad_id)] = orbit
        self._save()

    def all_cached_orbits(self) -> dict[int, dict]:
        result: dict[int, dict] = {}
        for k, v in self._data.get("tle_data", {}).items():
            try:
                result[int(k)] = v
            except (ValueError, TypeError):
                log.warning("   缓存中存在非法键，已跳过: %r", k)
        return result


# ── 调度器 ────────────────────────────────────────────────

def next_scheduled_time(minute: int = SCHEDULED_MINUTE) -> datetime:
    """
    返回下一个 :MM 分的 UTC 时刻。
    加 30 秒缓冲窗口，避免秒级误差时跳到下一小时。
    """
    now = datetime.now(timezone.utc)
    target = now.replace(minute=minute, second=0, microsecond=0)
    if now > target + timedelta(seconds=30):
        target += timedelta(hours=1)
    return target


def wait_until(target: datetime) -> None:
    """阻塞到指定时刻（每分钟唤醒一次，便于 Ctrl-C 响应）。"""
    while True:
        secs = (target - datetime.now(timezone.utc)).total_seconds()
        if secs <= 0:
            return
        log.info(
            "   下次查询：%s UTC（%.0f 分钟后）",
            target.strftime("%H:%M"),
            secs / 60,
        )
        time.sleep(min(secs, 60))


def compute_next_wake(cache: LocalCache, minute: int = SCHEDULED_MINUTE) -> datetime:
    """
    合并两个约束，取较晚时刻一次性等待：
      1. 下一个调度时刻（:MM 分）
      2. 距上次请求满 MIN_REQUEST_INTERVAL 的时刻
    """
    sched = next_scheduled_time(minute)

    secs_since = cache.seconds_since_last_fetch()
    if secs_since < MIN_REQUEST_INTERVAL:
        rate_ok_at = datetime.now(timezone.utc) + timedelta(
            seconds=MIN_REQUEST_INTERVAL - secs_since
        )
        if rate_ok_at > sched:
            log.info(
                "   速率保护：需等至 %s UTC（距上次请求仅 %.0f 分钟）",
                rate_ok_at.strftime("%H:%M"),
                secs_since / 60,
            )
            while sched <= rate_ok_at:
                sched += timedelta(hours=1)

    return sched


# ── Space-Track 会话 ──────────────────────────────────────

class FetchStatus(Enum):
    RELOGIN = auto()  # 401，会话过期
    SKIP = auto()     # 临时错误，本轮跳过


class SpaceTrackSession:
    """封装登录状态、重试和退避逻辑。"""

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
        try:
            resp = self._session.post(
                LOGIN_URL,
                data={"identity": USERNAME, "password": PASSWORD},
                timeout=15,
            )
        except requests.RequestException as e:
            log.error("   登录网络错误: %s", e)
            return False

        if self._check_login_response(resp):
            log.info("   登录成功")
            self._login_failures = 0
            self._logged_in_at = time.monotonic()
            return True

        log.error("   登录失败 (HTTP %d)", resp.status_code)
        try:
            log.error("   响应: %s", resp.json())
        except ValueError:
            log.error("   响应: %s", resp.text[:200])
        return False

    def login_with_retry(self) -> bool:
        """有界重试，最多 LOGIN_MAX_FAILURES 次，返回 False 时由调用方决定后续处理。"""
        for attempt in range(1, LOGIN_MAX_FAILURES + 1):
            if self.login_once():
                return True
            self._login_failures += 1
            if attempt < LOGIN_MAX_FAILURES:
                wait = REQUEST_RETRY_BASE * (2 ** (attempt - 1))
                log.warning(
                    "   登录失败（第 %d/%d 次），%d 秒后重试",
                    attempt, LOGIN_MAX_FAILURES, wait,
                )
                time.sleep(wait)
            else:
                log.error(
                    "   连续登录失败 %d 次，放弃本轮（建议等待 %d 分钟后再试）",
                    LOGIN_MAX_FAILURES,
                    LOGIN_PAUSE_SECONDS // 60,
                )
        return False

    def ensure_fresh_session(self) -> bool:
        """若会话超过 SESSION_MAX_AGE，主动重新登录。"""
        if self._logged_in_at is None:
            return self.login_with_retry()
        age = time.monotonic() - self._logged_in_at
        if age > SESSION_MAX_AGE:
            log.info("   会话已存在 %.0f 分钟，主动刷新登录...", age / 60)
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
        for attempt in range(1, REQUEST_MAX_RETRIES + 1):
            try:
                resp = self._session.get(url, timeout=30)
                if resp.status_code == 401:
                    return FetchStatus.RELOGIN
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                if resp.status_code != 200:
                    log.warning("   非预期状态码 %d", resp.status_code)
                    return FetchStatus.SKIP
                return resp
            except requests.RequestException as e:
                wait = REQUEST_RETRY_BASE * (2 ** (attempt - 1))
                if attempt < REQUEST_MAX_RETRIES:
                    log.warning(
                        "   请求错误（第 %d/%d 次）: %s，%d 秒后重试",
                        attempt, REQUEST_MAX_RETRIES, e, wait,
                    )
                    time.sleep(wait)
                else:
                    log.error("   请求失败，已重试 %d 次，本轮跳过: %s", REQUEST_MAX_RETRIES, e)
                    return FetchStatus.SKIP
        return FetchStatus.SKIP

    def __enter__(self) -> "SpaceTrackSession":
        return self

    def __exit__(self, *_) -> None:
        self.logout()
        self._session.close()


# ── 批量拉取 + 本地筛选 ───────────────────────────────────

def fetch_bulk_tle(st: SpaceTrackSession) -> "list[dict] | FetchStatus":
    """批量拉取最近 1 小时内发布的所有 TLE（1 次 gp 配额）。"""
    log.info("   → 请求批量 TLE（最近 1 小时发布）...")
    result = st.get(BULK_TLE_URL)
    if isinstance(result, FetchStatus):
        return result
    try:
        data = result.json()
    except ValueError as e:
        log.warning("   JSON 解析失败: %s", e)
        return FetchStatus.SKIP
    log.info("   ← 收到 %d 条记录", len(data))
    return data


def fetch_bulk_with_relogin(st: SpaceTrackSession) -> Optional[list[dict]]:
    """带重登录保护的批量拉取。"""
    result = fetch_bulk_tle(st)
    if result is FetchStatus.RELOGIN:
        log.info("   会话过期，重新登录...")
        if not st.relogin():
            return None
        result = fetch_bulk_tle(st)
    if isinstance(result, FetchStatus):
        return None
    return result


def filter_by_norad(records: list[dict], norad_ids: list[int]) -> dict[int, dict]:
    """从批量结果中筛选目标 NORAD ID（取每个 ID 的第一条匹配）。"""
    target_set = set(norad_ids)
    found: dict[int, dict] = {}
    for rec in records:
        try:
            nid = int(rec.get("NORAD_CAT_ID") or 0)
        except (ValueError, TypeError):
            continue
        if nid in target_set and nid not in found:
            found[nid] = rec
    return found


# ── 轨道解析 / 显示 / 日志 ────────────────────────────────

def parse_orbit(record: dict) -> dict:
    name = (record.get("OBJECT_NAME") or "").strip()
    tle1 = record.get("TLE_LINE1", "")
    tle2 = record.get("TLE_LINE2", "")
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
    """基于 BSTAR 和简化大气模型估算剩余再入天数（仅供参考）。"""
    peri, bstar, period = orbit["periapsis"], orbit["bstar"], orbit["period"]
    if peri > 400.0 or bstar <= 0.0 or period <= 0:
        return None
    rho_area = 2e-10 * math.exp(-(peri - 200.0) / 60.0) * 60000.0
    rho0 = 2.461e-5
    n = 1440.0 / period
    dn_dt = 3.0 * math.pi * (n ** 2) * bstar * (rho_area / rho0)
    if dn_dt <= 1e-12:
        return None
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


def log_record(orbit: dict) -> None:
    if not LOG_FILE:
        return
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **orbit}
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.error("   日志写入失败: %s", e)


# ── 状态恢复（兼容旧版 JSONL 日志） ─────────────────────────

def restore_from_log(norad_ids: list[int]) -> dict[int, dict]:
    prev_data: dict[int, dict] = {}
    if not LOG_FILE:
        return prev_data
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return prev_data
    seen: set[int] = set()
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
        log.info("   已从日志恢复 %d 个目标的历史状态", len(prev_data))
    return prev_data


# ── 记录处理 ──────────────────────────────────────────────

def process_records(
    raw_records: dict[int, dict],
    prev_data: dict[int, dict],
    last_hash: dict[int, str],
    cache: LocalCache,
) -> None:
    """
    比对哈希、打印变化、写日志。
    无论是否命中都推进时间戳，防止速率保护卡死。
    """
    updated_orbits: dict[int, dict] = {}

    for norad_id in NORAD_IDS:
        record = raw_records.get(norad_id)
        if record is None:
            log.info("   — [%d] 本批次无数据（过去 1 小时内未发布新 TLE）", norad_id)
            continue

        orbit = parse_orbit(record)
        prev = prev_data.get(norad_id)
        cur_hash = orbit["tle_hash"]

        if cur_hash != last_hash.get(norad_id):
            log.info(
                "   [%d] 检测到 TLE 变化！(hash: %s → %s)",
                norad_id, last_hash.get(norad_id, "无"), cur_hash,
            )
            print_orbit(orbit, prev)
            log_record(orbit)
            prev_data[norad_id] = orbit
            last_hash[norad_id] = cur_hash
        elif not ONLY_PRINT_ON_UPDATE:
            print_orbit(orbit, prev)
        else:
            log.info("   — [%d] %s：TLE 未变化（hash %s）", norad_id, orbit["name"], cur_hash)

        updated_orbits[norad_id] = orbit

    if updated_orbits:
        cache.update(updated_orbits)
    else:
        cache.mark_fetched()


# ── 主循环 ────────────────────────────────────────────────

def main() -> None:
    log.info("🛰  Space-Track 轨道监控（合规版）")
    log.info("   目标: %s", ", ".join(str(i) for i in NORAD_IDS))
    log.info(
        "   调度: 每小时第 %02d 分  |  再入预警: <%d km  |  日志: %s  |  缓存: %s",
        SCHEDULED_MINUTE, REENTRY_WARNING_KM,
        LOG_FILE or "关闭", CACHE_FILE or "关闭",
    )
    print()

    cache = LocalCache(CACHE_FILE)
    cached_orbits = cache.all_cached_orbits()

    if cached_orbits:
        prev_data: dict[int, dict] = {k: v for k, v in cached_orbits.items() if k in NORAD_IDS}
        log.info("   已从缓存恢复 %d 个目标", len(prev_data))
    else:
        prev_data = restore_from_log(NORAD_IDS)

    last_hash: dict[int, str] = {
        nid: orbit.get("tle_hash", "") for nid, orbit in prev_data.items()
    }

    for norad_id in NORAD_IDS:
        orbit = prev_data.get(norad_id)
        if orbit:
            print_orbit(orbit, None)

    with SpaceTrackSession() as st:
        while True:
            wake_at = compute_next_wake(cache, SCHEDULED_MINUTE)
            wait_until(wake_at)

            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            log.info("   [%s] 开始批量拉取...", now_str)

            if not st.ensure_fresh_session():
                log.error("   登录失败，等待下一个调度周期")
                continue

            all_records = fetch_bulk_with_relogin(st)
            if all_records is None:
                log.error("   本次拉取失败，等待下一个调度周期")
                continue

            raw_records = filter_by_norad(all_records, NORAD_IDS)
            found_ids = list(raw_records.keys())
            missing_ids = [nid for nid in NORAD_IDS if nid not in raw_records]

            if found_ids:
                log.info("   筛选命中：%s", ", ".join(str(i) for i in found_ids))
            if missing_ids:
                log.info("   本批次未包含（过去 1 小时内无新 TLE）：%s",
                         ", ".join(str(i) for i in missing_ids))

            process_records(raw_records, prev_data, last_hash, cache)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已停止监控")