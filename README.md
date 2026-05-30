# 多门店腾讯问卷开票自动化服务

这个项目会在服务端按天批量处理多个腾讯问卷门店的数据，并为每个门店生成独立的开票 Excel。

## 功能

- 同一账号 Cookie 复用，顺序处理多个门店问卷
- 每个门店独立维护 `last_processed_id`
- 只处理新增编号的数据
- 下载付款截图并调用兼容 OpenAI 的多模态接口识别总金额
- 企业抬头可选调用外部税号查询接口补税号
- 按模板生成门店独立 Excel
- 覆盖结果文件前自动备份旧文件
- 批处理完成后发送一封汇总邮件

## 目录

- `app/`: 主程序
- `data/state.db`: SQLite 状态库
- `backups/`: 按门店保存历史结果备份
- `output/`: 各门店最新结果文件

## 配置

1. 复制示例配置：

```bash
cp .env.example .env
cp stores.example.yaml stores.yaml
```

2. 编辑 `.env`：

- `TENCENT_SURVEY_*`: 腾讯问卷 Cookie、导出接口和图片下载默认参数
- `OPENAI_*`: 金额识别模型配置
- `OPENAI_SSL_VERIFY` / `OPENAI_CA_BUNDLE_PATH`: 模型网关证书配置
- `SMTP_*`: 邮件发送配置
- `TEMPLATE_XLSX_PATH`: Excel 模板路径
- `STATE_DB_PATH`: 状态数据库路径
- `STORES_CONFIG_PATH`: 门店配置路径
- `TAX_LOOKUP_PROVIDER`: 税号查询 provider，支持 `disabled`、`alapi`、`legacy_template`
- `TAX_LOOKUP_ALAPI_TOKEN`: `ALAPI` token，`TAX_LOOKUP_PROVIDER=alapi` 时使用
- `TAX_LOOKUP_TIMEOUT_SECONDS`: 税号查询超时
- `TAX_LOOKUP_CACHE_NEGATIVE_TTL_HOURS`: 本地负缓存时长，默认 `24`

如果你想直接使用默认推荐的免费链路，补上这几项：

```env
TAX_LOOKUP_PROVIDER=alapi
TAX_LOOKUP_ALAPI_TOKEN=your_alapi_token_here
```

如果你的模型网关或本地代理用了自签名证书：

- 推荐：把根证书路径写到 `OPENAI_CA_BUNDLE_PATH`
- 临时联调：设置 `OPENAI_SSL_VERIFY=false`

腾讯问卷和税号接口也支持同样的配置：

- `SURVEY_SSL_VERIFY` / `SURVEY_CA_BUNDLE_PATH`
- `TAX_LOOKUP_SSL_VERIFY` / `TAX_LOOKUP_CA_BUNDLE_PATH`

3. 编辑 `stores.yaml`：

- 每个门店一条配置
- `survey_id`、`output_xlsx_path`、`initial_last_processed_id` 必填
- `attachment_question_id` 可选，不填时使用 `.env` 中的默认值
- 税务局 runner 额外字段：
  - `portal_enabled`: 是否允许该门店进入税务局开票 runner
  - `portal_priority`: runner 默认执行顺序，数值越小越先执行
  - `portal_company_switch_name`: 身份切换列表里的目标公司名
  - `portal_company_verify_name`: 切换完成后首页/业务页里用于校验的公司名
  - `portal_company_role`: 身份类型，支持 `legal_representative`、`tax_operator`

## 运行

手动跑一次：

```bash
python3 -m app.main run-once --env-file .env
```

启动定时服务：

```bash
python3 -m app.main schedule --env-file .env
```

单独做视觉模型烟雾测试：

```bash
python3 -m app.main smoke-test --env-file .env
```

单独验证税号查询 provider：

```bash
python3 -m app.main tax-lookup-test --env-file .env --company-name "深圳易思商务咨询有限公司厦门分公司"
```

税务局 runner 首次本机安装：

```bash
python3 -m venv .venv
env -u http_proxy -u https_proxy .venv/bin/python -m pip install -r requirements.txt
env -u http_proxy -u https_proxy PLAYWRIGHT_BROWSERS_PATH=./data/ms-playwright .venv/bin/python -m playwright install chromium
```

税务局 runner 日常命令建议使用仓库内虚拟环境：

