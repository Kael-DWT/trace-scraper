# Trace Source Universal Extractor — 溯源数据通用提取平台

从农业种子二维码溯源网页中自动提取产品信息，输出标准化的 27 个字段。
覆盖 46 个不同溯源网站，无需为每个网站单独编写爬虫。

## 架构

Playwright(浏览器自动化) → 结构化提取(F004) → LLM 兜底(通义千问)

```
用户扫码 → 获取 URL → 匹配规则 / 自动学习
  ↓
Playwright 抓取页面(移动端 UA, JS 渲染, 跳转处理)
  ↓
结构化提取(table / dl / label / div 四种结构)
  ↓
缺关键字段 ? LLM 兜底(通义千问 DashScope) : 跳过
  ↓
27 字段标准化输出 → 缓存 / 日志 / 保存
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置 API Key

编辑 `.env` 文件，填入阿里百炼通义千问的 API Key：

```
DASHSCOPE_API_KEY=sk-xxxxx
```

LLM 仅在结构化提取缺 goods_name/company_name 时调用。

### 3. 运行服务

```bash
python run.py
```

API 服务启动在 `http://127.0.0.1:8000`

#### 查询接口 (F001)

```bash
curl -X POST http://127.0.0.1:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{"url":"http://ma.inb123.com/d/40120","trace_code":"40120"}'
```

#### 查看规则 (F002)

```bash
curl http://127.0.0.1:8000/api/rules
```

#### 查看日志

```bash
curl http://127.0.0.1:8000/api/logs/query
```

### 4. 批量提取

```bash
python batch_extract.py
```

对 `goods_qrcode_info.json` 中的 46 个域名去重后逐个提取，结果写入 SQLite 和 JSON。

## 输出字段

| 字段 | 说明 | 覆盖率 |
|------|------|--------|
| goods_code | md5(qrcode_url+goods_name+company_name) | 100% |
| goods_name | 商品名称 | 100% |
| company_name | 公司名称 | 100% |
| crop_category | 作物类别 | ~50% |
| unit_code | 单元识别代码 | ~62% |
| germination_rate | 发芽率 | ~33% |
| purity | 纯度 | ~36% |
| cleanliness | 净度 | ~36% |
| moisture | 水分 | ~33% |
| seed_category | 种子类别 | ~46% |
| license_number | 许可证编号 | ~41% |
| warranty_period | 质量保证期 | ~38% |
| mobile | 联系电话 | ~41% |
| reg_address | 注册地址 | ~36% |
| characteristics | 特征特性 | ~13% |
| risk_warning | 风险提示 | ~13% |
| cultivation_points | 栽培要点 | ~8% |
| + 10 个更多字段 | (brand/origin/sale_area 等) | |

trace_website 直接使用输入的 URL 地址。

## 项目文件

```
./
├── .env                 # API Key 配置
├── AGENT_PROMPT.md      # LLM 学习 Agent 系统提示词
├── PRD.md               # 产品需求文档
├── goods_qrcode_info.json  # 溯源网址样本(463条,46域名)
├── requirements.txt     # 依赖清单
├── run.py               # FastAPI 服务启动入口
├── batch_extract.py     # 批量提取入口
├── app/
│   ├── config.py        # 配置(路径/超时/LLM参数)
│   ├── database.py      # SQLite 会话
│   ├── models.py        # 数据模型(GoodsExtract/SiteRule/QueryLog)
│   ├── fetcher.py       # Playwright 页面抓取
│   ├── field_extractor.py  # 字段提取(Table/dl/label/div)
│   ├── alias.py         # 字段别名映射(26字段×同义词)
│   ├── learning_agent.py   # LLM 学习 Agent(F003)
│   ├── cache.py         # 结果缓存(F005,24h TTL)
│   ├── rule_engine.py   # 编排服务(抓取→学习→提取)
│   └── main.py          # FastAPI 接口(F001/F002)
└── data/
    ├── trace.db         # SQLite 数据库
    └── batch_results.json  # 批量提取结果
```