# Metabase Query Skill

一个面向 Codex、Claude Code 等支持 `SKILL.md` 的 coding agent 的 **Metabase 只读查库 skill**。附带零第三方运行依赖的 Python 工具，支持发现数据库与表、按字段名写 MBQL 过滤、查询任务及依赖链、解析嵌套 JSON。

这是从自用技能整理出的通用版本，仓库不包含实际账号、密码、API key、会话、内部服务地址、环境数据库编号或业务数据。

## 安装

需要 Python 3.10+ 和 Git。选择一个位置安装即可，已有同名目录时先检查其内容。

Codex（PowerShell）：

```powershell
# 如果使用自定义 CODEX_HOME，将下面路径改成该目录下的 skills/metabase-query。
git clone https://github.com/YjWilliam101010/metabase-query-skill.git "$env:USERPROFILE/.codex/skills/metabase-query"
```

Claude Code（PowerShell）：

```powershell
git clone https://github.com/YjWilliam101010/metabase-query-skill.git "$env:USERPROFILE/.claude/skills/metabase-query"
```

macOS / Linux（Codex）：

```sh
git clone https://github.com/YjWilliam101010/metabase-query-skill.git "${CODEX_HOME:-$HOME/.codex}/skills/metabase-query"
```

安装后启动新的 agent 会话。在需要时调用 `$metabase-query`，或说明“通过 Metabase 查询某条任务及其依赖”。脚本也可独立使用，不依赖某个 agent。

## 本地配置

连接信息由你在本机设置。不要把真实值粘贴到聊天、命令行参数、示例文件或 Git 提交里；可由操作系统、IDE 或私有启动脚本注入环境变量。

| 变量 | 用途 |
|---|---|
| `METABASE_URL` | 实例地址；保留 `/metabase` 等部署子路径 |
| `METABASE_API_KEY` | API key，与下面的账号密码方式二选一；优先使用 |
| `METABASE_USERNAME`、`METABASE_PASSWORD` | 账号密码登录 |
| `METABASE_CREDENTIALS_FILE` | 可选：指向仓库外的私有凭据 JSON，包含 `username`、`password` |
| `METABASE_DATABASES` | 可选：逗号分隔的数据库 ID，也可每次传 `--db` |
| `METABASE_TABLE` | 可选：表名，默认 `layout_task`；也可传 `--table` |

下面演示 PowerShell 中的交互输入，避免把真实密码写进命令历史。环境变量仍可被有权限的本机进程读取，密码会在 Python 登录过程中以明文驻留内存；这不是凭据保险箱。

```powershell
$env:METABASE_URL = 'https://metabase.example.com'
$env:METABASE_USERNAME = Read-Host 'Metabase username'
$securePassword = Read-Host 'Metabase password' -AsSecureString
$env:METABASE_PASSWORD = [System.Net.NetworkCredential]::new('', $securePassword).Password
# 从刚才安装的 skill 目录运行：
python -I ./mb.py databases
# 使用完毕后，清除这个终端中的密码变量。
Remove-Item Env:METABASE_PASSWORD
```

`credentials.example.json` 只展示格式。需要文件登录时，把自己的凭据文件放在仓库之外，限制本机访问权限，并设置 `METABASE_CREDENTIALS_FILE`。脚本不自动读取 `.env`，也不在磁盘缓存会话令牌或表结构。

## 使用

以下从 skill 目录运行。所有数据库编号和任务编号均为示例，请先发现真实的数据库与表结构。

```sh
python -I mb.py databases
python -I mb.py tables --db 1
python -I mb.py fields --db 1 --table layout_task
python -I mb.py cards layout
python -I mb.py get TASK_ID --db 1 --fields id,task_type,input_json,output_json --out results/task.json
python -I mb.py deps TASK_ID --db 1 --fields id,task_type,task_chain_id --out results/dependencies.json
python -I mb.py query --db 1 --table orders --filter-file examples/filter.json --fields id,status --limit 10 --out results/orders.json
```

可用 `--db 1,2` 查询多个环境、`--schema public` 消除同名表歧义、`--id-field task_id` 适配主键、`--dependency-fields parent_id,chain_id` 适配依赖字段、`--json-fields payload,result` 解析其他 JSON 列。排序可用 `--order-by=created_at` 或 `--order-by=-created_at`，相应字段必须存在。

默认最多返回 **每个数据库 20 行**，可通过 `--limit` 调整至 1–1000 行。默认字段为 `all`；建议显式传 `--fields`，减少大字段传输。终端只显示紧凑行摘要，`--out` 才保存完整查询结果；采样结果不代表全量计数。输出文件可能包含敏感业务数据，不要提交。

## 只读与兼容性

- 网络接口仅允许数据库/表/字段/卡片元数据读取、结构化 `POST /api/dataset` 查询，以及登录所需的 `POST /api/session`。不提供原生 SQL、执行已有卡片、写库或修改卡片/仪表盘的命令。
- 请使用具备只读数据权限的账号或 API-key 用户组；脚本限制不能替代服务端权限。
- 默认验证 TLS，拒绝包含账号密码的 URL 与远程明文 HTTP，禁止自动重定向认证请求。开发机上的 loopback HTTP 可用。
- API key 优先；密码方式仅在本次进程内保存会话，401 时最多重新登录一次。异常消息不输出认证值或原始服务端响应。
- Metabase API 不版本化。本工具沿用 classic table-based MBQL，并不承诺兼容所有 Metabase 版本；连接前可查看实例的 `/api/docs`。API key 是否可用取决于实例版本；SSO/MFA-only 登录流程不在这个工具的支持范围内。

参考：[Metabase API](https://www.metabase.com/docs/latest/api)、[API keys](https://www.metabase.com/docs/latest/people-and-groups/api-keys)。

## 离线验证

```sh
python -B -m unittest discover -s tests -v
python -I mb.py --help
```

测试使用伪造 HTTP 响应，不连接任何真实 Metabase 实例。MIT License。
