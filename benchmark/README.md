# Agent Proxy Gateway — 压测规划书

> **版本**: v1.0  
> **日期**: 2025-06-11  
> **目标**: 为面试提供真实、可信、可复现的性能数据  
> **原则**: 测网关本身，不测上游 LLM；控制变量，隔离噪声

---

## 一、目标与范围

### 1.1 我们要做什么（In Scope）

| 编号 | 目标 | 产出 | 面试价值 |
|------|------|------|---------|
| T1 | **单请求基线延迟** | P50/P95/P99 @ 1并发 | 回答"网关本身有多快" |
| T2 | **并发吞吐量曲线** | QPS @ 10/50/100/200并发 | 回答"能扛多少并发" |
| T3 | **流式 TTFT 基准** | 首token延迟 P50/P95 | 回答"流式用户体验" |
| T4 | **SQLite 写入瓶颈定位** | 不同并发下的 finish_span 延迟 | 回答"什么时候该迁移到 PostgreSQL" |
| T5 | **内存占用趋势** | RSS @ 不同并发 | 回答"资源消耗是否合理" |
| T6 | **各模块延迟分解** | Adapter / Middleware / Trace 各自耗时 | 回答"瓶颈在哪个模块" |

### 1.2 我们不要做（Out of Scope）

| 编号 | 排除项 | 原因 |
|------|--------|------|
| X1 | **不测试真实 LLM API** | 网络延迟不可控，会掩盖网关自身性能 |
| X2 | **不做长时间稳定性测试** | 面试场景不需要 7x24 稳定性数据，聚焦单次请求性能 |
| X3 | **不测试 Dashboard 性能** | Streamlit 是 MVP 工具，非核心考察点 |
| X4 | **不做分布式部署测试** | 当前架构是单实例，分布式是"演进方向"而非"当前状态" |
| X5 | **不测试 Presidio NLP 性能** | Presidio 是可选依赖，当前未启用（ImportError 降级） |
| X6 | **不做极限压力测试（>500并发）** | 超出 SQLite 合理负载范围，数据无实际参考价值 |

### 1.3 成功标准

- [ ] 每个测试配置至少运行 3 次，取中位数
- [ ] 所有数据保存到 `benchmark/results/` 目录，附带时间戳和硬件信息
- [ ] 产出一份 `BENCHMARK_REPORT.md`，包含数据表格、瓶颈分析、优化建议
- [ ] 面试文档中的估算数字全部替换为实测值

---

## 二、实验设计

### 2.1 测试环境

```
┌─────────────────┐     ┌─────────────────────┐     ┌─────────────────┐
│  Benchmark      │────→│  Agent Proxy        │────→│  Mock LLM       │
│  Client         │     │  Gateway (Port 18080)│     │  Server (Port 18081)│
│  (本机)         │     │  (本机)              │     │  (本机)          │
└─────────────────┘     └─────────────────────┘     └─────────────────┘
```

**硬件要求**（记录到报告中）：
- CPU: _____________
- 内存: _____________
- 操作系统: _____________
- Python 版本: _____________

**软件配置**：
- Gateway: `uv run gateway`（端口 18080）
- Mock LLM: `python benchmark/scripts/mock_llm_server.py`（端口 18081）
- 配置文件: `config/default.yaml` 中 `openai.base_url` 指向 `http://127.0.0.1:18081`

### 2.2 变量控制

| 变量 | 控制值 | 说明 |
|------|--------|------|
| 请求体大小 | 固定 15 prompt tokens + 10 completion tokens | 模拟单轮短对话 |
| 响应体大小 | 固定 10 completion tokens | Mock server 返回固定 JSON |
| 上游延迟 | 1-3ms（Mock server 模拟） | 模拟 fast LLM API |
| 连接池 | httpx.AsyncClient, http2=True | 复用连接，减少握手开销 |
| 预热 | 每个并发级别先跑 5 次 | 建立连接池，排除冷启动 |

### 2.3 实验矩阵

#### 实验 A：非流式延迟基准（单请求 → 高并发）

