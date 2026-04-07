import requests
import time

base_url = 'http://localhost:8000'

print("1. 获取历史任务...")
res = requests.get(f"{base_url}/api/report/history")
history = res.json().get('history', [])
if not history:
    print("没有找到历史任务，无法进行测试")
    exit()

task = history[0]
task_id = task['task_id']
print(f"使用任务: {task_id}")

print("2. 触发状态查询...")
res = requests.get(f"{base_url}/api/report/status?task_id={task_id}")
status = res.json()
print("Status Response:", status)

print("3. 测试取消功能...")
res = requests.post(f"{base_url}/api/report/cancel/{task_id}")
print("Cancel Response:", res.json())

print("测试完成。")