# Space-Track TLE Monitor

> **项目声明**
>
> 这个项目是一个：
> **轻量级空间目标监控工具（小玩具版SSA）**
>
> **仅适合爱好者个人使用**
>
> 如果你对轨道、航天或者数据监控感兴趣，可以随意改造扩展

---

一个面向 **Space-Track.org** 的轻量级轨道监控脚本，用于：

* 监控单颗或多颗卫星的 **TLE 更新**
* 自动检测轨道变化（基于哈希比对）
* 输出轨道参数变化（近地点 / 远地点等）
* 附带一个**极其简化的再入时间估算（仅供参考）**

---

## 特性

* **严格遵循 Space-Track API 速率限制**（每小时 1 次 gp 查询）
* **智能调度系统**：同时考虑调度时刻和速率限制，确保永远合规
* **批量拉取 + 本地筛选**：避开整点/半点高峰期，节省带宽，符合官方推荐
* **TLE 变化分类**：自动区分解算修正（Correction）与真实机动（Maneuver）
* **断点恢复机制**：程序崩溃后自动从缓存恢复未处理数据，避免数据丢失
* **双日志系统**：轨道数据文件（带 `change_type` 标记）+ 运行日志（JSONL，带轮转保护）
* **重启自动恢复状态**：从历史数据恢复上次轨道状态，避免误判“首次变化”


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
  reentry_warning_km: 200     # 近地点低于 200km 时发出预警
  only_print_on_update: true  # 只在 TLE 变化时打印，避免刷屏
```

**常用配置说明**：
- `norad_ids`: 要监控的卫星编号列表，可以在 [space-track.org](https://www.space-track.org) 查询
- `minute`: 每小时请求数据的分钟数，建议设置为 12、17、42、48 等，避开 0 和 30
- `only_print_on_update`: 设为 `true` 可以减少控制台输出，只在有更新时才显示

> **提示**：修改 `config.yaml` 后需要重启脚本才能生效

---

### 4. 运行脚本

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
  reentry_warning_km: 200     # 再入预警阈值（km）
  only_print_on_update: true  # 仅在 TLE 变化时打印

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

## 关于再入预测（非常重要）

> **本项目中的再入时间估算极其粗糙，仅供娱乐参考**

当前实现：

* 使用 **BSTAR + 简化指数大气模型**
* 通过轨道平均运动变化率进行估算
* **忽略了大量关键因素**（姿态、太阳活动、空间天气等）

实际**误差可能达到数倍甚至更大**，不适用于任何严肃分析或决策

---

### 如果你需要更专业的衰减预报：

推荐使用以下方案：

* 专业轨道传播器：如 SGP4 / SGP4‑XP 的官方实现
* 高精度大气模型：如 NRLMSISE‑00
* 数值积分与动力学建模

一个可参考的开源项目是 **[xpropagator](https://github.com/xpropagation/xpropagator)**。它是一个将美国太空军官方 SGP4/SGP4‑XP 
封装为 gRPC 服务的轨道传播工具，支持：单点传播与星历生成，编目级卫星批量处理，内置内存管理与并发控制。  
该服务**精度远高于一般开源实现**，尤其适合对中高轨卫星的长期预报。你可以将其部署为独立服务，并通过 gRPC 接口获得高精度的轨道状态数据，用于再入分析或衰减趋势判断。

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

**change_type 字段说明**：
- `initial`: 首次记录
- `correction`: 解算修正（近地点/远地点变化 < 5 km）
- `maneuver`: 真实机动（近地点/远地点变化 > 5 km）

可用于：轨道趋势分析，机动事件检测（直接过滤出 `change_type == "maneuver"`），数据归档与可视化等

---

## 注意事项


本项目严格遵守 Space-Track.org 的 API 使用规范：

### 速率限制

每小时仅允许向 `gp` 类端点发起 1 次请求，违规会导致账号被封

### 推荐的查询方式

不应针对单个 NORAD ID 频繁查询，而应使用批量查询获取过去一小时内发布的所有 TLE，然后在本地筛选目标卫星：

```
https://www.space-track.org/basicspacedata/query/class/gp/decay_date/null-val/CREATION_DATE/%3Enow-0.042/format/json
```

该查询会返回：
- 所有未衰减的卫星（`decay_date/null-val`）
- 在过去 1 小时内发布的 TLE（`CREATION_DATE/%3Enow-0.042`，0.042天 ≈ 1小时）
- JSON 格式输出（便于本地处理）

### 调度时间要求

- **避开整点和半点高峰期**（如 09:00、09:30 不可用），建议使用非高峰时段（如 09:12、09:48）
- 本脚本默认设置为每小时第 12 分钟执行 

### *请勿修改调度逻辑以规避速率限制*

---

## 相关链接
- [Space-Track.org](https://www.space-track.org/)
- [GP 类文档](https://www.space-track.org/documentation#api-basicSpaceDataGP)
- [API 限制和完整文档](https://www.space-track.org/documentation#/api)
- [SGP4/SGP4-XP](https://www.celestrak.com/software/vallado-sw.php)
- [xpropagator](https://github.com/xpropagation/xpropagator)