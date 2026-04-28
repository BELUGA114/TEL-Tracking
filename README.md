# Space-Track TLE Monitor

<a id="language-selector"></a>
**Language / 语言**: [🇺🇸 English](#english-version) | [🇨🇳 中文](#chinese-version)

---

<a id="english-version"></a>

A lightweight orbital monitoring script for Space-Track.org that provides:

- Monitoring of single or multiple satellites' TLE updates
- Automatic detection of orbital changes and distinction between solution corrections and real maneuvers (based on hash comparison)
- Output of orbital parameter changes (perigee/apogee, etc.)
- Simplified reentry time estimation (for reference only)

---

## Features

- Strictly follows Space-Track API rate limits (1 gp query per hour)
- Intelligent scheduling system: considers both scheduled time and rate limits
- Batch fetching + local filtering: avoids peak hours at :00 and :30
- TLE change classification: distinguishes between solution corrections and real maneuvers
  - Default simple threshold rules based on perigee/apogee
  - Optional high-precision residual analysis (requires xpropagator service)
- Breakpoint recovery mechanism: automatically recovers unprocessed data from cache after program crash

- Automatic state recovery on restart: restores last orbital state from historical data


---

## Quick Start

### 1. Install Python Dependencies

Make sure you have Python installed, then run in the project directory:

```bash
pip install requests python-dotenv
```

---

### 2. Configure Space-Track Account

**Step 1: Copy template file**

Copy `.env.example` and rename it to `.env`:

```bash
cp .env.example .env
```

**Step 2: Fill in credentials**

Open the `.env` file with a text editor and enter your Space-Track account credentials:

```env
SPACETRACK_USER=your_email@example.com
SPACETRACK_PASS=your_password
```


> - The `.env` file contains your account credentials, do not share it with others
> - If you don't have a Space-Track account, register at [space-track.org](https://www.space-track.org) first

---

### 3. Configure Monitoring Targets

If you want to modify the monitored satellites or adjust other parameters, edit the `config.yaml` file

**Simplest usage**: If you only want to monitor ISS, use the default configuration without modification

**Custom configuration example**:

```yaml
targets:
  norad_ids: [25544, 48273]  # Monitor multiple satellites, separated by commas

schedule:
  minute: 17  # Request data at the 17th minute of each hour (avoid peak hours at :00 and :30)

alerts:
  reentry_warning_km: 200                          # Warning when perigee is below 200km
  only_print_on_update: true                       # Only print when TLE changes to avoid spam
  fallback_maneuver_threshold_km: 5.0              # Fallback strategy maneuver detection threshold (km)
```

**Common configuration explanations**:
- `norad_ids`: List of satellite NORAD IDs to monitor, can be queried at [space-track.org](https://www.space-track.org)
- `minute`: Minute of each hour to request data, recommended values are 12, 17, 42, 48, etc., avoiding 0 and 30
- `only_print_on_update`: Set to `true` to reduce console output, only display when there are updates

> **Tip**: Restart the script after modifying `config.yaml` for changes to take effect

---

### 4. (Optional) Configure xpropagator Residual Analysis

To enable high-precision orbital prediction residual analysis, you need to deploy the xpropagator service first.

**Step 1: Install gRPC dependencies**

```bash
pip install grpcio grpcio-tools
```

**Step 2: Deploy xpropagator service**

See the [Orbital Prediction Backend (xpropagator)](#orbital-prediction-backend-xpropagator) section below.

**Step 3: Enable configuration**

Add to `config.yaml`:

```yaml
xpropagator:
  enabled: true              # Enable residual analysis
  host: localhost            # xpropagator service address (modify according to actual deployment)
  port: 50051                # gRPC port (modify according to actual deployment)
  maneuver_threshold_km: 5.0 # Maneuver detection threshold (km)
```

**Notes:**
- If xpropagator is not configured, the script will automatically fall back to simple perigee/apogee threshold rules
- Residual analysis is an optional feature and does not affect core monitoring functionality
- Fallback strategy threshold can be configured via `alerts.fallback_maneuver_threshold_km` (default 5.0 km)


---

### 5. Run the Script

Run in the project directory:

```bash
python spacetrack_monitor.py
```

On first run, the script will:
1. Load your account configuration
2. Log in to Space-Track
3. Execute the first data fetch immediately
4. Automatically check once every hour thereafter

Successful execution shows output similar to:

```
2026-04-27 21:31:54 Space-Track Orbital Monitor
2026-04-27 21:31:54 Config file: config.yaml | Credentials: .env
2026-04-27 21:31:54 Target: 25544
2026-04-27 21:31:54 Schedule: Every hour at minute 17 | Reentry warning: <200 km
```

---

## Configuration Details

### Business Configuration (config.yaml)

Parameters are located in `config.yaml`, restart the script after modification:

```yaml
targets:
  norad_ids: [25544]          # Monitoring targets (NORAD ID list)

schedule:
  minute: 12                  # Minute of each hour to request data (recommended 12 or 48)

files:
  data_file: tle_data.jsonl   # Orbital data file
  cache: tle_cache.json       # Temporary cache file
  run_log: tle_log.jsonl      # Runtime log file
  max_log_size_mb: 10         # Log rotation threshold (MB)

alerts:
  reentry_warning_km: 200                          # Reentry warning threshold (km)
  only_print_on_update: true                       # Print only when TLE changes
  fallback_maneuver_threshold_km: 5.0              # Fallback strategy maneuver detection threshold (km)

retry:
  login_max_failures: 5       # Maximum login failure attempts
  login_pause_seconds: 1800   # Wait time after login failure (seconds)
  request_max_retries: 3      # Maximum request retry attempts
  request_retry_base: 5       # Exponential backoff base (seconds)
```

### Data Files

The following files are automatically generated after running the script:

- **tle_data.jsonl**: Core orbital data (recorded on each TLE update), each record includes `change_type` field (initial/correction/maneuver) for easy post-processing filtering of real maneuver events, with rotation protection
- **tle_cache.json**: Temporary cache, saves last request time, full raw data and pending processing flags, supports breakpoint recovery, automatically overwritten
- **tle_log.jsonl**: Runtime logs, records program operation status, with rotation protection

> **Log Rotation**: When file size exceeds the configured threshold (default 10MB), it will be automatically renamed to `.bak` backup file.

---

## Output Examples

### Console Output

```text
2026-04-27 14:12:01 [25544] This batch has 3 solution records, taking the latest one
2026-04-27 14:12:01 [25544] TLE change detected! (hash: abc123 → def456, type: Solution Correction (Correction))

  ===============================================
    ISS (ZARYA)          NORAD 25544
    International Designator: 1998-067A
    Epoch:     2026-04-27T14:08:32
    Perigee:   418.5 km    Apogee: 421.2 km
    Inclination: 51.6400°   Period: 92.870 min
    Eccentricity: 0.0002000   BSTAR: 2.3456e-04
    TLE Hash: abcdef1234567890
  ===============================================  (Perigee +0.3 km, Apogee +0.2 km)
  1 25544U 98067A   ...
  2 25544  51.6400 ...
```

### Seemingly Anomalous Wait Times? (Actually Expected Behavior)

Example:
```text
2026-04-25 00:30:13 Next query: 02:12 UTC (in 102 minutes)
```

**Why does "102 minutes" appear?**

This is **expected normal behavior**, caused by the script's strict adherence to Space-Track API regulations:

1. **Current time**: 00:30, just completed a fetch
2. **Next scheduled time**: 01:12 (42 minutes from now)
3. **Rate limit requirement**: Must wait 60 minutes since last request (can only make legal requests after 01:30)
4. **Conflict resolution**: 01:12 < 01:30, Space-Track doesn't want us requesting during peak hours at :00 and :30, so postponed to next scheduled time 02:12
5. **Final wait time**: 02:12 - 00:30 = **102 minutes**

> **Scheduling is always predictable and always compliant for long-term benefits**
>
> The script prefers to wait longer rather than violate API regulations. This conservative strategy aims to ensure:
> Account safety (won't be banned for violations),
> Data reliability (each fetch occurs within legal time windows),
> Long-term stability (can run continuously for months or even years)



---

## Orbital Prediction Backend (xpropagator)

### Important Notice

**This repository does not contain or distribute official USSF SGP4/SGP4-XP binary files from Space-Track.org.**

Precise orbital prediction for this project is provided by external **xpropagator** service, which is based on
Space-Track.org officially released SGP4/SGP4-XP dynamic libraries, but **xpropagator itself also does not integrate these libraries**.

TEL-Tracking only calls external xpropagator service via network gRPC, the project itself contains no SGP4 source code or compiled artifacts

To use high-precision residual analysis functionality, please deploy xpropagator service yourself.

For detailed deployment instructions, refer to the [xpropagator official repository](https://github.com/xpropagation/xpropagator).

### Residual Analysis Principle

When TLE update is detected:

1. **Old TLE forward propagation**: Propagate old TLE to new TLE's epoch time
2. **New TLE initialization**: Initialize new TLE at new epoch time
3. **Calculate residuals**: Compute position difference in ECI Cartesian coordinates (km)
4. **Decision rules**:
   - Residual >= maneuver threshold → Real maneuver (Maneuver)
   - Residual < maneuver threshold → Solution correction (Correction)

This method is more accurate than directly comparing orbital elements, as it's based on USSF official SGP4-XP model,
comparing in state space, eliminating rounding errors from orbital element solutions.

---

## About Reentry Prediction

The reentry time estimation in this project is extremely rough, for entertainment reference only.

Current implementation:

- Uses BSTAR + simplified exponential atmospheric model
- Estimates through orbital mean motion change rate
- Ignores many critical factors (attitude, solar activity, space weather, etc.)

Actual errors may reach several times or more, not suitable for any serious analysis or decision-making.

For more professional decay forecasting, recommend using professional orbital propagators (such as SGP4/SGP4-XP) and high-precision atmospheric models (such as NRLMSISE-00).

---

## Data Format (JSONL)

### Orbital Data File (tle_data.jsonl)

Each TLE update records complete orbital parameters and change type:

```json
{
  "timestamp": "2026-04-27T14:12:01.123456+00:00",
  "change_type": "correction",
  "norad": 25544,
  "name": "ISS (ZARYA)",
  "periapsis": 418.5,
  "apoapsis": 421.2,
  "epoch": "2026-04-27T14:08:32+00:00",
  "tle_hash": "abcdef1234567890",
  ...
}
```

**change_type field explanation:**
- initial: First record
- correction: Solution correction (perigee/apogee change < threshold)
- maneuver: Real maneuver (perigee/apogee change > threshold)

Threshold can be configured via `alerts.fallback_maneuver_threshold_km` (default 5.0 km)

Can be used for: orbital trend analysis, maneuver event detection (directly filter out `change_type == "maneuver"`), data archiving and visualization, etc.

---

## Important Notes

This project strictly complies with Space-Track.org API usage specifications.

### Rate Limits

Only 1 request per hour is allowed to gp endpoints, violations will result in account suspension.

### Recommended Query Method

Should not frequently query individual NORAD IDs, instead use batch queries to get all TLEs published in the past hour, then filter target satellites locally.

```
https://www.space-track.org/basicspacedata/query/class/gp/decay_date/null-val/CREATION_DATE/%3Enow-0.042/format/json
```

This query returns:
- All non-decayed satellites (decay_date/null-val)
- TLEs published in the past 1 hour (CREATION_DATE/%3Enow-0.042, 0.042 days ≈ 1 hour)
- JSON format output (convenient for local processing)

### Scheduling Time Requirements

- Avoid peak hours at :00 and :30 (such as 09:00, 09:30), recommend using off-peak times (such as 09:12, 09:48)
- This script defaults to executing at the 17th minute of each hour 

### Do Not Modify Scheduling Logic to Circumvent Rate Limits

---

## Related Links

- [Space-Track.org](https://www.space-track.org/)
- [API Documentation](https://www.space-track.org/documentation#/api)
- [xpropagator](https://github.com/xpropagation/xpropagator)

---

<a id="chinese-version"></a>

# Space-Track TLE Monitor (中文版)

> **注意**: 这是中文版本。如需英文版本，请查看上方的 [English Version](#english-version)。

一个面向 Space-Track.org 的轻量级轨道监控脚本，用于：

- 监控单颗或多颗卫星的 TLE 更新
- 自动检测轨道变化并分辨解算修正与真实机动（基于哈希比对）
- 输出轨道参数变化（近地点 / 远地点等）
- 附带简化的再入时间估算（仅供参考）

---

## 特性

- 严格遵循 Space-Track API 速率限制（每小时 1 次 gp 查询）
- 智能调度系统：同时考虑调度时刻和速率限制
- 批量拉取 + 本地筛选：避开整点/半点高峰期
- TLE 变化分类：区分解算修正（Correction）与真实机动（Maneuver）
  - 默认使用简单的近地点/远地点阈值规则
  - 可选启用高精度残差分析（依靠 xpropagator 服务）
- 断点恢复机制：程序崩溃后自动从缓存恢复未处理数据

- 重启自动恢复状态：从历史数据恢复上次轨道状态


---

## 快速开始

### 1. 安装 Python 依赖

确保你已经安装了 Python，然后在项目目录下运行：

```bash
pip install requests python-dotenv
```

---

### 2. 配置 Space-Track 账号

**步骤 1：复制模板文件**

将 `.env.example` 复制一份并重命名为 `.env`：

```bash
cp .env.example .env
```

**步骤 2：填写账号密码**

用文本编辑器打开 `.env` 文件，填入你的 Space-Track 账号和密码：

```env
SPACETRACK_USER=your_email@example.com
SPACETRACK_PASS=your_password
```


> - `.env` 文件包含你的账号密码，不要分享给他人
> - 如果没有 Space-Track 账号，需要先去 [space-track.org](https://www.space-track.org) 注册

---

### 3. 配置监控目标

如果你想修改监控的卫星或调整其他参数，可以编辑 `config.yaml` 文件

**最简单的用法**：如果你只想监控 ISS，可以直接使用默认配置，无需修改

**自定义配置示例**：

```yaml
targets:
  norad_ids: [25544, 48273]  # 监控多个卫星，用逗号分隔

schedule:
  minute: 17  # 每小时第 17 分钟请求数据（避开整点/半点高峰）

alerts:
  reentry_warning_km: 200                          # 近地点低于 200km 时发出预警
  only_print_on_update: true                       # 只在 TLE 变化时打印，避免刷屏
  fallback_maneuver_threshold_km: 5.0              # 降级策略机动判定阈值（km）
```

**常用配置说明**：
- `norad_ids`: 要监控的卫星编号列表，可以在 [space-track.org](https://www.space-track.org) 查询
- `minute`: 每小时请求数据的分钟数，建议设置为 12、17、42、48 等，避开 0 和 30
- `only_print_on_update`: 设为 `true` 可以减少控制台输出，只在有更新时才显示

> **提示**：修改 `config.yaml` 后需要重启脚本才能生效

---

### 4. （可选）配置 xpropagator 残差分析

如需启用高精度的轨道预报残差分析功能，需要先部署 xpropagator 服务。

**步骤 1：安装 gRPC 依赖**

```bash
pip install grpcio grpcio-tools
```

**步骤 2：部署 xpropagator 服务**

参见下方的 [轨道预报后端 (xpropagator)](#轨道预报后端-xpropagator) 章节。

**步骤 3：启用配置**

在 `config.yaml` 中添加：

```yaml
xpropagator:
  enabled: true              # 启用残差分析
  host: localhost            # xpropagator 服务地址（根据实际部署修改）
  port: 50051                # gRPC 端口（根据实际部署修改）
  maneuver_threshold_km: 5.0 # 机动判定阈值（km）
```

**说明：**
- 如果不配置 xpropagator，脚本会自动降级到简单的近地点/远地点阈值规则
- 残差分析是可选功能，不影响核心监控功能
- 降级策略的阈值可通过 `alerts.fallback_maneuver_threshold_km` 配置（默认 5.0 km）


---

### 5. 运行脚本

在项目目录下运行：

```bash
python spacetrack_monitor.py
```

首次运行时，脚本会：
1. 加载你的账号配置
2. 登录 Space-Track
3. 立即执行第一次数据拉取
4. 之后每小时自动检查一次

看到类似以下输出表示运行成功：

```
2026-04-27 21:31:54 Space-Track 轨道监控
2026-04-27 21:31:54 配置文件: config.yaml | 密钥: .env
2026-04-27 21:31:54 目标: 25544
2026-04-27 21:31:54 调度: 每小时第 17 分 | 再入预警: <200 km
```

---

## 配置说明

### 业务配置（config.yaml）

参数位于 `config.yaml`，修改后重启脚本即可生效：

```yaml
targets:
  norad_ids: [25544]          # 监控目标（NORAD 编号列表）

schedule:
  minute: 12                  # 每小时请求的分钟数（建议 12 或 48）

files:
  data_file: tle_data.jsonl   # 轨道数据文件
  cache: tle_cache.json       # 临时缓存文件
  run_log: tle_log.jsonl      # 运行日志文件
  max_log_size_mb: 10         # 日志轮转阈值（MB）

alerts:
  reentry_warning_km: 200                          # 再入预警阈值（km）
  only_print_on_update: true                       # 仅在 TLE 变化时打印
  fallback_maneuver_threshold_km: 5.0              # 降级策略机动判定阈值（km）

retry:
  login_max_failures: 5       # 登录最大失败次数
  login_pause_seconds: 1800   # 登录失败后等待时间（秒）
  request_max_retries: 3      # 请求最大重试次数
  request_retry_base: 5       # 指数退避基数（秒）
```

### 数据文件

脚本运行后会自动生成以下文件：

- **tle_data.jsonl**: 核心轨道数据（每次 TLE 更新时记录），每条记录包含 `change_type` 字段（initial/correction/maneuver），便于后处理过滤真实机动事件，带轮转保护
- **tle_cache.json**: 临时缓存，保存上次请求时间、全量原始数据和待处理标记，支持断点恢复，自动覆盖
- **tle_log.jsonl**: 运行日志，记录程序运行状态，带轮转保护

> **日志轮转**：当文件大小超过配置的阈值（默认 10MB）时，会自动重命名为 `.bak` 备份文件。

---

## 输出示例

### 控制台输出

```text
2026-04-27 14:12:01 [25544] 本批次共 3 条解算记录，取最新一条
2026-04-27 14:12:01 [25544] 检测到 TLE 变化！(hash: abc123 → def456, 类型: 解算修正 (Correction))

  ===============================================
    ISS (ZARYA)          NORAD 25544
    国际编号: 1998-067A
    历元:     2026-04-27T14:08:32
    近地点:   418.5 km    远地点: 421.2 km
    倾角:     51.6400°   周期: 92.870 min
    离心率:   0.0002000   BSTAR: 2.3456e-04
    TLE Hash: abcdef1234567890
  ===============================================  （近地点 +0.3 km，远地点 +0.2 km）
  1 25544U 98067A   ...
  2 25544  51.6400 ...
```

### 看起来反常的等待提示?（其实是预期行为）

示例：
```text
2026-04-25 00:30:13 下次查询：02:12 UTC（102 分钟后）
```

**为什么会出现 "102 分钟"？**

这是**预期内的正常行为**，是脚本对 Space-Track API 规定的严格遵守导致的：

1. **当前时间**：00:30，刚完成一次拉取
2. **下一个调度时刻**：01:12（距离现在 42 分钟）
3. **速率限制要求**：距上次请求必须满 60 分钟（即 01:30 之后才能再次合法请求）
4. **冲突解决**：01:12 < 01:30，Space-Track 不希望我们在整点和半点高峰期请求，因此推迟到下一个调度时刻 02:12
5. **最终等待时间**：02:12 - 00:30 = **102 分钟**

> **调度永远可预测、永远合规是持续收益**
>
> 脚本宁可多等一会儿，也绝不违反 API 规定。这种保守策略旨在确保：
> 账号安全（不会因违规被封禁），
> 数据可靠性（每次拉取都在合法时间窗口内），
> 长期稳定性（可持续运行数月甚至数年）



---

## 轨道预报后端 (xpropagator)

### 重要声明

**本仓库不包含或分发来自 Space-Track.org 的官方 USSF SGP4/SGP4-XP 二进制文件。**

本项目的精密轨道预报由外部 **xpropagator** 服务提供，该服务基于
Space-Track.org 官方发布的 SGP4/SGP4-XP 动态库，但 **xpropagator 本身同样不集成这些库**。

TEL-Tracking 仅通过网络 gRPC 调用外部 xpropagator 服务，项目本身不含任何 SGP4 源码或编译产物

如需使用高精度残差分析功能，请自行部署 xpropagator 服务。

详细部署说明请参考 [xpropagator 官方仓库](https://github.com/xpropagation/xpropagator)。

### 残差分析原理

当检测到 TLE 更新时：

1. **旧 TLE 向前传播**：将旧 TLE 传播到新 TLE 的历元时刻
2. **新 TLE 初始化**：在新历元时刻初始化新 TLE
3. **计算残差**：在 ECI 笛卡尔坐标系中计算位置差（km）
4. **判定规则**：
   - 残差 >= 机动判定阈值 → 真实机动（Maneuver）
   - 残差 < 机动判定阈值 → 解算修正（Correction）

这种方法比直接比较轨道根数更准确，因为基于 USSF 官方 SGP4-XP 模型，
在状态空间比较，消除了轨道根数解算的舍入误差。

---

## 关于再入预测

本项目中的再入时间估算极其粗糙，仅供娱乐参考。

当前实现：

- 使用 BSTAR + 简化指数大气模型
- 通过轨道平均运动变化率进行估算
- 忽略了大量关键因素（姿态、太阳活动、空间天气等）

实际误差可能达到数倍甚至更大，不适用于任何严肃分析或决策。

如需更专业的衰减预报，推荐使用专业轨道传播器（如 SGP4/SGP4-XP）和高精度大气模型（如 NRLMSISE-00）。

---

## 数据格式（JSONL）

### 轨道数据文件（tle_data.jsonl）

每次 TLE 更新会记录完整轨道参数和变化类型：

```json
{
  "timestamp": "2026-04-27T14:12:01.123456+00:00",
  "change_type": "correction",
  "norad": 25544,
  "name": "ISS (ZARYA)",
  "periapsis": 418.5,
  "apoapsis": 421.2,
  "epoch": "2026-04-27T14:08:32+00:00",
  "tle_hash": "abcdef1234567890",
  ...
}
```

**change_type 字段说明：**
- initial: 首次记录
- correction: 解算修正（近地点/远地点变化 < 阈值）
- maneuver: 真实机动（近地点/远地点变化 > 阈值）

阈值可通过 `alerts.fallback_maneuver_threshold_km` 配置（默认 5.0 km）

可用于：轨道趋势分析，机动事件检测（直接过滤出 `change_type == "maneuver"`），数据归档与可视化等

---

## 注意事项

本项目严格遵守 Space-Track.org 的 API 使用规范。

### 速率限制

每小时仅允许向 gp 类端点发起 1 次请求，违规会导致账号被封。

### 推荐的查询方式

不应针对单个 NORAD ID 频繁查询，而应使用批量查询获取过去一小时内发布的所有 TLE，然后在本地筛选目标卫星。

```
https://www.space-track.org/basicspacedata/query/class/gp/decay_date/null-val/CREATION_DATE/%3Enow-0.042/format/json
```

该查询会返回：
- 所有未衰减的卫星（decay_date/null-val）
- 在过去 1 小时内发布的 TLE（CREATION_DATE/%3Enow-0.042，0.042天约等于1小时）
- JSON 格式输出（便于本地处理）

### 调度时间要求

- 避开整点和半点高峰期（如 09:00、09:30），建议使用非高峰时段（如 09:12、09:48）
- 本脚本默认设置为每小时第 17 分钟执行 

### 请勿修改调度逻辑以规避速率限制

---

## 相关链接

- [Space-Track.org](https://www.space-track.org/)
- [API 文档](https://www.space-track.org/documentation#/api)
- [xpropagator](https://github.com/xpropagation/xpropagator)

---

[↑ Back to Top / 返回顶部](#language-selector)