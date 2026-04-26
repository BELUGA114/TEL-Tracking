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

* 严格遵循 Space-Track API 速率限制（每小时 1 次 gp 查询）
* 批量拉取 + 避开整点/半点高峰期 + 本地筛选（节省带宽，符合官方推荐）
* 近地点高度预警（大气阻力显著时提醒）
* 双日志系统：轨道数据日志 + 运行日志（JSONL，带轮转保护）
* 重启自动恢复上次状态（避免误判"首次变化"）


---

## 快速开始

### 1️ 安装依赖

```bash
pip install requests python-dotenv
```



### 2️ 配置账号（推荐使用 `.env`）

创建 `.env` 文件：

```env
SPACETRACK_USER=your_username
SPACETRACK_PASS=your_password
```



### 3️ 运行脚本

```bash
python spacetrack_monitor.py
```

---

## 配置说明

顶部提供可调参数：

```python
NORAD_IDS = [68765, 25544]    # 监控目标（NORAD 编号列表）
SCHEDULED_MINUTE = 12         # 每小时请求的分钟数（建议 12 或 48）
DATA_LOG_FILE = "tle_data.jsonl"  # 最终轨道数据文件
CACHE_FILE = "tle_cache.json"     # 临时缓存文件
LOG_FILE = "tle_log.jsonl"        # 运行日志文件
REENTRY_WARNING_KM = 200      # 再入预警阈值（km）
ONLY_PRINT_ON_UPDATE = True   # 仅在 TLE 变化时打印
```

### 文件说明

- **tle_data.jsonl**: 存储最终的轨道数据（每次 TLE 更新时记录），带轮转保护
- **tle_cache.json**: 临时缓存，保存上次请求时间和最新 TLE，自动覆盖
- **tle_log.jsonl**: 运行日志，记录程序运行状态，带轮转保护

当文件大小超过 10MB 时，会自动轮转为 `.bak` 备份文件。

---

## 输出示例

```text
===============================================
  STARLINK-XXXX        NORAD 68765
  国际编号: 2026-XXXA
  历元:     2026-04-20T12:34:56
  近地点:   210.3 km    远地点: 320.1 km
  倾角:     53.0000°   周期: 90.123 min
  离心率:   0.0012345   BSTAR: 2.3456e-04
  TLE Hash: abcdef1234567890
===============================================
```

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

## 日志格式（JSONL）

每次 TLE 更新会记录：

```json
{"timestamp": "...", "norad": 25544, "periapsis": 400.1, ...}
```

可用于：

* 轨道趋势分析
* 可视化
* 数据归档

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