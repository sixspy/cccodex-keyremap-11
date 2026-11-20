# 实体键桥

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 实体键桥 |
| 目录 | `07-keyremap` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10921 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

一对多/多对一、生效窗、旧键只读、重映重放

## 避开

别做成 Feed 冲突归并；正式区不是 staging 换皮

## 互斥切口候选

- 冲突拒收整批 vs 进冲突区后重放
- 废弃旧键只读保留 vs 重写正式键
- 生效窗原子切换 vs 双窗按事件时裁

## 源键分段闸（租户号 / 业务号拆传）

入站不再接收拼好的旧键，而是**分开传租户段与业务段**，由服务端生成规范旧键并落证据。
原先 `srcparts_gate` 为空桩，现已落地为 `backend/srcparts_gate.py`。

### 证据形态（二选一，选第一种）

| 形态 | 说明 | 本仓 |
|------|------|------|
| `src_part_ledger` 只追加分段事件表 | `ingest_id + tenant_part + biz_part + canon_old`，触发器禁止 UPDATE/DELETE；补段 = 追加新事件 | ✅ 采用 |
| 入站行 `src_parts_blob` 不可变列 | 段证据内嵌在入站行 | ❌ 不采用 |

页面只提交两段原值，规范键一律由服务端生成；不存在“页面临时拼接”的旁路。

### 正确性口径（全局单选，部署级写死）

口径存于 `app_settings.src_part_arch`，启动时与 `KEYREMAP_SRC_PART_ARCH` 交叉校验；
**已有库不允许热切换**，请求参数也不能携带口径（带 `?arch=` 会被忽略）。

- **甲 `/ complete`**：任一分段为空 → 整笔拒绝重映，正式区零增，入站转“待补段”。
- **乙 `/ degrade`**：允许仅 `biz_part` 出键，但**同一事务**写 `missing_part_marks`
  标记并把该笔剔出“齐套”桶、落入“缺段”桶；落键/标记/入桶任一失败 → 一并回滚。

硬约束由数据库触发器兜底（即使绕过接口直写库也拦得住）：齐套架构禁止缺段出键、齐套正式行
必须有齐套证据、缺段标记只能挂缺段行、缺段不得进齐套桶、无标记不得进缺段桶。

### 并发

重映与补段共用 `BEGIN IMMEDIATE` 写锁；补段另带 `expected_rev` 乐观并发。争用/拒绝一律写入
`write_trace`，不允许两路同时抢写且无留痕。

### 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/src-parts/ingest` | 入站装载：`{tenant_part, biz_part, ingest_id?}` |
| POST | `/api/src-parts/complete?ingest_id=` | 补段：`{tenant_part?, biz_part?, expected_rev}` |
| POST | `/api/src-parts/remap?ingest_id=` | 重映闸：按部署口径出键/拒写 |
| GET | `/api/src-parts/ingest/{id}` | **只读**核证（`mode=ro` 连接）：分段、证据事件、映射、正式行、互证桶、不变量 |
| GET | `/api/src-parts/inbound` | 入站台列表 |
| GET | `/api/src-parts/summary` | 互证桶计数、缺段标记、写入留痕 |

### 本地夹具

```bash
python -m pytest -q          # tests/test_srcparts.py
```

覆盖：缺租户段时齐套架构拒写（正式区零增）、降级架构出键且缺段标记可查、并发补段争用可复现、
证据只追加、口径单库单选、核证接口只读。

## 启动（一键）

`powershell
cd projects/07-keyremap
docker compose up -d --build
`

浏览器：http://127.0.0.1:10921/  
健康检查：GET http://127.0.0.1:10921/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10604
python -m pytest -q
`
