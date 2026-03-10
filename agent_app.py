import streamlit as st
import sys
import json
import os
import datetime
import concurrent.futures
import platform
import difflib
from typing import List

# ================= 0. 核心库引用 =================
from pydantic import BaseModel, Field
from langchain_text_splitters import RecursiveCharacterTextSplitter
from openai import OpenAI  
from exa_py import Exa 

# ================= 1. 核心网络配置 =================
if platform.system() == "Windows":
    os.environ["http_proxy"] = "http://127.0.0.1:7890"
    os.environ["https_proxy"] = "http://127.0.0.1:7890"
else:
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)

# ================= 2. 文档排版库引用 =================
from docx import Document
from docx.shared import RGBColor, Pt
from docx.oxml.ns import qn
from docx.enum.text import WD_ALIGN_PARAGRAPH

st.set_page_config(page_title="DeepSeek 科技探员", page_icon="🐳", layout="wide")

# ================= 3. 定义结构化数据 =================
class NewsItem(BaseModel):
    title: str = Field(description="新闻标题（务必翻译为中文）")
    source: str = Field(description="来源媒体（保留原名）")
    date_check: str = Field(description="新闻发生的真实日期，格式 YYYY-MM-DD。")
    summary: str = Field(description="约300字的深度商业分析。必须严格分段并带有标识：【事件核心】、【深度细节/数据支撑】、【行业深远影响】。")
    importance: int = Field(description="重要性 1-5")

class NewsReport(BaseModel):
    news: List[NewsItem] = Field(description="新闻列表")

# ================= 4. 内置 DeepSeek 驱动 =================
class EnterpriseDeepSeekDriver:
    def __init__(self, api_key, model_id):
        self.valid = False
        if not api_key: return
        try:
            self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            self.model_id = model_id
            self.valid = True
        except Exception:
            pass

    def analyze_structural(self, prompt, structure_class):
        if not self.valid: return None
        schema_str = json.dumps(structure_class.model_json_schema(), ensure_ascii=False)
        sys_prompt = f"你是顶级商业情报分析师。必须严格按此 JSON Schema 返回数据，不带任何废话：\n{schema_str}"
        try:
            response = self.client.chat.completions.create(
                model=self.model_id,
                messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1, 
                max_tokens=4096 
            )
            raw_text = response.choices[0].message.content.strip()
            try:
                json_obj = json.loads(raw_text)
                if isinstance(json_obj, list): json_obj = {"news": json_obj}
                return structure_class(**json_obj)
            except Exception: return None
        except Exception: return None

# ================= 5. 核心业务函数 =================

def search_and_extract_with_exa(query, sites_text, time_opt, exa_key, max_results=10):
    if not exa_key: return "", 0, []
    
    exa = Exa(api_key=exa_key)
    sites = [s.strip() for s in sites_text.split('\n') if s.strip()]
    
    # 🔴 Exa 最佳实践 1：长且语义丰富的 Query
    search_query = f"In-depth news article, strategic analysis, or official announcement regarding {query}"
    
    # 🔴 Exa 最佳实践 2：底层核心参数配置
    search_args = {
        "query": search_query,
        "type": "auto", 
        "num_results": max_results,
        "category": "news",  # ⚡ 锁定专属新闻索引库，精准度飙升
        "contents": {
            "highlights": {  # ⚡ 放弃冗长全文，只提取含金量最高的片段，省 Token 10倍！
                "max_characters": 4000
            }
        }
    }
    
    if sites:
        search_args["include_domains"] = sites

    # 🔴 Exa 最佳实践 3：强制爬虫新鲜度 (max_age_hours + publish_date)
    if time_opt == "d":
        search_args["max_age_hours"] = 24
        search_args["start_published_date"] = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    elif time_opt == "w":
        search_args["max_age_hours"] = 168
        search_args["start_published_date"] = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    elif time_opt == "m":
        search_args["max_age_hours"] = 720
        search_args["start_published_date"] = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    try:
        response = exa.search(**search_args)
        
        full_content = ""
        valid_count = 0
        links = []
        
        for result in response.results:
            content_text = ""
            # 优先提取 Exa 智能计算出的精华片段 (Highlights)
            if hasattr(result, 'highlights') and result.highlights:
                content_text = "\n...\n".join(result.highlights)
            # 保底方案
            elif hasattr(result, 'text') and result.text:
                content_text = result.text[:4000]

            if content_text and len(content_text) > 50:
                valid_count += 1
                links.append({'href': result.url})
                full_content += f"\n\n=== SOURCE START: {result.url} ===\n{content_text}\n=== SOURCE END ===\n"
                
        return full_content, valid_count, links
    except Exception as e:
        st.error(f"🚨 Exa 接口报错: {str(e)}")
        return "", 0, []

