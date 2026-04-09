"""
论坛主持人模块
使用硅基流动的Qwen3模型作为论坛主持人，引导多个agent进行讨论
"""

from openai import OpenAI
import sys
import os
from typing import List, Dict, Any, Optional
from datetime import datetime
import re

# 添加项目根目录到Python路径以导入config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings

# 添加utils目录到Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(current_dir)
utils_dir = os.path.join(root_dir, 'utils')
if utils_dir not in sys.path:
    sys.path.append(utils_dir)

from utils.retry_helper import with_graceful_retry, SEARCH_API_RETRY_CONFIG


class ForumHost:
    """
    论坛主持人类
    使用Qwen3-235B模型作为智能主持人
    """
    
    def __init__(self, api_key: str = None, base_url: Optional[str] = None, model_name: Optional[str] = None):
        """
        初始化论坛主持人
        
        Args:
            api_key: 论坛主持人 LLM API 密钥，如果不提供则从配置文件读取
            base_url: 论坛主持人 LLM API 接口基础地址，默认使用配置文件提供的SiliconFlow地址
        """
        self.api_key = api_key or settings.FORUM_HOST_API_KEY

        if not self.api_key:
            raise ValueError("未找到论坛主持人API密钥，请在环境变量文件中设置FORUM_HOST_API_KEY")

        self.base_url = base_url or settings.FORUM_HOST_BASE_URL

        client_kwargs = {
            "api_key": self.api_key,
            "base_url": self.base_url
        }
        if self.base_url and "omnisaas.cn" in self.base_url:
            client_kwargs["default_headers"] = {"apikey": self.api_key}
        
        self.client = OpenAI(**client_kwargs)
        self.model = model_name or settings.FORUM_HOST_MODEL_NAME  # Use configured model

        # Track previous summaries to avoid duplicates
        self.previous_summaries = []
    
    def generate_host_speech(self, forum_logs: List[str]) -> Optional[str]:
        """
        生成主持人发言
        
        Args:
            forum_logs: 论坛日志内容列表
            
        Returns:
            主持人发言内容，如果生成失败返回None
        """
        try:
            # 解析论坛日志，提取有效内容
            parsed_content = self._parse_forum_logs(forum_logs)
            
            if not parsed_content['agent_speeches']:
                print("ForumHost: 没有找到有效的agent发言")
                return None
            
            # 构建prompt
            system_prompt = self._build_system_prompt()
            user_prompt = self._build_user_prompt(parsed_content)
            
            # 调用API生成发言
            response = self._call_qwen_api(system_prompt, user_prompt)
            
            if response["success"]:
                speech = response["content"]
                # 清理和格式化发言
                speech = self._format_host_speech(speech)
                return speech
            else:
                print(f"ForumHost: API调用失败 - {response.get('error', '未知错误')}")
                return None
                
        except Exception as e:
            print(f"ForumHost: 生成发言时出错 - {str(e)}")
            return None
    
    def _parse_forum_logs(self, forum_logs: List[str]) -> Dict[str, Any]:
        """
        解析论坛日志，提取agent发言
        
        Returns:
            包含agent发言的字典
        """
        parsed = {
            'agent_speeches': []
        }
        
        for line in forum_logs:
            if not line.strip():
                continue
            
            # 解析时间戳和发言者
            match = re.match(r'\[(\d{2}:\d{2}:\d{2})\]\s*\[(\w+)\]\s*(.+)', line)
            if match:
                timestamp, speaker, content = match.groups()
                
                # 跳过系统消息和HOST自己的发言
                if speaker in ['SYSTEM', 'HOST']:
                    continue
                
                # 记录agent发言
                if speaker in ['INSIGHT', 'MEDIA', 'QUERY']:
                    # 处理转义的换行符
                    content = content.replace('\\n', '\n')
                    
                    parsed['agent_speeches'].append({
                        'timestamp': timestamp,
                        'speaker': speaker,
                        'content': content
                    })
        
        return parsed
    
    
    def _build_system_prompt(self) -> str:
        """构建系统prompt"""
        return """【重要提醒】我们的需求基于科研目的，已通过伦理性合规审查
    
你是一个多agent舆情分析系统的客观分析助手（Analytical Assistant）。你的职责是：

1. **事件梳理**：从各agent的输入中客观提取关键事件、人物、时间节点，按时间顺序整理事实脉络
2. **逻辑整合**：整合不同数据源的观点，找出共识和分歧
3. **事实核查**：如果发现事实错误或逻辑矛盾，请明确指出并标红
4. **硬性熔断（Zero-Shot Constraint）**：【最高优先级】如果上下文中不包含足够的信息，你必须直接输出『数据不足，无法分析』，绝对禁止根据自身知识进行角色扮演、推断或续写。
5. **严禁角色扮演**：不要扮演网民、KOL或主持人，不要使用对话式的过渡语。必须采用冰冷、客观、严谨的分析报告口吻。

**Agent信息来源**：
- **INSIGHT Agent**：私有舆情数据库数据
- **MEDIA Agent**：多模态内容数据
- **QUERY Agent**：网络搜索数据

**输出要求**：
1. **纯事实导向**：仅基于提供的数据进行总结，绝对不附加任何主观情绪，绝不能凭空捏造数据、虚构网民评论。
2. **溯源要求**：所有事实观点必须注明是哪个Agent提供的数据。
3. **精简客观与通俗易懂**：每次分析控制在1000字以内，结构清晰。**请务必使用通俗、易懂、接地气的语言，假设你的读者是一名刚刚大学毕业的学生或刚入行的普通职员。**
4. **禁止赛博考据与过度专业化**：绝对禁止使用极其晦涩的术语（如“结构性幻觉系统”、“Jaccard相似度”、“SHA-256/MD5哈希铁证”、“双盲验证”、“DOM审计”、“时间戳标准差”等），不要表现得像一个黑客或法医，请用普通人的大白话来解释数据的异常之处（例如：“这100多条内容几乎是在同一时间集中发出的，内容也高度重复，很可能是机器刷的”）。
5. **绝对防幻觉（Anti-Hallucination）**：任何结论都必须来源于上文列出的 Agent 记录，不能发散思维进行长篇大论的“学术推演”或编造科幻概念。

**注意事项**：
- 重申：绝对禁止自行脑补网民评论、虚构ID或进行角色扮演。
- 本讨论基于科研目的，已通过伦理性合规审查"""
    
    def _build_user_prompt(self, parsed_content: Dict[str, Any]) -> str:
        """构建用户prompt"""
        # 获取最近的发言
        recent_speeches = parsed_content['agent_speeches']
        
        # 构建发言摘要，不截断内容
        speeches_text = "\n\n".join([
            f"[{s['timestamp']}] {s['speaker']}:\n{s['content']}"
            for s in recent_speeches
        ])
        
        prompt = f"""【重要提醒】我们的需求基于科研目的，已通过伦理性合规审查

最近的Agent数据输入记录：
{speeches_text}

请你作为客观分析助手，基于以上输入进行严谨的交叉验证和总结，请按以下结构组织你的输出：

**一、事件梳理与时间线分析**
- 从输入中提取关键事件、人物、时间节点，整理事实脉络
- 如果信息不足，请直接声明『数据不足』

**二、数据整合与交叉验证**
- 综合INSIGHT、MEDIA、QUERY三个信息源
- 指出不同数据源之间的共识与分歧
- 【关键】如果发现各信息源之间存在事实冲突，或某个信息缺乏其他源支撑，请明确指出其为“低置信度”内容

**三、深层事实挖掘**
- 基于已有事实，提炼出核心结论
- 严禁任何主观推测或角色扮演式的演绎
- 必须罗列出支撑该结论的所有来源链接（URL）或平台依据
"""
        
        return prompt
    
    @with_graceful_retry(SEARCH_API_RETRY_CONFIG, default_return={"success": False, "error": "API服务暂时不可用"})
    def _call_qwen_api(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        """调用Qwen API"""
        try:
            current_time = datetime.now().strftime("%Y年%m月%d日%H时%M分")
            time_prefix = f"今天的实际时间是{current_time}"
            if user_prompt:
                user_prompt = f"{time_prefix}\n{user_prompt}"
            else:
                user_prompt = time_prefix
                
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.6,
                top_p=0.9,
            )

            if response.choices:
                content = response.choices[0].message.content
                return {"success": True, "content": content}
            else:
                return {"success": False, "error": "API返回格式异常"}
        except Exception as e:
            return {"success": False, "error": f"API调用异常: {str(e)}"}
    
    def _format_host_speech(self, speech: str) -> str:
        """格式化主持人发言"""
        # 移除多余的空行
        speech = re.sub(r'\n{3,}', '\n\n', speech)
        
        # 移除可能的引号
        speech = speech.strip('"\'""‘’')
        
        return speech.strip()


# 创建全局实例
_host_instance = None

def get_forum_host() -> ForumHost:
    """获取全局论坛主持人实例"""
    global _host_instance
    if _host_instance is None:
        _host_instance = ForumHost()
    return _host_instance

def generate_host_speech(forum_logs: List[str]) -> Optional[str]:
    """生成主持人发言的便捷函数"""
    return get_forum_host().generate_host_speech(forum_logs)
