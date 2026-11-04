import requests
import json
import sys

# 对应大屏后端 full_process.py 启动的回调服务端口
TARGET_URL = "http://127.0.0.1:8004/"

def send_signal(event_name, extra_data=None):
    """向大屏后端发送POST请求模拟A2信号"""
    payload = {
        "event": event_name,
        "source": "A2_Manual_Simulator"
    }
    if extra_data:
        payload.update(extra_data)
    
    try:
        print(f"\n[🚀 发送中] 目标地址: {TARGET_URL}")
        print(f"[📦 报文内容] {json.dumps(payload, ensure_ascii=False)}")
        
        response = requests.post(TARGET_URL, json=payload, timeout=5)
        
        print(f"[✅ 发送成功] 后端响应状态码: {response.status_code}")
        print(f"[📄 后端返回消息] {response.text}")
    except requests.exceptions.ConnectionError:
        print("\n❌ [连接失败] 无法连接到大屏后端，请确保大屏控制后端 (full_process.py) 已经在运行且 8004 端口已开放！")
    except Exception as e:
        print(f"\n❌ [发送异常] 错误原因: {e}")

def main():
    print("=" * 60)
    print("      🚀 A2 机器人状态机信号手动模拟工具 (大屏调试专用) 🚀")
    print("=" * 60)
    print(f"当前监控目标大屏后端地址: {TARGET_URL}\n")
    
    menu = {
        "1": ("wait-person-start", "到达找人点位 —— [切换至机器人主视角并启动ReID识别]"),
        "2": ("fetch-start", "到达取物点位 —— [左侧大屏切换至抓取视角特写]"),
        "3": ("fetch-complete", "抓取动作完成 —— [左侧恢复虚拟人，提示机器人正在返回]"),
        "4": ("arrive-210", "到达210点位 —— [提示正在将咖啡投放至筐中]"),
        "5": ("task-end", "任务彻底结束 —— [标准中划线：提示返航顺利完成，大屏退回待机]"),
        "6": ("task_end", "任务结束(下划线) —— [ROS标准下划线：用来测试后端洗净拦截功能]"),
        "7": ("door-near", "到达门口打卡点 (door-near) —— [触发大屏右侧进门/走廊视频切换]"),
        # ================= 新增的三个手动信号 =================
        "8": ("hand-up", "模拟老人举手 —— [触发虚拟人询问有什么需要帮忙的]"),
        "9": ("mock-wait-person", "手动模拟接到找人任务 —— [强制触发找人流程前端展示]"),
        "10": ("mock-fetch-object", "手动模拟接到寻物任务 —— [强制触发取咖啡/寻物流程前端展示]"),
        # =====================================================
    }

    while True:
        print("\n" + "-" * 55)
        print("请选择要手动发出的机器人状态信号：")
        for key, (event, desc) in menu.items():
            print(f"  [{key}] {desc}")
        print("  [c] 自定义输入发送 (测试任意报文)")
        print("  [q] 退出模拟器")
        print("-" * 55)
        
        try:
            choice = input("请输入选项并回车: ").strip()
        except KeyboardInterrupt:
            print("\n已退出模拟脚本。")
            sys.exit(0)
            
        if choice.lower() == 'q':
            print("模拟脚本已关闭。")
            break
        elif choice in menu:
            event_name = menu[choice][0]
            
            # 针对新增的找人/寻物任务模拟，顺便带上测试的 text 字段，方便大屏识别
            if event_name == "mock-wait-person":
                send_signal("command", {"text": "请帮我找一下徐静"})
            elif event_name == "mock-fetch-object":
                send_signal("command", {"text": "请帮我拿一杯咖啡"})
            else:
                send_signal(event_name)
                
        elif choice.lower() == 'c':
            custom_event = input("请输入你想任意自定义的事件名称: ").strip()
            if custom_event:
                send_signal(custom_event)
            else:
                print("⚠️ 输入为空，已取消自定义发送。")
        else:
            print("❌ 无效输入，请输入正确的菜单编号 (1-10) 或 q/c。")

if __name__ == "__main__":
    main()