def map_reduce_analysis(ai_driver, topic, full_text, current_date, time_opt):
    if not full_text or len(full_text) < 50: return []
    docs = RecursiveCharacterTextSplitter(chunk_size=8000, chunk_overlap=1000).create_documents([full_text])
    all_extracted_news = []

    def process_single_doc(doc):
        # 🔴 减负：由于 Exa 已在物理层过滤了旧闻和垃圾信息，DeepSeek 不再需要承担繁重的清洗任务
        map_prompt = f"""
        【时间锚点】：今天是 **{current_date}**。
        任务：从以下经过 Exa AI 精选的片段中，提取关于【{topic}】的核心商业情报。
        要求：【{topic}】必须是事件的绝对主角。无符合条件内容请返回空列表。
        文本：{doc.page_content}
        """
        return ai_driver.analyze_structural(map_prompt, NewsReport)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        for future in concurrent.futures.as_completed([executor.submit(process_single_doc, d) for d in docs]):
            res = future.result()
            if res and res.news: all_extracted_news.extend(res.news)

    if not all_extracted_news: return []
    combined_json = json.dumps([item.model_dump() for item in all_extracted_news], ensure_ascii=False)

    reduce_prompt = f"""
        【时间锚点】：今天是 **{current_date}**。
        你是极其严苛的科技媒体总编。
        任务：
        1. 合并去重：报道同一事件的新闻必须合并。
        2. 深度扩写与高级排版：将每条新闻的 summary 扩展至 300 字左右。必须在 summary 中使用明显的分段和换行，明确包含以下三个部分：
           【事件核心】：概括事件
           【深度细节】：核心数据与细节支撑
           【行业影响】：精简的行业深远影响
        3. 按重要性降序，最多保留最核心的 5 条。
        数据：{combined_json}
    """
    final_report = ai_driver.analyze_structural(reduce_prompt, NewsReport)
    return final_report.news if final_report else []

