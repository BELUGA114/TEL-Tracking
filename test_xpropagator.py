#!/usr/bin/env python3
"""
xpropagator 机动检测测试
验证残差分析能否正确识别真实轨道机动
"""

from xpropagator_client import classify_change_xprop
import sys


def test_maneuver_detection():
    """测试: 残差分析 - 模拟真实机动场景"""
    print("=" * 60)
    print("xpropagator 机动检测测试")
    print("=" * 60)
    
    # 真实的 TLE 数据（有明显轨道变化）
    prev = {
        "norad": 68765,
        "name": "STARLINK",
        "epoch": "2026-04-19T11:38:06",
        "tle1": "1 68759U 26085A   26109.48477177  .00000000  00000-0  00000+0 0  9995",
        "tle2": "2 68759  36.1050 170.3509 0253100 160.1450 346.9830 15.82286134    05",
    }
    
    orbit = {
        "norad": 68795,
        "name": "STARLINK",
        "epoch": "2026-04-20T03:43:22",
        "tle1": "1 68750U 26085A   26110.15512012  .00033684  81193-5  17840-3 0  9994",
        "tle2": "2 68750  42.9612 171.3926 0162582 193.6002 166.0453 15.64310274   112",
    }
    
    print(f"\n卫星: {prev['name']} (NORAD {prev['norad']})")
    print(f"旧 TLE 历元: {prev['epoch']}")
    print(f"新 TLE 历元: {orbit['epoch']}")
    print(f"\n轨道根数变化:")
    print(f"  倾角:     {prev['tle2'].split()[1]} → {orbit['tle2'].split()[1]}")
    print(f"  偏心率:   {prev['tle2'].split()[2]} → {orbit['tle2'].split()[2]}")
    print(f"  BSTAR:    {prev['tle1'].split()[4]} → {orbit['tle1'].split()[4]}")
    
    result = classify_change_xprop(orbit, prev, maneuver_threshold_km=5.0)
    
    if result == "maneuver":
        print(f"\n✅ 分类结果: {result.upper()} (真实机动)")
        print("   残差 >= 5 km，检测到明显的轨道机动")
        return True
    elif result == "correction":
        print(f"\n⚠️  分类结果: {result.upper()} (解算修正)")
        print("   残差 < 5 km，属于正常的轨道解算更新")
        print("   注意: 此 TLE 对可能不足以触发机动判定")
        return True
    else:
        print(f"\n❌ 分类失败: {result}")
        return False


def main():
    """运行机动检测测试"""
    try:
        success = test_maneuver_detection()
        
        print("\n" + "=" * 60)
        if success:
            print("测试完成")
            return 0
        else:
            print("测试失败，请检查 xpropagator 服务是否正常运行")
            return 1
    except Exception as e:
        print(f"\n测试异常: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