```bash
.venv/bin/python -m app.main portal-sync --store-key fuzzy
.venv/bin/python -m app.main portal-issue-dry-run --store-key fuzzy
.venv/bin/python -m app.main portal-issue-run --store-key fuzzy
```

只同步服务器上的最新模板到本地 `output/`：

```bash
python3 -m app.main portal-sync --env-file .env --store-key fuzzy
```

税务局 runner 只做导入校验，不真实提交：

```bash
python3 -m app.main portal-issue-dry-run --env-file .env --store-key fuzzy
```

税务局 runner 真实提交：

```bash
python3 -m app.main portal-issue-run --env-file .env --store-key fuzzy
```

如果你先从服务器拉了一份模板到本地 `output/`，并且手工改过这份本地文件，希望本次开票直接使用本地修正版而不是重新从服务器覆盖，增加 `--skip-sync`：

```bash
python3 -m app.main portal-issue-dry-run --env-file .env --store-key fuzzy --skip-sync
python3 -m app.main portal-issue-run --env-file .env --store-key fuzzy --skip-sync
```

税务局 runner 相关环境变量：

- `TAX_PORTAL_USER_DATA_DIR`: 本机浏览器持久化 profile 目录，建议使用独立目录
- `TAX_PORTAL_ARTIFACTS_DIR`: runner 截图和调试产物目录
- `TAX_PORTAL_HOME_URL`: 税务局首页
- `TAX_PORTAL_IDENTITY_SWITCH_URL`: 企业办税身份切换页
- `TAX_PORTAL_BATCH_ISSUE_URL`: 批量开票页
- `TAX_PORTAL_DISABLE_PROXY`: 为 `true` 时，对 runner 拉起的 Chrome 显式禁用代理，只影响这次税务局自动化，不改系统代理
- `TAX_PORTAL_LOGIN_TIMEOUT_MINUTES`: 登录等待超时
- `TAX_PORTAL_BLOCK_ON_EMPTY_AMOUNT`: 为 `true` 时，模板里金额为空将直接阻断提交
- `PLAYWRIGHT_BROWSERS_PATH`: Playwright 浏览器二进制目录，当前建议使用 `./data/ms-playwright`
- `TAX_PORTAL_SYNC_FROM_SERVER`: 为 `true` 时，runner 开票前先从服务器同步最新 `output/*.xlsx`
- `TAX_PORTAL_REMOTE_HOST`: 服务器地址，如 `root@139.196.140.215`
- `TAX_PORTAL_REMOTE_OUTPUT_DIR`: 服务器上的 output 目录，当前生产为 `/var/lib/auto-invoice-issuance/output`
- `TAX_PORTAL_SSH_KEY_PATH`: 可选，专门给 runner 用的 SSH 私钥路径
- `TAX_PORTAL_SSH_PORT`: SSH 端口
- `TAX_PORTAL_SYNC_CONNECT_TIMEOUT_SECONDS`: 同步模板时的 SSH 连接超时

## Docker

构建镜像：

```bash
docker build -t survey-invoice-batch .
```

运行容器：

```bash
docker run -d \
  --name survey-invoice-batch \
  -v $(pwd)/.env:/app/.env \
  -v $(pwd)/stores.yaml:/app/stores.yaml \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/backups:/app/backups \
  -v $(pwd)/data:/app/data \
  survey-invoice-batch
```

## 发布约束

- 不要直接在服务器上热更新代码，避免出现本地代码、仓库历史和服务器运行版本不一致。
- 所有 repo 跟踪的代码修改，都应先在本地完成开发和验证，再走标准 git 流程发布到服务器。
- 推荐流程：本地改代码 -> 本地测试 -> `git commit` -> 推送远端 -> 服务器拉取指定提交/分支 -> 重建并重启服务。
- 服务器上可以单独维护 `.env`、运行数据和日志，但不要直接修改仓库内业务代码来“临时修复”。

## 服务器部署

当前生产服务器是 `root@139.196.140.215`，已按独立 systemd 服务方式部署：

- 服务：`auto-invoice-issuance.service`
- 运行用户：`invoicebot`
- 代码目录：`/opt/auto-invoice-issuance/current`
- Python 虚拟环境：`/opt/auto-invoice-issuance/venv`
- 环境变量：`/etc/auto-invoice-issuance.env`
- 门店配置：`/etc/auto-invoice-issuance/stores.yaml`
- 运行数据：`/var/lib/auto-invoice-issuance/data`
- 输出目录：`/var/lib/auto-invoice-issuance/output`
- 备份目录：`/var/lib/auto-invoice-issuance/backups`

