#!/usr/bin/env python3
"""
xpropagator 集成测试脚本
验证残差分析功能是否正常工作
"""

from xpropagator_client import (
    is_service_alive,
    propagate_tle,
    classify_change_xprop,
)
from datetime import datetime, timezone
import sys


def test_service_connection():
    """测试 1: 服务连接"""
    print("=" * 60)
    print("测试 1: 检查 xpropagator 服务连接")
    print("=" * 60)
    
    alive = is_service_alive()
    if alive:
        print("服务已连接")
        return True
    else:
        print("服务未响应，请确认 Docker 容器正在运行")
        return False


def test_single_propagation():
    """测试 2: 单次轨道预报"""
    print("\n" + "=" * 60)
    print("测试 2: 单次 TLE 轨道预报")
    print("=" * 60)
    
    # ISS TLE 示例
    norad_id = 25544
    name = "ISS (ZARYA)"
    tle1 = "1 25544U 98067A   26116.52257038  .00009972  00000-0  18903-3 0  9999"
    tle2 = "2 25544  51.6321 195.8185 0006977 353.1614   6.9278 15.48971361563741"
    
    target_time = datetime.now(timezone.utc)
    
    print(f"NORAD ID: {norad_id}")
    print(f"卫星名称: {name}")
    print(f"目标时间: {target_time.isoformat()}")
    
    sv = propagate_tle(norad_id, name, tle1, tle2, target_time)
    
    if sv:
        print(f"\n预报成功:")
        print(f"  位置 (km):     X={sv.x:10.3f}, Y={sv.y:10.3f}, Z={sv.z:10.3f}")
        print(f"  速度 (km/s):  VX={sv.vx:10.6f}, VY={sv.vy:10.6f}, VZ={sv.vz:10.6f}")
        
        # 计算轨道高度（简化）
        altitude = (sv.x**2 + sv.y**2 + sv.z**2)**0.5 - 6378.137
        print(f"  轨道高度:      {altitude:.1f} km")
        return True
    else:
        print("预报失败")
        return False


def test_maneuver_detection():
    """测试 3: 残差分析 - 模拟真实机动场景"""
    print("\n" + "=" * 60)
    print("测试 3: 残差分析 - 模拟真实机动场景")
    print("=" * 60)
    
    # 真实的 TLE 数据（BlueBird 7 卫星，有明显轨道变化）
    # 蓝色起源新格伦第三次发射，一级回来了，卫星也回来了
    prev = {
        "norad": 68765,
        "name": "BLUE_BIRD",
        "epoch": "2026-04-19T11:38:06",
        "tle1": "1 68765U 26085A   26109.48477177  .00000000  00000-0  00000+0 0  9995",
        "tle2": "2 68765  36.1050 170.3509 0253100 160.1450 346.9830 15.82286134    05",
    }

    orbit = {
        "norad": 68765,
        "name": "BLUE_BIRD",
        "epoch": "2026-04-20T03:43:22",
        "tle1": "1 68765U 26085A   26110.15512012  .00033684  81193-5  17840-3 0  9994",
        "tle2": "2 68765  42.9612 171.3926 0162582 193.6002 166.0453 15.64310274   112",
    }

    print(f"\n卫星: {prev['name']} (NORAD {prev['norad']})")
    print(f"旧 TLE 历元: {prev['epoch']}")
    print(f"新 TLE 历元: {orbit['epoch']}")
    print(f"\n轨道根数变化:")
    # TLE Line 2 格式：列 8-16=倾角, 列 26-33=偏心率
    print(f"  倾角:     {prev['tle2'][8:16].strip()}° → {orbit['tle2'][8:16].strip()}°")
    print(f"  偏心率:   0.{prev['tle2'][26:33]} → 0.{orbit['tle2'][26:33]}")
    print(f"  BSTAR:    {prev['tle1'].split()[4]} → {orbit['tle1'].split()[4]}")
    
    result = classify_change_xprop(orbit, prev, maneuver_threshold_km=5.0)
    
    if result == "maneuver":
        print(f"\n分类结果: {result.upper()} (真实机动)")
        print("   残差 >= 5 km，检测到明显的轨道机动")
        return True
    elif result == "correction":
        print(f"\n分类结果: {result.upper()} (解算修正)")
        print("   残差 < 5 km，属于正常的轨道解算更新")
        return True
    else:
        print(f"\n分类失败: {result}")
        return False


def main():
    """运行所有测试"""
    print("\n" + "xpropagator 集成测试套件".center(50) + "\n")
    
    tests = [
        ("服务连接", test_service_connection),
        ("单次预报", test_single_propagation),
        ("残差分析(机动)", test_maneuver_detection),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            success = test_func()
            results.append((name, success))
        except Exception as e:
            print(f"\n测试异常: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))
    
    # 汇总结果
    print("\n" + "=" * 60)
    print("测试结果汇总".center(50))
    print("=" * 60)
    
    passed = sum(1 for _, s in results if s)
    total = len(results)
    
    for name, success in results:
        status = "通过" if success else "失败"
        print(f"  {status} - {name}")
    
    print("-" * 60)
    print(f"总计: {passed}/{total} 个测试通过")
    
    if passed == total:
        print("\n所有测试通过！xpropagator 集成正常。")
        return 0
    else:
        print(f"\n有 {total - passed} 个测试失败，请检查配置。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
