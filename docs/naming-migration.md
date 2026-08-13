# N.M 命名迁移

## 目标

本项目的原创产品名称为 **N.M**。本次迁移把历史 WorkBuddy/旧 Harness 内部标识从新业务路径中移除，同时保留一次性数据迁移能力，避免已有用户数据和浏览器登录态丢失。

## 命名映射

| 范畴 | 旧标识 | N.M 新标识 |
|---|---|---|
| 产品显示名 | `WorkBuddy AI`、旧 Harness 文案 | `N.M` |
| Python 顶层包 | `harness` | `nm` |
| Python 发行包 | `ai-agent-harness` | `nm-office` |
| CLI 主命令 | `harness` | `nm` |
| Web CLI | `harness-web` | `nm-web` |
| 默认运行目录 | `.harness`、`.workbuddy` | `.nm` |
| 环境变量前缀 | `HARNESS_` | `NM_` |
| localStorage token | `wb_token` | `nm_token` |
| localStorage 用户 ID | `wb_user_id` | `nm_user_id` |
| Docker 镜像 | `workbuddy-ai` | `nm-office` |
| Docker 数据卷 | `workbuddy-data` | `nm-data` |

## 兼容策略

- 新代码只生成和读取 N.M 标识。
- 配置加载可以在一个迁移周期内读取 `HARNESS_*`，读取时输出弃用警告；不再写入旧变量。
- 浏览器首次加载时，如果存在 `wb_token`/`wb_user_id`，先复制到 N.M 键并调用 `/api/auth/me` 验证；验证成功后删除旧键，失败时清理失效旧键并回到登录页。
- 旧 `.harness`/`.workbuddy` 数据只通过 `nm.migrations` 迁移到 `.nm`。源文件必须保留，迁移失败不得删除源数据。
- 迁移应幂等，重复执行不能重复写入或覆盖用户新数据。

## 旧标识允许出现的位置

旧标识只允许出现在以下位置：

1. `nm/migrations/`：旧数据、旧环境变量和旧浏览器键的读取兼容代码。
2. `docs/naming-migration.md`：映射表、升级说明和回滚说明。
3. `tests/fixtures/legacy/`：不可修改的历史数据夹具。
4. 迁移测试中明确标注的旧输入值。

旧标识不得出现在新业务代码、默认配置、日志、UI、包元数据、默认路径或新生成的数据中。`wb` 作为第三方 `openpyxl` workbook 局部变量不属于产品标识，可以保留。

## 迁移期限

默认兼容周期为一个版本周期：N.M `3.0.0` 发布后保留一次迁移入口；下一个主版本移除旧环境变量、旧浏览器键和旧目录读取逻辑。实际移除前必须在 release notes 中再次声明。

## 回滚

1. 停止 N.M 服务。
2. 保留并恢复迁移前备份的 `.harness`/`.workbuddy` 源目录。
3. 使用迁移前版本启动旧目录。
4. 如果只需回滚浏览器登录态，可重新写入旧版本使用的浏览器键；服务端 token 不上传到第三方。

## 验收清单

- 新业务源码、UI、日志、默认配置和包元数据只使用 N.M 标识。
- `.nm` 能独立初始化并保存 session、memory、OA、IM、用户和缓存数据。
- 旧目录、旧变量、旧 localStorage 键和旧 session 可以迁移，源数据完整。
- 迁移重复执行幂等，迁移失败保留源数据。
- Python 测试、打包、Docker 和 API 启动验收通过。