| 实验编号 | 并发数 | 总请求数 | 目标 |
|---------|--------|---------|------|
| A1 | 1 | 50 | 单请求基线（无锁竞争） |
| A2 | 10 | 100 | 轻并发（日常负载） |
| A3 | 50 | 200 | 中并发（压力开始显现） |
| A4 | 100 | 300 | 高并发（SQLite 锁竞争） |
| A5 | 200 | 400 | 极限并发（观察错误率） |

**指标**: P50/P95/P99 延迟、QPS、错误数、错误率

#### 实验 B：流式 TTFT 基准

| 实验编号 | 请求数 | 流式 chunk 数 | chunk 间隔 |
|---------|--------|--------------|-----------|
| B1 | 20 | 5 chunks | 5ms |
| B2 | 20 | 10 chunks | 5ms |

**指标**: TTFT P50/P95、总流式时间 P50/P95

#### 实验 C：模块延迟分解（插桩测试）

| 实验编号 | 方法 | 目标 |
|---------|------|------|
| C1 | 在 ProxyEngine.handle_request() 中插入 time.perf_counter() | 分解 Adapter / Middleware / Forward / Response 各阶段耗时 |
| C2 | 在 TraceEngine.finish_span() 中插桩 | 分解序列化 / 分级存储 / 费用计算 / SQLite UPDATE 各阶段耗时 |

**指标**: 各阶段 P50 延迟占比

#### 实验 D：SQLite 专项测试

| 实验编号 | 方法 | 目标 |
|---------|------|------|
| D1 | 对比 WAL 模式 vs 默认模式 | WAL 模式对并发写入的提升 |
| D2 | 对比批量提交 vs 逐条提交 | 批量写入的优化空间 |

**指标**: 相同并发下的 P95 延迟对比

---

## 三、执行步骤

### Step 0: 环境准备（一次性）

```bash
# 1. 确认 mock server 脚本存在
ls benchmark/scripts/mock_llm_server.py

# 2. 确认 benchmark client 存在
ls benchmark/scripts/benchmark.py

# 3. 修改配置，指向 mock server
# 编辑 config/default.yaml:
# proxy:
#   providers:
#     openai:
#       base_url: "http://127.0.0.1:18081"
```

### Step 1: 启动服务（每个实验前）

```bash
# 终端 1: 启动 Mock LLM Server
python benchmark/scripts/mock_llm_server.py

# 终端 2: 启动 Gateway
uv run gateway

# 终端 3: 运行压测（见 Step 2）
```

### Step 2: 运行实验

```bash
# 实验 A: 非流式延迟基准
cd benchmark
python scripts/benchmark.py --experiment latency --output results/latency_$(date +%Y%m%d_%H%M%S).json

# 实验 B: 流式 TTFT 基准
python scripts/benchmark.py --experiment streaming --output results/streaming_$(date +%Y%m%d_%H%M%S).json

# 实验 C: 模块延迟分解（需要插桩版本）
python scripts/benchmark.py --experiment breakdown --output results/breakdown_$(date +%Y%m%d_%H%M%S).json

# 实验 D: SQLite WAL 对比
# 先修改 trace/store.py 启用 WAL 模式，再重新运行实验 A
```

### Step 3: 数据收集与报告生成

```bash
# 汇总所有结果
python scripts/generate_report.py --input results/ --output BENCHMARK_REPORT.md
```

---

## 四、数据收集模板

### 4.1 原始数据格式（JSON）

```json
{
  "experiment": "latency",
  "timestamp": "2025-06-11T14:30:00",
  "hardware": {
    "cpu": "Apple M3 Pro",
    "memory_gb": 16,
    "os": "macOS 14.5",
    "python": "3.11.9"
  },
  "config": {
    "gateway_port": 18080,
    "mock_port": 18081,
    "upstream_delay_ms": "1-3"
  },
  "results": [
    {
      "concurrency": 1,
      "total_requests": 50,
      "qps": 45.23,
      "latency_ms": {
        "p50": 18.52,
        "p95": 22.14,
        "p99": 25.67,
        "min": 15.23,
        "max": 28.45
      },
      "errors": 0,
      "error_rate": 0.0
    }
  ]
}
```

