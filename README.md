# Trace Source Universal Extractor — 溯源数据通用提取平台

从农业种子二维码溯源网页中自动提取产品信息，输出标准化的 27 个字段。
覆盖 46 个不同溯源网站，无需为每个网站单独编写爬虫。

## 架构

```
LLM 导航学习(首次) → 规则持久化 → Playwright 按规则导航
  ↓
单次浏览器会话: 填表单 → 点详情页 → 等待容器
  ↓
结构化提取(table / dl / label / div) → LLM 字段兜底
  ↓
27 字段输出 + goods_code(md5)
```

**关键设计:**
- Playwright 浏览器常驻: FastAPI 启动时初始化，所有请求复用
- 单次会话: 每次查询只启动一次浏览器上下文，完成全部导航（摘要页→表单→详情页）
- LLM 只学导航规则（AGENT_PROMPT.md），不参与字段提取
- 规则持久化到 data/rules.json，服务重启后复用

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置 API Key

编辑 .env：
```
DASHSCOPE_API_KEY=sk-xxxxx
```
LLM 仅在首次访问域名时学习导航规则，结构化提取缺关键字段时兜底。

### 3. 启动服务

```bash
python run.py
```

API 运行在 http://127.0.0.1:8000

### 4. 查询

```bash
curl -X POST http://127.0.0.1:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{"url":"http://ma.inb123.com/d/40120"}'
```

响应结构:
```json
{
  "success": true,
  "data": {
    "source": "live",
    "fields": {
      "goods_code": "f452c411...",
      "goods_name": "20gBM800甜玉米",
      "trace_website": "http://ma.inb123.com/d/40120"
    },
    "elapsed_ms": 3500
  }
}
```

所有 27 个字段始终输出，缺失字段为空字符串。

## 性能

| 模式 | 耗时 | 说明 |
|------|------|------|
| 首次域名 | ~6s | LLM 学习导航规则 + 全流程提取 |
| 后续同域名 | ~3.5s | 复用规则 + 共享浏览器，无 LLM |
| 缓存命中 | <1ms | 同一 URL 24h 内重复查询 |

## 输出字段（27个）

| 字段 | 说明 |
|------|------|
| goods_code | md5(qrcode_url + goods_name + company_name) |
| goods_name | 商品名称 |
| company_name | 公司名称（生产经营者名称） |
| crop_category | 作物类别 |
| mobile | 联系电话 |
| reg_address | 注册地址 |
| sale_area | 销售区域 |
| unit_code | 单元识别代码 |
| batch_number | 批次 |
| brand | 品牌 |
| query_count | 查询次数 |
| germination_rate | 发芽率 |
| purity | 纯度 |
| cleanliness | 净度 |
| moisture | 水分 |
| seed_category | 种子类别 |
| origin | 产地 |
| test_date | 检测日期 |
| warranty_period | 质量保证期 |
| consult_service | 咨询服务信息 |
| supplier | 供应商 |
| characteristics | 特征特性 |
| cultivation_points | 栽培要点 |
| risk_warning | 风险提示 |
| trace_website | 追溯网站（输入 URL） |
| license_number | 许可证编号 |
| planting_season | 种植季节 |

## 项目文件

```
./
├── .env                     # DASHSCOPE_API_KEY
├── AGENT_PROMPT.md          # LLM 导航学习提示词
├── PRD.md                   # 产品需求文档
├── README.md                # 本文件
├── goods_qrcode_info.json   # 463条样本数据
├── requirements.txt         # Python 依赖
├── run.py                   # FastAPI 启动入口
├── batch_extract.py         # 批量提取脚本
├── app/
│   ├── config.py            # 全局配置
│   ├── fetcher.py           # 页面抓取（共享浏览器）
│   ├── field_extractor.py   # 字段提取（4种结构）
│   ├── alias.py             # 字段别名映射（26字段 x 同义词）
│   ├── learning_agent.py    # LLM 导航学习 + 字段兜底
│   ├── rule_store.py        # 导航规则持久化（JSON）
│   ├── cache.py             # 结果缓存（内存，24h TTL）
│   ├── rule_engine.py       # 编排：学规则→导航→提取→缓存
│   └── main.py              # FastAPI 接口（F001）
└── data/
    └── rules.json            # 已学习的导航规则（自动生成）
```
