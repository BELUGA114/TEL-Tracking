#!/usr/bin/env python3
"""
Space-Track.org TLE 轨道监控脚本

功能：
  - 监控单颗或多颗卫星
  - TLE 哈希比对，检测轨道元素变化
  - 近地点过低预警 + 粗略再入时间估算
  - 登录失败指数退避
  - 网络抖动自动重试
  - 会话生命周期管理
  - 日志自动保存

说明：
  这个版本优先保证稳定性，因此默认顺序拉取数据，不做多线程并发。
  对 Space-Track 这种有登录状态的会话，顺序请求通常更稳妥。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import os
from dotenv import load_dotenv
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Optional

import requests

load_dotenv()
# ╔══════════════════════════════════════════════════════╗
# ║                    用户配置区                         ║
# ╠══════════════════════════════════════════════════════╣

# Space-Track 账号
USERNAME = os.getenv("SPACETRACK_USER")
PASSWORD = os.getenv("SPACETRACK_PASS")

# 监控目标：NORAD 编号列表，可填多个
# 常用目标示例：
#   25544   ISS
#   48274   JWST
#   68765   新格伦 BlueBird 7 (2026-085A)
NORAD_IDS = [68765]

# 轮询间隔（秒）。Space-Track 限速，建议不低于 120 秒
POLL_INTERVAL = 300

# 日志文件路径（追加写入，留空 "" 则不保存日志）
LOG_FILE = "tle_log.jsonl"

# 近地点预警阈值（km）。低于此值时显示预警并估算剩余寿命。
# 设为 0 或负数可关闭预警。
REENTRY_WARNING_KM = 200

# 只在 TLE 有更新时打印详情，设为 False 则每次都打印
ONLY_PRINT_ON_UPDATE = True

# 登录连续失败上限，超过后暂停 LOGIN_PAUSE_SECONDS 再恢复
LOGIN_MAX_FAILURES = 5
LOGIN_PAUSE_SECONDS = 1800  # 30 分钟

# 单次请求失败最大重试次数（针对网络抖动 / 5xx，不含登录失败）
REQUEST_MAX_RETRIES = 3
REQUEST_RETRY_BASE = 5      # 重试基础等待秒数，指数退避：5, 10, 20 ...

# ╚══════════════════════════════════════════════════════╝


BASE_URL = "https://www.space-track.org"
LOGIN_URL = f"{BASE_URL}/ajaxauth/login"
LOGOUT_URL = f"{BASE_URL}/ajaxauth/logout"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


class FetchStatus(Enum):
    """请求结果状态。"""

    RELOGIN = auto()   # 401，会话过期
    SKIP = auto()      # 临时错误，本轮跳过
    EMPTY = auto()     # 返回空列表（编号有误 / 尚未编目）


class SpaceTrackSession:
    """封装登录状态、重试和退避逻辑。"""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._login_failures = 0
        self._paused_until: Optional[float] = None

    def _check_login_response(self, resp: requests.Response) -> bool:
        """
        多重验证登录是否成功：
          1. HTTP 状态码必须为 200
          2. Session cookie 中存在 chocolatechip
          3. 响应 JSON 不含 {"Login": "Failed"}
        """
        if resp.status_code != 200:
            return False

        if "chocolatechip" not in self._session.cookies:
            return False

        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("Login") == "Failed":
                return False
        except ValueError:
            # 非 JSON 响应时，仅依靠 cookie 判断。
            pass

        return True

    def _on_login_failure(self) -> None:
        self._login_failures += 1
        if self._login_failures >= LOGIN_MAX_FAILURES:
            log.error(
                "   连续登录失败 %d 次，暂停 %d 秒后再试",
                self._login_failures,
                LOGIN_PAUSE_SECONDS,
            )
            self._paused_until = time.monotonic() + LOGIN_PAUSE_SECONDS
        else:
            wait = REQUEST_RETRY_BASE * (2 ** (self._login_failures - 1))
            log.warning("   第 %d 次失败，%d 秒后可重试", self._login_failures, wait)

    def _wait_if_paused(self) -> None:
        """如处于暂停期，阻塞等待直到暂停结束。"""
        if self._paused_until is None:
            return
        remaining = self._paused_until - time.monotonic()
        if remaining > 0:
            log.warning("   登录暂停中，等待 %d 秒...", int(remaining))
            time.sleep(remaining)
        self._paused_until = None

    def login_once(self) -> bool:
        """只执行一次登录请求。"""
        try:
            resp = self._session.post(
                LOGIN_URL,
                data={"identity": USERNAME, "password": PASSWORD},
                timeout=15,
            )
        except requests.RequestException as e:
            log.error("   登录网络错误: %s", e)
            self._on_login_failure()
            return False

        if self._check_login_response(resp):
            log.info("   登录成功")
            self._login_failures = 0
            self._paused_until = None
            return True

        log.error("   登录失败 (HTTP %d)", resp.status_code)
        try:
            log.error("   响应: %s", resp.json())
        except ValueError:
            log.error("   响应: %s", resp.text[:200])
        self._on_login_failure()
        return False

    def login_with_retry(self) -> bool:
        """
        登录并按指数退避重试。
        在连续失败达到上限后，会暂停一段时间再继续尝试。
        """
        while True:
            self._wait_if_paused()

            if self.login_once():
                return True

            if self._login_failures >= LOGIN_MAX_FAILURES:
                return False

            wait = REQUEST_RETRY_BASE * (2 ** (self._login_failures - 1))
            time.sleep(wait)

    def logout(self) -> None:
        try:
            self._session.get(LOGOUT_URL, timeout=10)
        except Exception:
            pass
        self._session.cookies.clear()

    def relogin(self) -> bool:
        """登出后重新登录。"""
        self.logout()
        self._session = requests.Session()
        return self.login_with_retry()

    def get(self, url: str) -> "requests.Response | FetchStatus":
        """
        带重试的 GET 请求。

        返回：
          - requests.Response：成功
          - FetchStatus.RELOGIN：401，会话过期
          - FetchStatus.SKIP：网络错误 / 5xx 重试耗尽 / 非预期状态码
        """
        for attempt in range(1, REQUEST_MAX_RETRIES + 1):
            try:
                resp = self._session.get(url, timeout=15)

                if resp.status_code == 401:
                    return FetchStatus.RELOGIN

                if resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")

                if resp.status_code != 200:
                    log.warning("  非预期状态码 %d", resp.status_code)
                    return FetchStatus.SKIP

                return resp

            except requests.RequestException as e:
                wait = REQUEST_RETRY_BASE * (2 ** (attempt - 1))
                if attempt < REQUEST_MAX_RETRIES:
                    log.warning(
                        "  请求错误（第 %d/%d 次）: %s，%d 秒后重试",
                        attempt,
                        REQUEST_MAX_RETRIES,
                        e,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    log.error(
                        "  请求失败，已重试 %d 次，本轮跳过: %s",
                        REQUEST_MAX_RETRIES,
                        e,
                    )
                    return FetchStatus.SKIP

        return FetchStatus.SKIP

    def __enter__(self) -> "SpaceTrackSession":
        return self

    def __exit__(self, *_) -> None:
        self.logout()
        self._session.close()


def fetch_tle(st: SpaceTrackSession, norad_id: int) -> dict | FetchStatus:
    """按 NORAD 编号拉取最新一条 TLE 记录。"""
    url = (
        f"{BASE_URL}/basicspacedata/query/class/gp"
        f"/NORAD_CAT_ID/{norad_id}"
        f"/orderby/EPOCH desc/limit/1/format/json"
    )
    result = st.get(url)

    if isinstance(result, FetchStatus):
        return result

    try:
        data = result.json()
    except ValueError as e:
        log.warning("  [%d] JSON 解析失败: %s", norad_id, e)
        return FetchStatus.SKIP

    if not data:
        log.warning("  [%d] 未找到数据（编号有误或尚未编目）", norad_id)
        return FetchStatus.EMPTY

    return data[0]


def fetch_with_relogin(st: SpaceTrackSession, norad_id: int) -> Optional[dict]:
    """
    获取 TLE；如会话过期则尝试重新登录后再取一次。
    返回 dict（成功）或 None（跳过本轮）。
    """
    result = fetch_tle(st, norad_id)

    if result is FetchStatus.RELOGIN:
        log.info(" [%d] 会话过期，重新登录...", norad_id)
        if not st.relogin():
            return None
        result = fetch_tle(st, norad_id)

    if isinstance(result, FetchStatus):
        return None

    return result


def parse_orbit(record: dict) -> dict:
    """抽取并标准化轨道字段。"""
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
    """
    基于 BSTAR 和简化大气模型估算剩余再入天数。
    仅做趋势参考，不适合作为精确预测。
    """
    peri = orbit["periapsis"]
    bstar = orbit["bstar"]
    period = orbit["period"]  # 分钟

    # 条件限制：近地点高于 400 km 或 bstar <= 0 或 周期无效
    if peri > 400.0 or bstar <= 0.0 or period <= 0:
        return None

    # 简化指数大气模型：只是启发式近似。
    rho_vol = 2e-10 * math.exp(-(peri - 200.0) / 60.0)

    # 转换为面密度（与 SGP4 的量纲做一个粗略对齐）
    H = 60000.0
    rho_area = rho_vol * H

    # SGP4 标准参考密度
    rho0 = 2.461e-5

    # 当前每天圈数
    n = 1440.0 / period

    # 衰减率 dn/dt (rev/day²)
    dn_dt = 3.0 * math.pi * (n ** 2) * bstar * (rho_area / rho0)

    if dn_dt <= 1e-12:
        return None

    # 再入圈速：约 16 rev/day（对应高度 ~100 km）
    n_reentry = 16.0
    if n <= n_reentry:
        return 0.0

    return (n - n_reentry) / dn_dt


def format_reentry_estimate(days: float) -> str:
    """将估算天数格式化为人类可读字符串。"""
    if days == 0.0:
        return "即将再入"
    if days < 1.0:
        return f"约 {days * 24:.0f} 小时内（粗估）"
    if days < 30.0:
        return f"约 {days:.1f} 天内（粗估）"
    return f"约 {days:.0f} 天（粗估，误差较大）"


def print_orbit(orbit: dict, prev: Optional[dict]) -> None:
    """打印单颗卫星的轨道信息。"""
    peri = orbit["periapsis"]
    apo = orbit["apoapsis"]
    delta = ""

    if prev:
        dp = peri - prev["periapsis"]
        da = apo - prev["apoapsis"]
        delta = f"  （近地点 {dp:+.1f} km，远地点 {da:+.1f} km）"

    print(
        f"""
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
  {orbit['tle2']}"""
    )

    if REENTRY_WARNING_KM > 0 and peri < REENTRY_WARNING_KM:
        days = estimate_reentry_days(orbit)
        if days is not None:
            est_str = format_reentry_estimate(days)
            print(f"   再入高风险：近地点 {peri:.1f} km，预计 {est_str}，实际误差可达数倍")
        else:
            print(f"   再入高风险：近地点 {peri:.1f} km")
            if orbit["bstar"] <= 0:
                print("     BSTAR=0，寿命无法估算（可能为初始定轨解，阻力项尚未计算）")
            else:
                print("     近地点 > 400 km 或周期无效，不满足估算条件")
    elif peri < 300:
        print(f"     注意：近地点 {peri:.1f} km，大气阻力明显，轨道将持续衰减")


def log_record(orbit: dict) -> None:
    """将轨道记录追加写入 JSONL 日志。"""
    if not LOG_FILE:
        return

    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **orbit}
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.error("   日志写入失败: %s", e)



def restore_last_state(norad_ids: list[int]) -> tuple[dict[int, dict], dict[int, str]]:
    """
    从 JSONL 日志中恢复每个 NORAD 的最后一条记录。
    返回：
      prev_data: dict[norad_id] -> orbit dict
      last_hash: dict[norad_id] -> tle_hash
    """
    prev_data: dict[int, dict] = {}
    last_hash: dict[int, str] = {}

    if not LOG_FILE:
        return prev_data, last_hash

    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            # 倒序读取（避免全文件扫描）
            lines = f.readlines()
    except OSError:
        return prev_data, last_hash

    seen: set[int] = set()

    # 从后往前找每个 NORAD 的最后一条
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        norad = entry.get("norad")
        if norad not in norad_ids:
            continue

        if norad in seen:
            continue

        # 恢复
        prev_data[norad] = entry
        last_hash[norad] = entry.get("tle_hash", "")

        seen.add(norad)

        if len(seen) == len(norad_ids):
            break

    if prev_data:
        log.info("   已从日志恢复 %d 个目标的历史状态", len(prev_data))

    return prev_data, last_hash

def fetch_all(st: SpaceTrackSession, norad_ids: list[int]) -> dict[int, Optional[dict]]:
    """顺序拉取所有目标的 TLE。"""
    results: dict[int, Optional[dict]] = {}
    for i, nid in enumerate(norad_ids):
        results[nid] = fetch_with_relogin(st, nid)
        if i < len(norad_ids) - 1:
            time.sleep(3)
    return results


def main() -> None:
    ids_str = ", ".join(str(i) for i in NORAD_IDS)
    log.info("🛰  Space-Track 轨道监控")
    log.info("   目标: %s", ids_str)
    log.info(
        "   间隔: %ds  |  再入预警: <%d km  |  日志: %s",
        POLL_INTERVAL,
        REENTRY_WARNING_KM,
        LOG_FILE or "关闭",
    )
    print()

    prev_data, last_hash = restore_last_state(NORAD_IDS)

    if prev_data:
        for norad_id in NORAD_IDS:
            orbit = prev_data.get(norad_id)
            if not orbit:
                continue
            print_orbit(orbit, None)
    
    with SpaceTrackSession() as st:
        if not st.login_with_retry():
            log.error("   初始登录失败，退出")
            return

        while True:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            log.info("   [%s] 开始轮询 %d 个目标...", now, len(NORAD_IDS))

            records = fetch_all(st, NORAD_IDS)

            for norad_id in NORAD_IDS:
                record = records.get(norad_id)
                if record is None:
                    continue

                orbit = parse_orbit(record)
                prev = prev_data.get(norad_id)
                cur_hash = orbit["tle_hash"]

                if cur_hash != last_hash.get(norad_id):
                    log.info(
                        "   [%d] 检测到 TLE 变化！(hash: %s → %s)",
                        norad_id,
                        last_hash.get(norad_id, "无"),
                        cur_hash,
                    )
                    print_orbit(orbit, prev)
                    log_record(orbit)
                    prev_data[norad_id] = orbit
                    last_hash[norad_id] = cur_hash
                elif not ONLY_PRINT_ON_UPDATE:
                    print_orbit(orbit, prev)
                else:
                    log.info(
                        "   — [%d] %s：TLE 未变化（hash %s）",
                        norad_id,
                        orbit["name"],
                        cur_hash,
                    )

            log.info("   下次轮询在 %d 秒后（%d 分钟）...\n", POLL_INTERVAL, POLL_INTERVAL // 60)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已停止监控")
