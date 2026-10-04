# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：单案状态转换、法定天数、补件期限和材料完整性检查。
- `src/deadlines.py`：案组期限联动纯规则（选择性失效、累计顺延、已决定案件差异）。
- `src/repository.py`：单案SQLite建表、事务和查询。
- `src/group_repository.py`：案组、回执、期限行、组任务与待合组表，以及写锁内原语。
- `src/group_service.py`：可恢复组事务编排、权限、失效重算、断点续跑与合组。
- `src/service.py`：单用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：单案事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与案组事务测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表，并恢复上次写入中断、尚未完成的组事务。

## 单案接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

## 案组事务接口

同行家属案组成员区分`main`（主案）与`related`（关联案，带`depends_on_record_id`指向同组主案）。

- `POST /api/groups`：建组，`{"reference":"FAM-1","data":{"members":[{"record_id":1,"member_role":"main"},{"record_id":2,"member_role":"related","depends_on_record_id":1}]}}`。
- `GET /api/groups` / `GET /api/groups/{id}`：案组列表 / 详情（含成员、期限行、已决定案件差异）。
- `GET /api/groups/{id}/audit`：案组事件时间线。
- `POST /api/groups/{id}/receipts`：登记材料回执并执行组事务：
  - `{"data":{"receipt_key":"RC-1","origin_record_id":1,"expected_revision":1,"shift_days":15,"note":"主案补件改期"}}`。
  - 成功返回`200`；修订号落后时返回`202 {"status":"pending_merge"}`，输入原样进入待合组队列。
- `POST /api/regroups`：拆分或合并案组，`{"data":{"sources":[{"group_id":1,"expected_revision":2}],"plans":[{"reference":"FAM-2","members":[...]}]}}`。
- `GET /api/pending-merges`：待合组队列。
- `POST /api/pending-merges/{id}/resolve`：人工合组；回执需带`{"data":{"target_group_id":2}`，改组可携带最新来源修订号确认。
- `POST /api/pending-merges/{id}/discard`：放弃待合组输入。
- `POST /api/group-jobs/resume`：手动从检查点恢复未完成的组事务（启动时也会自动执行）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。案组读写需要`case_officer`/`supervisor`/`admin`，拆分合并与合组需要`supervisor`/`admin`。

## 组事务语义

- **回执带案组修订号**：每个案组维护单调递增`revision`，回执携带`expected_revision`；拆分/合并产生修订号从1开始的**新案组**，旧组定格为`dissolved`。旧修订回执不能写进新组，只能停在待合组，经人工显式指定目标并按目标组当前修订重新校验后才能合入。
- **选择性失效重算**：回执只让出具回执的主案和**直接依赖它的关联案**失效；其中未决定案件标记`stale`并重算期限，其他案组与依赖其他主案的案件不受影响。
- **已决定/归档只追加差异**：`decided`/`closed`案件的期限不改写，回执仅向`deadline_diffs`追加"本应顺延到何日"的差异。
- **先到修订生效，后到待合组**：所有写操作在`BEGIN IMMEDIATE`写锁内做修订号CAS。先提交者推进修订；后提交者保留原始输入停在`pending_merges`，不触碰任何期限。
- **可恢复**：回执事务分两段。登记段在一把锁内写入回执、任务、逐案件条目、失效标记和修订号；执行段逐案件独立提交并记录检查点`last_completed_record_id`。写入中断后，服务重启或手动恢复时从最近完成案件之后继续。
- **不重复顺延**：回执编号全局唯一，任务条目按`(job_id, record_id)`唯一。重复回执、崩溃重试、重复合组都会命中已完成条目或既有回执，不再二次顺延。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖单案完整流程、规则计算、权限与版本冲突，以及案组的选择性失效、已决定案件差异、重复回执幂等、修订落后停车、拆分后旧修订隔离、合并、回执与改组并发交错、写入中断断点恢复等场景。
