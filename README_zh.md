# Agent 代理网关

[English](README.md) | [中文](README_zh.md)

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-green)](https://fastapi.tiangolo.com)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**位于 AI Agent 与 LLM / 工具 API 之间的透明代理网关** —— 拦截、追踪、护栏、评估并控制所有 Agent 流量。

## 架构

```
Agent (OpenAI / Anthropic SDK)
  │  HTTP 请求
  ▼
┌─────────────────────────────────────────────────────────┐
│                   FastAPI 网关                           │
│                                                          │
│  ┌────────────── 中间件链 ──────────────────────┐   │
│  │ 优先级 10: GuardrailsEngine                    │   │
│  │   ├── PII 检测（脱敏邮箱/手机/身份证）        │   │
│  │   ├── 注入检测（拦截攻击）                    │   │
│  │   └── 内容安全（拦截有害内容）                │   │
│  │                                                   │   │
│  │ 优先级 15: SlidingWindowRateLimiter            │   │
│  │   └── RPM / TPM 滑动窗口限流                  │   │
│  │                                                   │   │
│  │ 优先级 90: EvalPipeline                        │   │
│  │   ├── 响应长度 / 重复 / 延迟（启发式）         │   │
│  │   ├── 工具调用（启发式，同步）                 │   │
│  │   └── LLM 评审（相关性/安全性/连贯性）         │   │
│  └──────────────────────────────────────────────────┘   │
│                          ▼                               │
│  ┌────────────── 协议适配器 ──────────────────────┐   │
│  │  OpenAI 适配器  /  Anthropic 适配器             │   │
│  │  标准化 → 转发 → 标准化                        │   │
│  └──────────────────┬──────────────────────────────┘   │
│                     ▼                                    │
│  ┌────────────── 追踪引擎 ──────────────────────┐   │
│  │  trace_id / span_id / span 树 → SQLite          │   │
│  └──────────────────────────────────────────────────┘   │
└────────────────────┬────────────────────────────────────┘
                     ▼
           LLM / 工具后端 API
```

## 快速开始

### 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) 包管理器
- OpenAI API 密钥（可选 Anthropic）

### 安装与运行

```bash
# 克隆仓库
git clone <repo-url>
cd agent-gateway

# 安装依赖
uv sync --extra dev

# 设置 API 密钥（pydantic-settings 自动读取 .env）
export OPENAI_API_KEY=sk-your-key
# 或创建 .env 文件：
echo "OPENAI_API_KEY=sk-your-key" > .env

# 启动网关
uv run gateway
# → http://localhost:18080

# （可选）启动仪表盘
uv run streamlit run dashboard/app.py --server.port 8599
# → http://localhost:8599
```

### 测试验证

```bash
# 健康检查
curl http://localhost:18080/health

# 代理一个对话补全请求
curl -X POST http://localhost:18080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-key" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"你好！"}],"max_tokens":10}'

# 查看追踪记录
curl http://localhost:18080/api/traces

# 运行演示
uv run python scripts/demo.py
```

### 生成测试数据

```bash
uv run python scripts/seed_data.py --count 50
```

### Docker 部署

```bash
docker-compose up -d
# 网关: http://localhost:18080
# 仪表盘: http://localhost:8501
```

## 功能特性

| 功能 | 状态 | 描述 |
|------|------|------|
| **透明代理** | ✅ | Agent 只需修改 `base_url` —— 无需改动代码 |
| **多提供商** | ✅ | OpenAI + Anthropic 适配器，可扩展注册表 |
| **流式响应（SSE）** | ✅ | 逐块转发，护栏实时生效 |
| **追踪引擎** | ✅ | 完整请求生命周期追踪，span 树结构 |
| **PII 护栏** | ✅ | 邮箱、手机号、身份证、银行卡检测与脱敏 |
| **注入护栏** | ✅ | 提示词注入攻击检测与拦截 |
| **内容安全** | ✅ | 暴力、自残、违法内容过滤 |
| **速率限制** | ✅ | 按 Agent 和模型维度的 RPM/TPM 滑动窗口 |
| **Token 预算** | ✅ | 小时/日限额，80% 触发预警 |
| **熔断器** | ✅ | CLOSED → OPEN → HALF_OPEN 三态机 |
| **评估流水线** | ✅ | 4 项启发式评估 + 可选 LLM 评审 |
| **仪表盘** | ✅ | Streamlit UI：追踪、护栏、预算、评估 |
| **Docker** | ✅ | Dockerfile + docker-compose.yml |
| **策略热加载** | ✅ | YAML 配置变更自动生效 |

## 项目结构

```
agent-gateway/
├── config/
│   ├── default.yaml          # 网关 + 预算 + 限流 + 评估配置
│   └── guardrails.yaml       # 护栏规则配置
├── src/
│   ├── shared/               # Pydantic 模型、配置加载、日志
│   └── gateway/
│       ├── adapter/          # 协议适配器（OpenAI、Anthropic）
│       ├── api/              # 管理 API 端点
│       ├── budget/           # 速率限制器、Token 计数器、熔断器
│       ├── eval/             # 启发式评估、LLM 评审、评估流水线
│       ├── guardrails/       # PII、注入、内容安全规则
│       ├── policy/           # YAML 配置加载 + Pydantic 校验
│       ├── proxy/            # 代理引擎、SSE 拦截器、中间件链
│       ├── trace/            # 追踪引擎、SQLite 存储、span 树
│       └── main.py           # FastAPI 入口
├── dashboard/
│   ├── app.py                # Streamlit 仪表盘入口
│   └── pages/                # 概览、追踪、护栏、预算、评估
├── scripts/
│   ├── demo.py                   # 端到端演示
│   ├── gateway_overhead_bench.py # 网关开销基准测试
│   ├── seed_data.py              # 测试数据生成器
│   ├── test_pii_live.py          # PII 实时测试
├── tests/                        # 154 个测试
├── .github/workflows/ci.yml      # CI/CD（ruff + mypy + pytest + Docker）
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

## API 端点

### 代理端（面向 Agent）

| 方法 | 路径 | 描述 |
|------|------|------|
| POST | `/{path}` | 透明代理到上游 |

### 管理端

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/api/traces` | 最近追踪记录 |
| GET | `/api/traces/{id}` | 追踪详情 + span 树 |
| GET | `/api/traces/stats` | 追踪统计 |
| GET | `/api/guardrails/stats` | 护栏命中统计 |
| GET | `/api/guardrails/rules` | 活跃规则 |
| GET | `/api/budget/status` | Token 预算使用情况 |
| GET | `/api/eval/metrics` | 评估指标定义 |

## 配置说明

网关行为由 `config/` 下的 YAML 文件控制：

### 护栏配置（`config/guardrails.yaml`）

```yaml
guardrails:
  enabled: true
  rules:
    - id: "pii-detection"
      type: "pii"
      action: "redact"     # block | redact | log
      confidence_threshold: 0.7
      enabled: true

    - id: "injection-detection"
      type: "injection"
      action: "block"
      patterns: ["忽略之前的指令", "系统覆盖", ...]
      enabled: true

    - id: "content-safety"
      type: "content"
      action: "block"
      enabled: true
```

### 预算配置（`config/default.yaml`）

```yaml
budget:
  defaults:
    max_tokens_per_day: 1000000
    max_tokens_per_hour: 100000
    warning_threshold: 0.8

rate_limit:
  defaults:
    rpm: 60
    tpm: 100000

circuit_breaker:
  failure_threshold: 5
  recovery_timeout: 30
```

## 设计决策

1. **中间件链（而非过滤器链）**：双向流转 —— 中间件可同时拦截请求和响应，这对于需要同时检查输入和输出的护栏至关重要。

2. **MVP 阶段使用 SQLite**：零外部依赖，开箱即用。后续只需修改一个连接字符串即可迁移到 PostgreSQL。

3. **拦截 vs 脱敏 vs 记录**：
   - **拦截（Block）**：明确有害（注入、严重违规）
   - **脱敏（Redact）**：敏感但非致命（PII）—— 剥离 PII，其余内容放行
   - **记录（Log）**：可疑但不确定 —— 记录以便人工复核

4. **启发式 + LLM 评估**：启发式评估在每个请求上运行（零成本、确定性），LLM 评审在采样请求上异步执行（质量更高、非阻塞）。

5. **协议适配器使用 TypedDict**：每个适配器使用 TypedDict 来描述其提供商特有的 JSON 结构，提供 IDE 自动补全，并在开发期捕获字段拼写错误。

## 测试

```bash
# 运行全部测试（排除需要真实 API 密钥的集成测试）
uv run pytest tests/ -v

# 带覆盖率
uv run pytest tests/ --cov=src/gateway --cov-report=html

# 集成测试（需要 OPENAI_API_KEY）
uv run pytest tests/ -v -k integration
```

## 许可证

MIT
