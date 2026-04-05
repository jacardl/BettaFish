"""
关键词优化中间件
使用Qwen AI将Agent生成的搜索词优化为更适合舆情数据库查询的关键词
"""

from openai import OpenAI
import json
import sys
import os
from typing import List, Dict, Any
from dataclasses import dataclass

# 添加项目根目录到Python路径以导入config
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from config import settings
from loguru import logger

# 添加utils目录到Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(os.path.dirname(current_dir))
utils_dir = os.path.join(root_dir, 'utils')
if utils_dir not in sys.path:
    sys.path.append(utils_dir)

from retry_helper import with_graceful_retry, SEARCH_API_RETRY_CONFIG

@dataclass
class KeywordOptimizationResponse:
    """关键词优化响应"""
    original_query: str
    optimized_keywords: List[str]
    reasoning: str
    success: bool
    error_message: str = ""

class KeywordOptimizer:
    """
    关键词优化器
    使用硅基流动的Qwen3模型将Agent生成的搜索词优化为更贴近真实舆情的关键词
    """
    
    def __init__(self, api_key: str = None, base_url: str = None, model_name: str = None):
        """
        初始化关键词优化器
        
        Args:
            api_key: 硅基流动API密钥，如果不提供则从配置文件读取
            base_url: 接口基础地址，默认使用配置文件提供的SiliconFlow地址
        """
        self.api_key = api_key or settings.KEYWORD_OPTIMIZER_API_KEY

        if not self.api_key:
            raise ValueError("未找到API密钥，请在config.py中设置KEYWORD_OPTIMIZER_API_KEY")

        self.base_url = base_url or settings.KEYWORD_OPTIMIZER_BASE_URL

        client_kwargs = {
            "api_key": self.api_key,
            "base_url": self.base_url
        }
        if self.base_url and "omnisaas.cn" in self.base_url:
            client_kwargs["default_headers"] = {"apikey": self.api_key}

        self.client = OpenAI(**client_kwargs)
        self.model = model_name or settings.KEYWORD_OPTIMIZER_MODEL_NAME
    
    def optimize_keywords(self, original_query: str, context: str = "") -> KeywordOptimizationResponse:
        """
        优化搜索关键词
        
        Args:
            original_query: Agent生成的原始搜索查询
            context: 额外的上下文信息（如段落标题、内容描述等）
            
        Returns:
            KeywordOptimizationResponse: 优化后的关键词列表
        """
        logger.info(f"🔍 关键词优化中间件: 处理查询 '{original_query}'")
        
        try:
            # 构建优化prompt
            system_prompt = self._build_system_prompt()
            user_prompt = self._build_user_prompt(original_query, context)
            
            # 调用Qwen API
            response = self._call_qwen_api(system_prompt, user_prompt)
            
            if response["success"]:
                # 解析响应
                content = response["content"]
                try:
                    # 尝试解析JSON格式的响应
                    if content.strip().startswith('{'):
                        parsed = json.loads(content)
                        keywords = parsed.get("keywords", [])
                        reasoning = parsed.get("reasoning", "")
                    else:
                        # 如果不是JSON格式，尝试从文本中提取关键词
                        keywords = self._extract_keywords_from_text(content)
                        reasoning = content
                    
                    # 验证关键词质量
                    validated_keywords = self._validate_keywords(keywords)
                    
                    logger.info(
                        f"✅ 优化成功: {len(validated_keywords)}个关键词" +
                        ("" if not validated_keywords else "\n" +
                         "\n".join([f"   {i}. '{k}'" for i, k in enumerate(validated_keywords, 1)]))
                    )
                        
                    
                    
                    return KeywordOptimizationResponse(
                        original_query=original_query,
                        optimized_keywords=validated_keywords,
                        reasoning=reasoning,
                        success=True
                    )
                
                except Exception as e:
                    logger.exception(f"⚠️ 解析响应失败，使用备用方案: {str(e)}")
                    # 备用方案：从原始查询中提取关键词
                    fallback_keywords = self._fallback_keyword_extraction(original_query)
                    return KeywordOptimizationResponse(
                        original_query=original_query,
                        optimized_keywords=fallback_keywords,
                        reasoning="API响应解析失败，使用备用关键词提取",
                        success=True
                    )
            else:
                logger.error(f"❌ API调用失败: {response['error']}")
                # 使用备用方案
                fallback_keywords = self._fallback_keyword_extraction(original_query)
                return KeywordOptimizationResponse(
                    original_query=original_query,
                    optimized_keywords=fallback_keywords,
                    reasoning="API调用失败，使用备用关键词提取",
                    success=True,
                    error_message=response['error']
                )
                
        except Exception as e:
            logger.error(f"❌ 关键词优化失败: {str(e)}")
            # 最终备用方案
            fallback_keywords = self._fallback_keyword_extraction(original_query)
            return KeywordOptimizationResponse(
                original_query=original_query,
                optimized_keywords=fallback_keywords,
                reasoning="系统错误，使用备用关键词提取",
                success=False,
                error_message=str(e)
            )
    
    def _build_system_prompt(self) -> str:
        """构建系统prompt"""
        return """你是一位专业的舆情数据挖掘与SEO/GEO关键词研究专家。你的任务是将用户提供的搜索查询优化为更适合在社交媒体舆情数据库和搜索引擎中查找的高价值关键词矩阵。

**核心优化策略（基于高级Keyword Research Skill）**：
1. **意图分类 (Intent Classification)**：分析查询背后的用户意图，生成覆盖不同维度的关键词：
   - 资讯类 (Informational)：疑问、科普、教程（如"怎么"、"是什么"、"原理解析"）
   - 商业/交易类 (Commercial/Transactional)：测评、对比、推荐、避雷（如"推荐"、"vs"、"踩雷"、"平替"、"多少钱"）
2. **关键词扩展与长尾变体 (Keyword Expansion & Long-tail)**：
   - 补充修饰词 (Modifiers)：如"最新"、"最全"、"真实评价"、"翻车"、"破防"等网民常用情绪修饰词。
   - 长尾变体 (Long-tail Variations)：针对特定受众或具体场景的表达（如"新手小白必看"、"大学生平替"、"平民玩家攻略"）。
3. **GEO（生成式引擎优化）相关性**：补充容易触发AI问答和深度讨论的结构化查询词，如"区别"、"优缺点"、"为什么"。

**基础约束原则**：
1. **贴近网民语言**：使用普通网友在社交媒体上会真实使用的词汇，接地气。
2. **避免专业术语**：坚决不使用"舆情管理"、"传播路径"、"情绪倾向"、"战略规划"等官方或学术词汇。
3. **数量控制**：最少提供10个，最多提供20个高质量关键词。
4. **格式严格**：每个关键词都必须是一个不可分割的独立词条，严禁在词条内部包含空格（如应使用"雷军班争议"而非"雷军班 争议"）。

**输出格式**：
请以JSON格式返回结果：
{
    "keywords": ["关键词1", "关键词2", "关键词3"],
    "reasoning": "说明关键词的意图分类覆盖情况、长尾扩展逻辑，以及为何选择这些词汇"
}"""

    def _build_user_prompt(self, original_query: str, context: str) -> str:
        """构建用户prompt"""
        prompt = f"请将以下搜索查询优化为适合舆情数据库查询的关键词：\n\n原始查询：{original_query}"
        
        if context:
            prompt += f"\n\n上下文信息：{context}"
        
        prompt += "\n\n请记住：要使用网民在社交媒体上真实使用的词汇，避免官方术语和专业词汇。"
        
        return prompt
    
    @with_graceful_retry(SEARCH_API_RETRY_CONFIG, default_return={"success": False, "error": "关键词优化服务暂时不可用"})
    def _call_qwen_api(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        """调用Qwen API"""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
            )

            if response.choices:
                content = response.choices[0].message.content
                return {"success": True, "content": content}
            else:
                return {"success": False, "error": "API返回格式异常"}
        except Exception as e:
            return {"success": False, "error": f"API调用异常: {str(e)}"}
    
    def _extract_keywords_from_text(self, text: str) -> List[str]:
        """从文本中提取关键词（当JSON解析失败时使用）"""
        # 简单的关键词提取逻辑
        lines = text.split('\n')
        keywords = []
        
        for line in lines:
            line = line.strip()
            # 查找可能的关键词
            if '：' in line or ':' in line:
                parts = line.split('：') if '：' in line else line.split(':')
                if len(parts) > 1:
                    potential_keywords = parts[1].strip()
                    # 尝试分割关键词
                    if '、' in potential_keywords:
                        keywords.extend([k.strip() for k in potential_keywords.split('、')])
                    elif ',' in potential_keywords:
                        keywords.extend([k.strip() for k in potential_keywords.split(',')])
                    else:
                        keywords.append(potential_keywords)
        
        # 如果没有找到，尝试其他方法
        if not keywords:
            # 查找引号中的内容
            import re
            quoted_content = re.findall(r'["""\'](.*?)["""\']', text)
            keywords.extend(quoted_content)
        
        # 清理和验证关键词
        cleaned_keywords = []
        for keyword in keywords[:20]:  # 最多20个
            keyword = keyword.strip().strip('"\'""''')
            if keyword and len(keyword) <= 20:  # 合理长度
                cleaned_keywords.append(keyword)
        
        return cleaned_keywords[:20]
    
    def _validate_keywords(self, keywords: List[str]) -> List[str]:
        """验证和清理关键词"""
        validated = []
        
        # 不良关键词（过于专业或官方）
        bad_keywords = {
            '态度分析', '公众反应', '情绪倾向',
            '未来展望', '发展趋势', '战略规划', '政策导向', '管理机制'
        }
        
        for keyword in keywords:
            if isinstance(keyword, str):
                keyword = keyword.strip().strip('"\'""''')
                
                # 基本验证
                if (keyword and 
                    len(keyword) <= 20 and 
                    len(keyword) >= 1 and
                    not any(bad_word in keyword for bad_word in bad_keywords)):
                    validated.append(keyword)
        
        return validated[:20]  # 最多返回20个关键词
    
    def _fallback_keyword_extraction(self, original_query: str) -> List[str]:
        """备用关键词提取方案"""
        # 简单的关键词提取逻辑
        # 移除常见的无用词汇
        stop_words = {'、'}
        
        # 分割查询
        import re
        # 按空格、标点分割
        tokens = re.split(r'[\s，。！？；：、]+', original_query)
        
        keywords = []
        for token in tokens:
            token = token.strip()
            if token and token not in stop_words and len(token) >= 2:
                keywords.append(token)
        
        # 如果没有有效关键词，使用原始查询的第一个词
        if not keywords:
            first_word = original_query.split()[0] if original_query.split() else original_query
            keywords = [first_word] if first_word else ["热门"]
        
        return keywords[:20]

# 全局实例
keyword_optimizer = KeywordOptimizer()
