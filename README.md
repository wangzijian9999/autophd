# autophd

我的赛博科研助手：一个面向机器学习/计算机视觉研究的闭环自动化科研框架。

autophd 的目标不是替代研究者的最终学术判断，而是把高重复度、可审计的科研探索流程自动化：

```text
找创新点 -> Codex 实现 -> sanity check -> 训练 -> 评估
-> Codex/Claude 独立审阅反思 -> 指标门控 -> 回滚或晋升
```

## 核心能力

- 使用 Codex API/CLI 作为代码实现代理。
- 使用本机 Claude Code 会员账号通过 `claude -p` 做独立审阅，不需要 Anthropic API key。
- 通过配置文件接入真实训练、评估、文献检索、SOTA 核验和外部科研 agent。
- 对每个候选实验使用 Git 分支、日志、指标、门控和提交记录。
- 候选未达标时回滚，只保留诊断信息；候选达标时晋升为新的 verified parent。
- 严格区分事实、假设和信息不足，避免伪造实验结果、引用或 SOTA 结论。

## 安装

```bash
git clone https://github.com/wangzijian9999/autophd.git
cd autophd
python -m pip install -e .
```

也可以不安装，直接运行：

```bash
python autophd/orchestrator.py --config configs/example_config.yaml validate
```

## 快速开始

复制配置模板到你的目标研究代码仓库中：

```bash
cp configs/example_config.yaml /path/to/your/research_repo/auto_research_config.yaml
cd /path/to/your/research_repo
```

修改配置中的关键项：

```yaml
project:
  root: .
  objective: "你的具体研究目标"

commands:
  train: "你的训练命令"
  evaluate: "你的评估命令"

metrics:
  primary:
    name: psnr
    mode: maximize
    baseline: 25.0
    min_delta: 0.05
  extractors:
    - name: psnr
      source: evaluate_stdout
      regex: 'psnr:\s*([0-9.]+)'
      type: float

files:
  allowed_paths:
    - models
    - configs
    - scripts
```

初始化状态：

```bash
autophd --config auto_research_config.yaml init-state
```

运行一次闭环：

```bash
autophd --config auto_research_config.yaml run-once
```

## 外部项目接入

外部项目建议作为命令适配器接入，不直接混入本仓库。例如：

```yaml
commands:
  idea: python /path/to/AI-Scientist-v2/ai_scientist/perform_ideation_temp_free.py --workshop-file {project_root}/PROJECT_BRIEF.md
```

可接入的项目类型：

- AI-Scientist-v2 / Agent Laboratory：创新点和实验计划。
- OpenScholar / PaperQA2：文献证据、SOTA 表和 novelty 检查。
- RD-Agent / Curie / AIDE：实验执行和候选搜索。
- 自定义训练脚本：真实训练、评估、消融和 robustness test。

## 结果目录

默认写入目标研究仓库的 `.auto_research/`：

```text
.auto_research/
  experiments.jsonl
  parents.jsonl
  DEAD_ENDS.md
  INSIGHTS.md
  PROTOCOL.md
  runs/<run_id>/
    logs/
    metrics.json
    review_summary.json
    gate_decision.json
```

## 科研诚信

autophd 不会把模型主观判断当作科学事实。候选实验只有在主指标、关键不退化项、机制证据和可用审阅门控通过后，才会晋升为新的 parent。失败分支只能作为诊断信息，不能继续作为理论构造基础。
