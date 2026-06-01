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
./tax-portal sync --store-key fuzzy
./tax-portal dry-run --store-key fuzzy
./tax-portal run --store-key fuzzy
```

- `./tax-portal sync`：先把服务器上的最新模板同步到本地 `output/*.xlsx`
- `./tax-portal dry-run`：导入税务局批量开票页做校验，不真实提交
- `./tax-portal run`：执行真实提交
- `./tax-portal dry-run` / `./tax-portal run`：如果本地还没有打开专用 Chrome CDP 窗口，会先自动帮你拉起，再继续原流程

当前推荐的税务局浏览器模式是 `chrome_cdp`：

- 不再依赖独立 Playwright profile 里的税务局会话
- 先启动一个专用 Chrome 实例
- 可以在这个专用实例里手动登录一次税务局；如果配置了电子税务局账号密码，runner 也可以在扫码页自动完成 app 登录和扫码确认
- 后续 runner 直接接管这份活跃会话

推荐流程：

1. 启动专用税务局 Chrome CDP 实例：

```bash
./tax-portal open
```

也可以直接执行 `./tax-portal dry-run ...` 或 `./tax-portal run ...`，如果本地还没打开专用 Chrome，它们会先自动拉起一个。

2. 二选一：

   - 在新打开的 Chrome 窗口里手动登录税务局，进入目标企业首页
   - 或者配置本机 `电子税务局` app 账号密码，让 runner 在扫码页自动完成登录和扫码确认

   建议：

   - 保持这个专用 Chrome 窗口不要关闭
   - 最好保留“已登录的税务局首页”这个 tab，runner 会优先复用它
   - 如果这个首页 tab 不在了，runner 会退化成自己新开一个“干净首页”再继续

自动扫码登录当前实现的实际步骤：

1. Chrome 落到税务局扫码页后，runner 会截取二维码并写入 `TAX_PORTAL_ARTIFACTS_DIR`
2. 把二维码导入本机 `照片` 资料库
3. 拉起 `电子税务局` app，进入“我的”
4. 走短信登录：
   - 账号 / 密码登录
   - 点击 `获取验证码`，并等待按钮变成 `xx秒重新获取`
   - 优先使用 macOS 一次性验证码填充；没有出现时，再从 macOS `信息` 读取最新厦门税务验证码
5. 登录后固定按顺序处理：
   - `请选择身份类型` -> 选择 `法定代表人` 或 `办税员`
   - `是否开启指纹快捷登录?` -> 点击 `暂不设置`
   - 回到已登录首页
6. 点击首页右上角扫码图标
7. 在扫码页点击右下角 `相册`
8. 打开的不是系统 `照片` app 主窗口，而是税务 app 内部的照片选择器；runner 会在这个内部选择器里优先选择左上第一张二维码图片
9. 进入 `登录确认` 界面后点击 `登录`

3. 跑导入校验：

```bash
./tax-portal dry-run --store-key fuzzy --skip-sync
```

4. 跑真实提交：

```bash
./tax-portal run --store-key fuzzy --skip-sync
```

说明：

- `./tax-portal sync` 适合“先拉最新模板到本地，再人工检查或修改本地 Excel”这类场景
- `./tax-portal run` / `./tax-portal dry-run` 会自动清掉代理环境变量，并强制使用 `chrome_cdp`
- 如果本地 `http://127.0.0.1:9222` 没有可用 CDP，会先自动执行一次“打开专用 Chrome”再继续
- `--skip-sync` 表示直接使用当前本地 `output/*.xlsx`，不在执行前重新从服务器覆盖
- 如果要先同步服务器模板，再跑开票，先执行 `./tax-portal sync`，再执行 `./tax-portal dry-run --skip-sync` 或 `./tax-portal run --skip-sync`

只同步服务器上的最新模板到本地 `output/`：

```bash
./tax-portal sync --store-key fuzzy
```

税务局 runner 只做导入校验，不真实提交：

```bash
./tax-portal dry-run --store-key fuzzy
```

税务局 runner 真实提交：

```bash
./tax-portal run --store-key fuzzy
```

如果你先从服务器拉了一份模板到本地 `output/`，并且手工改过这份本地文件，希望本次开票直接使用本地修正版而不是重新从服务器覆盖，增加 `--skip-sync`：

```bash
./tax-portal dry-run --store-key fuzzy --skip-sync
./tax-portal run --store-key fuzzy --skip-sync
```

税务局 runner 相关环境变量：

- `TAX_PORTAL_USER_DATA_DIR`: 本机浏览器持久化 profile 目录，建议使用独立目录
- `TAX_PORTAL_ARTIFACTS_DIR`: runner 截图和调试产物目录
- `TAX_PORTAL_BROWSER_BACKEND`: 税务局浏览器后端，支持 `playwright` 和 `chrome_cdp`
- `TAX_PORTAL_CHROME_CDP_URL`: 当 `TAX_PORTAL_BROWSER_BACKEND=chrome_cdp` 时，连接当前 Chrome 的 CDP 地址，默认 `http://127.0.0.1:9222`
- `TAX_PORTAL_CHROME_CDP_USER_DATA_DIR`: `portal-open-chrome-cdp` 启动专用 Chrome 实例时使用的独立 user data dir，默认 `./data/tax-portal-chrome-cdp`
- `TAX_PORTAL_CHROME_EXECUTABLE_PATH`: 可选，显式指定 Chrome 可执行文件路径
- `TAX_PORTAL_ETAX_APP_USERNAME`: 可选，本机 `电子税务局` app 登录账号；配置后，runner 在扫码页会尝试自动登录并扫码
- `TAX_PORTAL_ETAX_APP_PASSWORD`: 可选，本机 `电子税务局` app 登录密码
- `TAX_PORTAL_ETAX_APP_PATH`: 可选，本机 `电子税务局` app 路径，默认 `/Applications/电子税务局.app`
- `TAX_PORTAL_SYNC_FROM_CHROME_PROFILE`: 为 `true` 时，runner 启动前先把当前 Chrome profile 的税务局会话相关数据同步到 `TAX_PORTAL_USER_DATA_DIR/Default`
- `TAX_PORTAL_CHROME_PROFILE_DIR`: 可选，显式指定要同步的 Chrome profile 目录；不填时默认读取本机 Chrome `Local State` 的最近使用 profile
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

开启 `电子税务局` app 自动扫码前，请先手动完成一次本机准备：

- 允许当前终端 / Python 进程使用“辅助功能”和“自动化”
- 完成一次 `照片` app 首次初始化
- 确保税务验证码会同步到 macOS `信息`
- 首次使用时，建议先手工确认 `电子税务局` app 的短信登录、身份类型选择、指纹提示、扫码页、内部照片选择器都能正常出现

自动扫码登录的调试信号：

- `requesting SMS verification code`：开始请求短信验证码
- `SMS verification code request accepted attempt=N`：验证码发送成功
- `selected identity role in 电子税务局 app role=... attempt=N`：身份类型选择成功
- `dismissed fingerprint quick login prompt`：已处理指纹快捷登录提示
- `opening scan flow in 电子税务局 app`：开始进入扫码页
- `opening album from scan page`：开始打开扫码页右下角相册入口
- `selecting latest imported QR image from album`：开始在税务 app 内部照片选择器里选图
- `confirming scan login in 电子税务局 app`：进入登录确认并准备提交

`portal-open-chrome-cdp` 默认会自动拉起一个带 CDP 的专用 Chrome 实例。等价的手工启动命令示例：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$(pwd)/data/tax-portal-chrome-cdp" \
  --no-first-run \
  --new-window \
  "https://etax.xiamen.chinatax.gov.cn:8443/loginb/"
```

然后把 `TAX_PORTAL_CHROME_CDP_URL` 设为 `http://127.0.0.1:9222`，并在这个专用实例里手动登录税务局。

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
