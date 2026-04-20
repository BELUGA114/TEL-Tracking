# Space-Track TLE Monitor

> **⚠️ 项目声明**
>
> 这个项目是一个：
> **轻量级空间目标监控工具（Space Situational Awareness 小玩具版）**
>
> **仅适合爱好者个人使用**
>
> 如果你对轨道、航天或者数据监控感兴趣，可以随意改造扩展 🚀 

---

一个面向 **Space-Track.org** 的轻量级轨道监控脚本，用于：

* 监控单颗或多颗卫星的 **TLE 更新**
* 自动检测轨道变化（基于哈希）
* 输出轨道参数变化（近地点 / 远地点等）
* 提供**近地点过低预警**
* 附带一个**极其简化的再入时间估算（仅供参考）**

---

## 特性

* 自动登录 + 会话管理（支持失效重登）
* 请求重试 + 指数退避（网络抖动友好）
* TLE 哈希比对，精准检测轨道更新
* 近地点高度预警（大气阻力显著时提醒）
* 极其简化的仅供参考的再入时间估算（基于 BSTAR）
* JSONL 日志记录（可用于后续分析）
* 启动自动恢复上次状态（避免误判“首次变化”）

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
NORAD_IDS = [25544]        # 监控目标（NORAD 编号）
POLL_INTERVAL = 300        # 轮询间隔（秒）
LOG_FILE = "tle_log.jsonl" # 日志文件
REENTRY_WARNING_KM = 200   # 再入预警阈值（km）
```

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

实际效果：

* **误差可能达到数倍甚至更大**
* 不适用于任何严肃分析或决策

---

### 如果你需要更专业的衰减预报：

推荐使用以下方案：

* 专业轨道传播器：如 SGP4 / SGP4‑XP 的官方实现
* 高精度大气模型：如 NRLMSISE‑00
* 数值积分与动力学建模

一个可参考的开源项目是 **[xpropagator](https://github.com/xpropagation/xpropagator)**。它是一个将美国太空军官方 SGP4/SGP4‑XP 封装为 gRPC 服务的轨道传播工具，支持：
* 单点传播与星历生成
* 编目级卫星批量处理
* 内置内存管理与并发控制

该服务精度远高于一般开源实现，尤其适合对中高轨卫星的长期预报。你可以将其部署为独立服务，并通过 gRPC 接口获得高精度的轨道状态数据，用于再入分析或衰减趋势判断。

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

* Space-Track 有速率限制（建议 ≥120 秒轮询）
* 请勿频繁请求，否则可能被限制访问
* 建议长期运行时使用稳定网络环境

---

## 🔗 相关链接
- [Space-Track.org](https://www.space-track.org/)
- [SGP4/SGP4-XP](https://www.celestrak.com/software/vallado-sw.php)
- [xpropagator](https://github.com/xpropagation/xpropagator)