def generate_word(data, filename, model_name):
    doc = Document()
    normal_style = doc.styles['Normal']
    normal_style.font.name = '微软雅黑'
    normal_style._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
    normal_style.font.size = Pt(10.5) 
    
    for i in range(1, 4):
        h_style = doc.styles[f'Heading {i}']
        h_style.font.name = '微软雅黑'
        h_style._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
        if i == 1:
            h_style.font.color.rgb = RGBColor(0, 51, 102)

    title = doc.add_heading("DeepSeek 企业级深度科技研报", 0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    meta_p = doc.add_paragraph()
    meta_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta_run = meta_p.add_run(f"生成日期: {datetime.date.today()}  |  数据来源: Exa Neural Search (News Index)  |  分析模型: {model_name}")
    meta_run.font.color.rgb = RGBColor(128, 128, 128)
    meta_run.font.size = Pt(9)
    
    doc.add_paragraph("━" * 50).alignment = WD_ALIGN_PARAGRAPH.CENTER

    for section in data:
        doc.add_heading(f"🔷 专题：{section['topic']}", level=1)
        if not section['data']:
            doc.add_paragraph("    在指定时间范围内，未发现重大核心情报。").font.italic = True
            continue
            
        for news in section['data']:
            doc.add_heading(f"🔹 {news.title}", level=2)
            
            p_info = doc.add_paragraph()
            run_info = p_info.add_run(f"    📌 来源: {news.source}    |    🕒 时间: {news.date_check}    |    🔥 价值: {'⭐'*news.importance}")
            run_info.font.color.rgb = RGBColor(100, 100, 100)
            run_info.font.bold = True
            
            p_summary = doc.add_paragraph(news.summary)
            p_summary.paragraph_format.line_spacing = 1.5 
            p_summary.paragraph_format.first_line_indent = Pt(21) 
            
            divider = doc.add_paragraph("┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
            divider.alignment = WD_ALIGN_PARAGRAPH.CENTER
            divider.runs[0].font.color.rgb = RGBColor(200, 200, 200)
    
    path = f"{filename}.docx"
    doc.save(path)
    return path

# ================= 6. 主界面 =================
with st.sidebar:
    st.header("🐳 DeepSeek 控制台")
    api_key = st.text_input("DeepSeek API Key", type="password")
    exa_key = st.text_input("Exa AI API Key (必填)", type="password", help="去 dashboard.exa.ai 免费获取。")
    
    model_id = st.selectbox("模型", ["deepseek-chat"], index=0)
    st.divider()
    
    time_opt = st.selectbox("时间范围（绝对严控）", ["过去 24 小时", "过去 1 周", "过去 1 个月", "不限时间"], index=0)
    time_limit_dict = {"过去 24 小时": "d", "过去 1 周": "w", "过去 1 个月": "m", "不限时间": None}
    
    st.markdown("**🌐 情报雷达范围**")
    sites = st.text_area(
        "定向搜索源 (留空则开启 Exa 全网新闻搜索)", 
        value="", 
        height=100, 
        help="留空：Exa 将自动在全球新闻索引库中搜寻。\n定向：如果只想看特定媒体，可输入域名，每行一个。"
    )
    file_name = st.text_input("文件名", f"深度研报_{datetime.date.today()}")

st.title("🐳 企业情报探员 (Exa 最佳实践最终版)")
query_input = st.text_input("输入主题 (用 \\ 隔开)", "Tesla Robotaxi \\ Apple Vision Pro")
btn = st.button("🚀 开始生成研报", type="primary")

if btn:
    if not api_key or not exa_key:
        st.error("❌ 请先在左侧边栏填入 DeepSeek 和 Exa 的 API Key！")
    elif not query_input:
        st.warning("请输入关键词！")
    else:
        topics = [t.strip() for t in query_input.split('\\') if t.strip()]
        all_data = []
        ai = EnterpriseDeepSeekDriver(api_key, model_id)
        current_date_str = datetime.date.today().strftime("%Y年%m月%d日")
        
        global_seen_titles = []

        st.info("🚀 探员已出击，Exa 神经元网络正在提取全球情报...")

        for topic in topics:
            st.markdown(f"#### 🔵 追踪目标: 【{topic}】 (要求: {time_opt})")
            
            with st.spinner(f"正在启用 Exa Highlights 极速摘要模式..."):
                full_text_data, valid_count, links = search_and_extract_with_exa(topic, sites, time_limit_dict[time_opt], exa_key)
            
            if not full_text_data: 
                st.warning(f"⚠️ {topic}：未搜寻到任何有效新闻。说明目标近期很安静。")
                continue
                
            st.write(f"🔍 成功获取 {valid_count} 个高价值网页的核心精华片段。DeepSeek 开始分析...")

            with st.spinner("AI 正在提炼商业本质与深度细节..."):
                final_news_list = map_reduce_analysis(ai, topic, full_text_data, current_date_str, time_opt)
            
            if final_news_list:
                deduped_news = []
                for news in final_news_list:
                    is_duplicate = False
                    for seen_title in global_seen_titles:
                        similarity = difflib.SequenceMatcher(None, news.title, seen_title).ratio()
                        if similarity > 0.6:
                            is_duplicate = True
                            break
                    
                    if not is_duplicate:
                        deduped_news.append(news)
                        global_seen_titles.append(news.title)
                
                if deduped_news:
                    all_data.append({"topic": topic, "data": deduped_news})
                    filtered_count = len(final_news_list) - len(deduped_news)
                    st.success(f"✅ 【{topic}】分析完毕！已锁定 {len(deduped_news)} 条新鲜情报。" + 
                               (f"(已跨主题去重过滤 {filtered_count} 条重复事件)" if filtered_count > 0 else ""))
                else:
                    st.warning(f"⚠️ 【{topic}】提炼出的新闻均与之前的主题高度重合，已执行全局去重抹杀！")
                    
            else:
                st.warning(f"⚠️ 【{topic}】片段经审核后未达到核心情报标准。")
            
            st.divider()

        if all_data:
            path = generate_word(all_data, file_name, model_id)
            st.balloons()
            st.success("🎉 全链条任务执行完毕！")
            with open(path, "rb") as f:
                st.download_button("📥 立即下载精美排版研报 (Word)", f, file_name=path, type="primary")
        else:
            st.error(f"❌ 任务结束。未产生独立且有效的大事件情报。")