部署时特意与 `wechat-claw` 隔离：

- 不复用 `wechat-claw.service`
- 不改 `/opt/wechat-claw/current`
- 不改 `/etc/wechat-claw.env`
- Python 依赖只安装到 `auto-invoice-issuance` 自己的 venv

首次部署已经完成，并验证过：

- `tax-lookup-test` 可正常查询 `ALAPI`
- `run-once` 可成功执行
- `wechat-claw.service` 保持 `active (running)`

### 后续更新

后续代码更新走标准 git 发布，不在线改代码。

仓库内已提供简短发布脚本：

```bash
bash deploy/deploy-auto-invoice-issuance.sh
```

默认会发布到 `root@139.196.140.215`。如果后续目标机变化，也可以显式传入：

```bash
bash deploy/deploy-auto-invoice-issuance.sh root@139.196.140.215
```

如果你已经登录到了服务器内部，也可以直接执行：

```bash
sudo bash deploy/deploy-auto-invoice-issuance.sh
```

脚本会自动识别当前机器已经是部署目标机，此时不再二次 `ssh` 自己。

这个脚本会在服务器上执行：

1. 用现有 GitHub SSH key 对 `origin/main` 做 `git pull --ff-only`
2. 在 `/opt/auto-invoice-issuance/venv` 中执行 `pip install -r requirements.txt`
3. 重启 `auto-invoice-issuance.service`
4. 输出最新的 `systemctl status`

如果你只想手动执行远端更新，也可以直接运行：

```bash
ssh root@139.196.140.215 \
  "export GIT_SSH_COMMAND='ssh -i /home/wechatclaw/.ssh/id_ed25519_github_wechat_claw -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/home/wechatclaw/.ssh/known_hosts -o ConnectTimeout=10' && \
   git -C /opt/auto-invoice-issuance/current pull --ff-only origin main && \
   /opt/auto-invoice-issuance/venv/bin/pip install -r /opt/auto-invoice-issuance/current/requirements.txt && \
   systemctl restart auto-invoice-issuance.service && \
   systemctl status --no-pager --lines=20 auto-invoice-issuance.service"
```

### 运行检查

常用检查命令：

```bash
ssh root@139.196.140.215 "systemctl status --no-pager --lines=20 auto-invoice-issuance.service"
ssh root@139.196.140.215 "journalctl -u auto-invoice-issuance.service -n 100 --no-pager"
ssh root@139.196.140.215 "sqlite3 /var/lib/auto-invoice-issuance/data/state.db 'select id, store_key, status, processed_count, started_at, finished_at from run_history order by id desc limit 10;'"
```

## 联调建议

每个新门店至少抓一次：

1. 导出 CSV 的真实 cURL
2. 图片下载请求里真实使用的 `question_id`

如果门店间 `question_id` 不一致，就把它补到对应门店的 `attachment_question_id`。

当前代码已经按下面这条导出链路实现：

1. `POST /api/answer_exports/generate` 创建导出任务
2. 读取返回里的 `data.id`
3. 轮询 `GET /api/files/export_check?job_id=<id>`
4. 使用返回的 `data.result.cos_download_url` 下载 `.csv.zip`
5. 自动解压其中的 `.csv`

## 税号规则

- CSV 里的税号只要是字母数字组合，直接写入
- 中文说明文字，例如 `无税号 个人抬头`，会被视为空
- 企业抬头税号为空时，才尝试调外部查询接口
- `ALAPI` provider 会先按公司名搜索，再只接受“标准化后精确相等”的候选企业名
- 正向命中会写入 SQLite 本地缓存，未命中结果也会按 `TAX_LOOKUP_CACHE_NEGATIVE_TTL_HOURS` 做负缓存
- 如果没配置 provider，则跳过税号查询
- `legacy_template` 会继续兼容旧的 `TAX_LOOKUP_URL_TEMPLATE` / `TAX_LOOKUP_VALUE_PATH` 配置

## 注意

- 当前 Excel 写入基于 `openpyxl`
- 模板中的主要结构、工作表和数据内容会保留，但部分 Excel 扩展型数据校验能力取决于 `openpyxl` 对模板的兼容性
