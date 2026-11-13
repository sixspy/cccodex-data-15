# cc-data-lab

CC_Codex 二期（26.11）**数据工程**主线基线：**采集任务控制台**（Flask 后端 + HTMX 前端页 + 站内 Mock 目录）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数据采集、爬虫与 RPA  

## 技术栈

- Python 3.12+、Flask 3、SQLite  
- HTMX（CDN）+ Jinja 模板  
- pytest  

## 本地运行

```powershell
cd claude-workspace\cc-data-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

浏览器打开 http://127.0.0.1:5590 ，登录 **admin / 123456**（撤下急件请用 **supervisor / 123456**）。

流程：创建任务 → 选种子 `/mock/catalog/alpha` 或 `beta` → 详情页「运行采集」→ 查看落库（alpha 含重复标题，会演示去重跳过）。

## 急件通道 / 常件候着区 / 当日名额

急件标记不是单任务上的一个勾，而是「任务级指定 + 站级按日名额 + 当日锁定落空口径」的组合：

| 规则 | 实现位置 | 说明 |
|------|----------|------|
| 新建时指定急件 | 列表页「新建任务」勾选“指定为急件” | 必须写急件编码与原因，任一为空直接失败 |
| 全站当日名额 | `/settings` 急件设置页 | 按**后台日期**结算；用满后新指定失败并提示“名额已满”。测试可钉死日期：`app.config["TODAY_OVERRIDE"]` / 环境变量 `BACKEND_DATE` |
| 并发争名额 | `BEGIN IMMEDIATE` 串行化 | 两个任务同时争最后一个名额只有一个成功，失败原因记入详情页「争用失败原因」 |
| 开跑先急后常 | `start_run` | 急件标记的任务优先整批入账；常件按“让路条数”先收一部分，其余进候着区按 `seq` 排队 |
| 落空处理 | 指定时选定并在当日锁定 | `冻结`：冻结候着区直到撤下急件标记；`降级`：候着区旁路按常件继续，写入记录打降级标记 |
| 处理不对 | 点「继续」时校验 | 当日锁定的落空口径与设置页登记值不一致 → 直接挡下并记录“处理不对” |
| 撤下约束 | 详情页「撤下急件标记」 | 必须写原因；不能由指定人本人撤下 |
| 未收束保护 | `is_day_unsettled` | 当日还有候着卡片未收束时，不能改急件标记、不能撤下 |

演示数据：`/mock/catalog/empty`（空目录）与 `/mock/catalog/flaky`（页面 404 但登记了应收条目）用于演练急件落空。

列表卡片展示：急件通道、候着顺序、名额余量（`今日已用 / 名额`）、当前口径；详情页展示急标记、降级标记、候着顺序与争用失败原因。

验收对照 `tests/test_rush.py` 的七段：名额用尽失败、急件先于常件、冻结/降级、处理不对、并发唯一成功、未收束拒绝修改、列表卡片信息。

## Docker 一键启动（推荐用于快照前验收）

需已打开 **Docker Desktop**：

```powershell
cd claude-workspace\cc-data-lab
docker compose up -d --build
docker compose ps
```

- 页面：http://127.0.0.1:5590/login  
- Mock：http://127.0.0.1:5590/mock/catalog/alpha  
- 停止：`docker compose down`（保留库卷）· 清卷：`docker compose down -v`

`docker compose ps` 中 `STATUS` 含 **healthy** 表示服务可访问。线下表「环境可复现等级」可填 **已容器化，可一键起环境**。

## 前后端说明

| 层 | 说明 |
|----|------|
| 后端 | `app/` 路由、任务状态机、同步 runner、SQLite |
| 前端 | `templates/` + `static/`，浏览器可点操作 |
| Mock 源站 | `/mock/catalog/<id>` HTML，无需外网 |

## 初始快照

在首次 Claude 单轮跑题前 commit + push 本树（不含 `.venv/`、`instance/*.db`）。后续题目在同 repo 上叠加调度、RPA 步骤、增量水位等能力。

## 基线 vs 跑题目录

| 路径 | 角色 |
|------|------|
| `claude-workspace/cc-data-lab`（本目录） | **基线**：改功能、docker 验收 |
| `ccproject/cc-data-lab/data-XX` | **单题**：复制基线后挂容器、出快照 |

复制：`..\scripts\Copy-BaselineToQuestion.ps1 -QuestionId data-01`

## 跑题 Harness

见 `CCcodex/docs/ClaudeCode_Windows使用说明.md`（镜像 2.0）。容器挂载 **ccproject 下该题目录**，不是本基线路径。
