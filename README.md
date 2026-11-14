# cc-etl-lab

CC_Codex 二期（26.11）**数据工程**产品线：**抽取转换加载台**（FastAPI + Vue 3 SPA + SQLite）。

与 `cc-data-lab`（Flask + HTMX 采集控制台）错开：本线面向 **ETL 管道编排 / 抽取落库**，前端为 Vue 单页，暖色仓运风格。

## 领域

- **所属领域**：数据工程  
- **子方向**：抽取-转换-加载 / 数据采集与落库  

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、FastAPI、Uvicorn、SQLite |
| 前端 | Vue 3（CDN Composition）、双栏管道台 |
| 风格 | 暖琥珀仓运底、圆角强调、等宽源地址 |
| 测试 | pytest + httpx |

## 基线能力

登录、管道列表、管道详情、执行抽取、执行落库。后续单轮题在此叠加增量水位、清洗口径、并发争用、跨日重算等。

## 本轮增量：落库前字段脱敏闸

- **脱敏规则册**：增删规则（规则名必填，抽空禁止保存）；抽取流程未收束（`抽取中`）时禁止改规则。
- **脱敏强度**：`全掩码` / `按共享分流`，选定即写入当天策略，当天不允许更换；提交强度与当天策略不一致时报「脱敏强度写歪了」。
- **对照抽样**：掩码全部在服务端完成，前端只展示前后对照，每个字段至少三条。
- **落库闸门**：
  - 全掩码：所有管道必须先掩码才允许落库；
  - 按共享分流：对外共享管道强制掩码，内部管道可明文落库但写审计；
  - 未脱敏完成一律返回 409 并提示「未脱敏完成，禁止落库」。
- **并发争抢**：同一管道当天脱敏提交只成一笔，其余提交记争抢失败并留痕。
- **总览 / 详情**：总览显示待脱敏、已掩码、审计条数、当日强度名；详情可读对照样例、争抢记录与审计记录。

## 本地

```powershell
cd claude-workspace\cc-etl-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6000/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
