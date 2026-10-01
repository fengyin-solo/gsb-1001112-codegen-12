# 市政道路桥梁养护管理平台

覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 路段管理 | `road_section` | 管养路段 | 路段编号、路段名称、起止桩号 |
| 日常巡查 | `patrol` | 巡查记录 | 巡查编号、巡查路段、巡查日期 |
| 路面病害 | `pavement` | 病害记录 | 病害编号、所属路段、病害类型 |
| 桥梁定检 | `bridge` | 检测记录 | 检测编号、桥梁名称、检测类型 |
| 桥梁档案 | `bridge_info` | 桥梁 | 桥梁编号、桥梁名称、桥型结构 |
| 隧道管养 | `tunnel` | 隧道 | 隧道编号、隧道名称、隧道长度 |
| 交安设施 | `traffic_facility` | 交安设施 | 设施编号、设施类型、所属路段 |
| 排水设施 | `drainage` | 排水设施 | 设施编号、设施类型、所属路段 |
| 绿化管养 | `green` | 绿化区域 | 区域编号、区域名称、植物品种 |
| 路灯照明 | `lighting` | 路灯设施 | 灯具编号、灯具类型、功率 |
| 除雪防滑 | `winter` | 除雪作业 | 作业编号、作业路段、作业日期 |
| 风雪事件指挥板 | `snow_command` | 风雪事件、封控段、调度任务、应急指挥令 | 事件编号、气象时段、天气基准、事件状态、操作号 |
| 防汛应急 | `flood` | 防汛记录 | 记录编号、预警级别、影响路段 |
| 边坡防护 | `slope` | 边坡 | 边坡编号、所属路段、边坡类型 |
| 伸缩缝管理 | `expansion` | 伸缩缝 | 缝编号、所属桥梁、缝类型 |
| 支座维护 | `bearing` | 桥梁支座 | 支座编号、所属桥梁、支座类型 |
| 养护工程 | `project` | 养护工程 | 工程编号、工程名称、工程类型 |
| 养护车辆 | `vehicle` | 养护车辆 | 车辆编号、车辆类型、车牌号 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 风雪事件指挥板（除雪防滑）

`/snow_command` 把气象时段、作业路段（封控段）与车辆位置编排为同一风雪事件下的连续
事件窗口；调度围绕封控段直接调用材料与班组。

业务规则（均在 `backend/app/services/snow_command.py`，回归测试见
`backend/app/tests/test_snow_command.py`）：

- **三处引用同一事件**：调度会话结论一次写入「除雪作业台账（`snow_ledger`）、
  路段阶段（`snow_segment`）、物资清单（`snow_material`）」，三处均带同一事件引用；
  `GET /api/snow/events/{id}/references` 用于核对一致性。
- **状态顺序流转**：事件与路段阶段均按 `待启动 → 执行中 → 收尾 → 已关闭` 单向流转，
  禁止跳步、禁止回退，`已关闭` 为终态；关闭事件前要求所有路段关闭、任务完成。
- **指挥令优先级**：除雪与防汛同时占用道路时，以现行有效应急指挥令（`emergency_order`）
  判定优先方——防汛优先则暂停除雪派发，无令也不放行；历史台账按事件冻结时的天气基准
  保留，不随后续天气或指挥令变化改动。
- **事务相符**：任务派发、车辆占用（`snow_lock`）、材料库存扣减在同一事务内完成，
  任一校验不通过整体回滚；任务完成时释放车辆并回填台账。
- **断网幂等续传**：每个写操作携带「操作号」；离线时前端把操作排队到
  localStorage（`frontend/src/api/offlineQueue.ts`），联网复位后整单
  `POST /api/snow/sync`，服务端按操作号幂等重放，重复操作只回放首次结果、不重复扣料。
