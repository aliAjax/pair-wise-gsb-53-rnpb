# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动（启动时自动续算中断的组事务）。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限、材料完整性和冲突检查，以及案组期限顺延/差异追加。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/group_repository.py`：案组、成员、材料回执表与可恢复组事务（修订/水位线、检查点、续算）。
- `src/group_service.py`：案组建组、回执提交、拆分/合并仲裁、待合组处置与崩溃恢复。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与关联案组事务测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

关联案组接口（同样需要`X-User-Id`、`X-Role`）：

- `POST /api/groups`：建组，`{"name":"...","members":[{"record_id":1,"role":"principal"},{"record_id":2,"role":"dependent","depends_on_receipt":"R-1"}]}`，恰好一个主案。
- `GET /api/groups` / `GET /api/groups/{id}` / `GET /api/groups/{id}/members`：案组与成员。
- `GET /api/groups/{id}/pending`：待合组的回执与改组请求。
- `GET /api/groups/{id}/audit`：案组事件时间线。
- `POST /api/groups/{id}/receipts`：提交材料回执，请求体`{"base_revision":1,"base_op_seq":0,"data":{"receipt_key":"R-1","anchor_record_id":1,"kind":"reschedule","shift_days":15,"documents":["..."]}}`，`kind`为`reschedule`或`supplement`。
- `POST /api/groups/{id}/restructure/{split|merge}`：改组，`{"base_revision":1,"base_op_seq":0,"data":{...}}`。
- `POST /api/receipts/{id}/resolve|discard`：把待合组回执合入当前修订或弃用。
- `POST /api/group-requests/{id}/resolve|discard`：处置待合组的改组请求。
- `POST /api/group-transactions/{id}/resume`：从最近完成案件续算中断的组事务。

## 关联案组事务语义

主案改期或补件后，关联案的期限按一个**可恢复的组事务**处理：

- **回执带案组修订号**。案组有两个水位线：`revision`（仅拆分/合并推进的结构修订号）和`op_seq`（写入总序号）。
- **选择性失效重算**：回执只让锚点主案与声明依赖该回执的关联案失效重算；未决定案件顺延期限并刷新派生字段（审计记`deadline_recomputed`，含`invalidated`标记），**已决定/归档案件只追加差异**（审计记`receipt_diff_appended`，状态与期限不变）。
- **拆分/合并隔离**：改组产生新修订。携带旧修订号、或水位线落后（`base_op_seq`不匹配）的回执/改组不会写入，而是**保留输入停在“待合组”**，由`resolve`显式决定是否按当前修订执行；`discard`可弃用。已合并/拆分的旧组上的回执同样不能写进新组。
- **先到修订生效**：回执与改组并发时，以`BEGIN IMMEDIATE`串行化并比较水位线，先到者生效，后到者停在待合组；活动组事务执行期间到达的写入也停在待合组。
- **可恢复**：组事务按案件拆成步骤，每完成一个案件立即提交检查点（`case_receipt_applications`以“案件+回执”幂等）。写入中断后从最近完成的案件继续，重复回执或重试不会重复顺延；服务启动时自动恢复所有活动事务。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及关联案组的选择性失效重算、拆分/合并隔离、先到修订仲裁、待合组处置、检查点续算与崩溃恢复。
