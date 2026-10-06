# 脱敏文稿工作台

供小团队发布脱敏文稿的网页工作台：作者粘贴正文创建**不可变版本**，审阅人在当前版本上选中文字提议遮盖范围，另一审阅人逐条裁决，全部裁决完毕且**两名不同且非作者的审阅人**确认同一份最终预览后，作者才能发布并导出遮盖后的文本。

技术栈：Python / FastAPI + SQLite + 原生 HTML/JS，本地账号，无任何第三方服务。

## 快速开始（Docker，一条命令）

```bash
docker compose up --build
```

启动后访问：**http://localhost:8000**

数据库会在首次启动时自动创建（建表 + 写入种子账号），SQLite 文件持久化在 `workbench-data` 卷中。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000 ，数据库默认建在 `./data/app.db`。

## 默认账号

| 用户名 | 密码 | 角色 |
|--------|-----------|------|
| alice | alice123 | 作者 |
| bob | bob123 | 审阅人 |
| carol | carol123 | 审阅人 |

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `8000` | 服务监听端口（Compose 中映射到宿主机 8000） |
| `DB_PATH` | 容器内 `/data/app.db`；本地 `./data/app.db` | SQLite 数据库文件路径 |
| `SEED_USERS` | 上表三个默认账号 | JSON 数组，首次启动时写入的账号，如 `[{"username":"u1","password":"p1","role":"author"}]`，`role` 为 `author` 或 `reviewer`；仅对不存在的用户生效 |

## 使用流程

1. **作者**登录后在首页粘贴正文创建文稿（自动生成 v1）。
2. **审阅人**打开文稿，在正文中**选中文字**（表情等 Unicode 字符按 UTF-16 码元精确定位），点击"提议遮盖该范围"。
3. **另一审阅人**对每条提议逐条**接受 / 驳回**（不能裁决自己的提议）；提议人可在被裁决前**撤回**自己的提议。
4. 范围重叠时，预览与导出只按**已接受提议的并集**遮盖，各提议仍保持独立裁决状态。
5. 全部提议裁决完毕后，**两名不同的审阅人**分别点击"确认当前最终预览"；此后任何裁决变化（新提议 / 撤回 / 裁决）都会使已有确认**失效**，需重新确认。
6. 满足条件后**作者**点击"发布"；发布后文稿锁定，不能再修改。
7. 任何登录用户可随时**导出遮盖文本**（只含遮盖后的内容，不泄露原文）。
8. 修改正文须由作者**另建新版本**；旧版本及其批注永久保留、不可变，且**不沿用**到新版本。

## 并发与一致性

- 所有写操作携带 `expected_state_version`（文稿状态版本号），服务端比对不一致即返回 **409** 并提示刷新，前端会自动重新加载。
- 确认与发布的校验和落库在同一个 `BEGIN IMMEDIATE` 事务中完成，保证原子一致。
- 非法选区（越界 / 为空）、无权限操作、确认不足等均返回明确的中文错误信息。

## 主要 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/login` `/api/logout` | 登录 / 退出（会话 Cookie） |
| GET | `/api/todo` | 当前用户待办 |
| GET/POST | `/api/documents` | 文稿列表 / 创建文稿（作者） |
| GET | `/api/documents/{id}` | 文稿详情（含预览、提议、确认状态） |
| POST | `/api/documents/{id}/versions` | 另建新版本（作者） |
| GET | `/api/documents/{id}/versions/{seq}` | 查看历史版本及其批注 |
| POST | `/api/documents/{id}/proposals` | 提议遮盖范围（审阅人） |
| POST | `/api/proposals/{id}/withdraw` | 撤回自己未裁决的提议 |
| POST | `/api/proposals/{id}/decide` | 接受 / 驳回他人提议 |
| POST | `/api/documents/{id}/confirm` | 确认当前最终预览（审阅人） |
| POST | `/api/documents/{id}/publish` | 发布（作者，需满足全部条件） |
| GET | `/api/documents/{id}/export` | 导出遮盖后的文本（text/plain） |