### 4.2 报告格式（Markdown）

```markdown
## 实验 A: 非流式延迟基准

| 并发 | 请求数 | QPS | P50(ms) | P95(ms) | P99(ms) | 错误数 |
|------|--------|-----|---------|---------|---------|--------|
| 1    | 50     | 45  | 18.5    | 22.1    | 25.7    | 0      |
| 10   | 100    | 112 | 21.3    | 28.6    | 35.1    | 0      |

**瓶颈分析**: 100 并发时 P95 从 22ms 上升到 79ms，主要瓶颈是 SQLite 写入锁竞争。
**优化建议**: 启用 WAL 模式或迁移到 PostgreSQL。
```

---

## 五、风险与限制

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| Mock server 和 Gateway 在同一台机器，CPU 竞争 | 延迟数据偏高 | 记录硬件信息，面试时说明"本机测试，生产环境会更优" |
| SQLite 文件在 SSD 上，I/O 性能过好 | 低估生产环境瓶颈 | 明确说明测试环境，不夸大性能 |
| 单轮短对话不代表长对话性能 | 数据适用范围有限 | 在报告中标注"基于 15 prompt + 10 completion tokens 的短对话" |
| 未测试真实网络延迟 | 缺少端到端数据 | 补充说明"网关自身延迟 = 总延迟 - 上游延迟" |

---

## 六、反思与改进流程（面试后闭环）

### 6.1 面试中暴露的问题 → 压测改进

```
面试中被问到的问题
        │
        ▼
  是否涉及性能数据？
        │
   ┌────┴────┐
   │         │
  是        否
   │         │
   ▼         ▼
  检查当前    无需改进
  压测数据
  是否足够
   │
   ├─ 数据缺失 → 补充实验（回到 Step 2）
   │
   └─ 数据不准 → 修正测试方法（回到实验设计）
        │
        ▼
  更新 BENCHMARK_REPORT.md
  更新 INTERVIEW_GUIDE_V2.md 中的数字
```

### 6.2 定期复盘检查清单

每次面试后，用以下清单复盘：

- [ ] 面试官是否问了性能相关问题？
- [ ] 我给出的数字是否有压测数据支撑？
- [ ] 面试官是否对某个数字表示怀疑？
- [ ] 是否有我没测过的场景被问到？（如长对话、多轮流式）
- [ ] 是否需要补充新的实验？
- [ ] 是否需要修正已有的测试方法？

### 6.3 持续改进方向

| 优先级 | 改进项 | 触发条件 |
|--------|--------|---------|
| P1 | 补充长对话压测（1000 tokens+） | 面试中被问到"长 prompt 性能如何" |
| P1 | 补充真实 LLM API 端到端测试 | 面试中被问到"真实场景下的延迟是多少" |
| P2 | 补充内存泄漏测试（长时间运行） | 面试中被问到"运行一天后内存占用多少" |
| P2 | 补充 Presidio NLP 性能基准 | 项目中启用了 Presidio 后 |
| P3 | 补充分布式部署压测 | 架构演进为多实例后 |

---

## 七、文件清单

```
benchmark/
├── README.md                    # 本文件（压测规划书）
├── scripts/
│   ├── mock_llm_server.py      # Mock 上游 LLM 服务器
│   ├── benchmark.py            # 压测客户端（主脚本）
│   └── generate_report.py      # 报告生成器
├── results/
│   ├── latency_20250611_143000.json
│   ├── streaming_20250611_143500.json
│   └── ...                     # 每次运行的原始数据
└── BENCHMARK_REPORT.md         # 最终报告（由 generate_report.py 生成）
```

---

> **执行原则**: 先跑起来，再优化。Mock server + 50 次单请求测试只需要 5 分钟，就能拿到第一个真实数据。不要追求完美方案而迟迟不开始。
