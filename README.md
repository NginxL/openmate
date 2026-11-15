# OpenMate

OpenMate 是一组开源（MIT）的 [Persona](https://github.com/nwverse/persona) 扩展，把聊天软件接进 Persona：私聊和群消息进收件箱和知识库，Persona 的助手和 Agent 可以在聊天软件里替你出面。助手以你身份主动发的每一条消息都要你先在手机上确认；只有你自己发起的 `/agent` 请求，回复不再确认，但每一条都会记进 Persona 的“以你身份发出的消息”记录。

第一个扩展是**飞书**（`openmate-feishu`）。微信、钉钉以后再做。

> **现状**：一期的平台无关核心（协议、存储、收件箱、会话页、知识库归档、扫码登录流程、写操作、`/agent`）和连接飞书的 LarkAgentX 后端（一期 1b）都已经完成，有完整的自动测试（后端用假的 LarkAgentX 测，另有不联网、不登录的真实 LarkAgentX 冒烟测试）。用真实账号扫码登录、收发消息的端到端验证还在进行。

## 和 Persona 的关系

- **仓库就是扩展市场**：根目录的 `persona-extensions.json` 是市场索引，在 Persona 里添加市场 `github:NginxL/openmate` 就能搜到并安装。发布包放在 `packages/<id>/<版本>/` 里，带 sha256 和大小，发布后不再改动。
- **标准的 Persona 扩展**：每个扩展有自己的 `persona-extension.json`，程序通过 Persona 公开的扩展协议和连接器协议（标准输入输出上的 JSON 行）和 hub 通信。飞书扩展的连接器 id 是 `matefeishu`，工具名都以 `matefeishu_` 开头，可以和其他飞书扩展同时安装。
- **登录信息只存在 hub 里**：扫码得到的凭据通过连接器的 `secrets.*` 加密保存在 hub 的数据库里，从不写进文件或日志。
- 需要 Persona hub 构建号 202611110000 或更新。

## 飞书扩展能做什么

| 功能 | 说明 |
| --- | --- |
| 登录 | 扫码登录，支持飞书和 Lark（国际版），可以配置代理 |
| 收件箱 | 最近 3 天、最多 200 个会话，每个会话一条：私聊里没回的、群里 @我 没回的、群里的新消息。没有 @我 的群消息按规则直接判为噪音，不调用模型；我最后回复了就算结束。可以写自己的分拣规则 |
| 收件箱回复 | 以你本人身份发出，同一条回复只发一次 |
| 会话页 | 每页最多 50 条，可以往前翻，本地没有的旧消息会从飞书补拉；图片 4 MB 以内可以预览 |
| 知识库 | 按游标增量归档聊天记录，附件 20 MB 以内按需下载 |
| 只读工具 | `matefeishu_status`、`matefeishu_chats`、`matefeishu_messages`、`matefeishu_search`（搜人和群）、`matefeishu_docs`（通过管理端的 lark-cli 只读集成搜文档） |
| 写操作 | `matefeishu_send`、`matefeishu_send_dm`：每次都在手机上确认全文和发给谁；同一个请求只执行一次；结果不确定时绝不重发 |
| `/agent` | 你自己在飞书里发 `/agent …`，交给 Agent 处理并以你的身份回复到原话题（私聊里是引用回复），末尾带“AI 代答”标记；同一话题里接着发会进同一个会话；`/stop`、`/停止`、`/停` 可以停下。回复不经手机确认，每一条都记进 Persona 的“以你身份发出的消息”记录，所以安装时 Persona 会提示这个扩展会“以你的身份发消息” |
| 历史补收 | 首次登录和每次重连后补收最近会话里漏掉的消息 |

## 安装（Mac 上的管理端）

前提：管理端是一台 Mac（自带 `python3`，3.9 或更新）。扩展本身只用 Python 标准库；连接飞书靠 LarkAgentX，它和它的依赖要你**自己装**在一个 Python 里（本仓库不分发它）。

1. 安装 LarkAgentX，必须是这个扩展适配的提交 `97ad48e69e34bd8c78ee30f795b7c3759dd81659`（扩展启动时会检查，版本不对会在状态里说明）：

   ```sh
   git clone https://github.com/cv-cat/LarkAgentX.git ~/larkx/LarkAgentX
   git -C ~/larkx/LarkAgentX checkout 97ad48e69e34bd8c78ee30f795b7c3759dd81659
   python3 -m venv ~/larkx/venv
   ~/larkx/venv/bin/pip install -e ~/larkx/LarkAgentX
   ~/larkx/venv/bin/pip install python-socks   # 系统代理是 SOCKS 代理时需要（Mac 上常见）
   ```

   版本是从检出目录的 git 记录读出来的，所以请用 `pip install -e <检出目录>`（或者 `pip install git+https://github.com/cv-cat/LarkAgentX@97ad48e69e34bd8c78ee30f795b7c3759dd81659`）安装；别处构建的 wheel 看不出版本，会被拒绝。

2. 添加市场：

   ```sh
   personad extension market add github:NginxL/openmate
   ```

   也可以在 Persona 的 设置 › 扩展 › 管理市场 里添加。

3. 安装：

   ```sh
   personad extension search openmate            # 列出 market:<市场 id>/openmate-feishu
   personad extension install market:<市场 id>/openmate-feishu
   ```

   安装预览里会列出“以你的身份发消息”：这是 `/agent` 的回复（见上表），只有你在扩展设置里打开 `/agent` 后才会发生。

4. 关闭沙箱。非官方市场的扩展默认在沙箱里运行，读不到你家目录里的 LarkAgentX：

   ```sh
   personad extension sandbox openmate-feishu off
   ```

5. 设置 LarkAgentX 的 Python，写绝对路径（在扩展设置页填也可以）：

   ```sh
   personad extension config openmate-feishu larkx_python=$HOME/larkx/venv/bin/python
   ```

   hub 用系统的 `python3` 启动扩展，扩展读到这个设置后，会换成 `<larkx_python> -I` 重新启动自己（hub 发来的初始化信息里有 hub 的令牌，只通过管道交给新进程，不经过命令行、环境变量或文件）。路径不对、不能执行或 Python 低于 3.9 时，连接器状态会显示“未配置”和原因。

6. 可选：代理。默认用系统代理设置；要单独指定，就在扩展设置的“代理”里填 `http://` 或 `https://` 开头的地址（不支持在这里填 SOCKS），只对这个扩展生效。区域在“区域”里选飞书或 Lark（国际版），换区域后要重新扫码。

7. 扫码登录：设置 › 连接器 › 飞书（OpenMate） › 登录，用飞书手机 App 扫码并确认。登录信息只交给 hub 加密保存，扩展不往磁盘写任何凭据。

8. 可选：
   - 在扩展设置里打开 `/agent`，选它在哪台电脑、用哪个启动器运行；
   - 在“收件箱分拣规则”里用自己的话写分拣规则；
   - 文档搜索需要管理端开启 lark-cli 只读集成（管理端配置里的 `lark.cli.extensions: true`）。

## 风险，请先读

- **LarkAgentX 不是官方接口。** 它用的是逆向出来的飞书网页协议，随时可能失效，也可能触发账号风控（例如被要求重新登录或被限制使用）。如果用的是公司账号，请先自己判断是否合适。
- **与飞书 / Lark 官方无关。** OpenMate 是独立的开源项目，不是飞书或 Lark 的产品，也没有得到它们的认可；图标是本项目自己画的。
- **不分发 LarkAgentX。** 本仓库和发布包里都不包含 LarkAgentX，需要你自己安装并遵守它的条款。
- 所有发出的消息都以你本人的身份出现；写操作每次都需要你在手机上确认。`/agent` 只响应你自己发的消息，它的回复不再确认，但每一条都记进 Persona 的“以你身份发出的消息”记录。

## 开发

```text
core/openmate_core/            各平台共用：JSON 行协议、SQLite 存储、收件箱 / 会话页 / 归档、写操作日志、/agent、hub API 客户端、测试用 FixtureBackend
extensions/openmate-feishu/    飞书扩展：清单、启动脚本（按 larkx_python 重新启动）、LarkAgentX 后端、LarkAgentX 版本检查、助手说明、图标
scripts/package.py             打包、算 sha256、生成 persona-extensions.json（--check 检查是否最新）
scripts/test.sh                跑全部测试
tests/                         用假的平台后端和假的 hub 驱动完整的 JSON 行协议
```

- 只用 Python 3.9+ 标准库，没有第三方依赖；LarkAgentX 后端用的是你自己装的 LarkAgentX 和它的依赖。
- `scripts/test.sh` 跑全部测试（`python3 -I`）；`PYTHON=/usr/bin/python3 scripts/test.sh` 可以指定解释器。后端的测试用假的 LarkAgentX（`extensions/openmate-feishu/tests/fakelarkx`），不联网；`OPENMATE_LARKX_PYTHON=$HOME/larkx/venv/bin/python scripts/test.sh` 会再对真实的 LarkAgentX 跑冒烟测试（同样不联网、不登录）。
- 改了代码要发布时，先改扩展的版本号，再运行 `python3 -I scripts/package.py`，把新包和索引一起提交。已经发布的版本不可改动。
- `OPENMATE_BACKEND=fixture` 让扩展用内置的假后端运行（不连网、不需要账号），可以用来跑 Persona 自己的连接器一致性测试。
- **从零实现**：只依据 Persona（MIT）公开的协议文档和本仓库的功能清单编写，不含任何其他飞书桥接项目的代码或文字；LarkAgentX 只通过它的 API 调用，不复制它的代码。仓库里不放任何公司内容。

## English

OpenMate is a set of open-source (MIT) [Persona](https://github.com/nwverse/persona) extensions that bridge chat apps
into Persona: direct and group messages flow into the Inbox and knowledge base, and Persona's assistant and Agents can
answer in the chat app as you. Every message the assistant sends as you is confirmed on your phone first; only the
replies to your own `/agent` requests go out unconfirmed, and each of them is recorded in Persona's sent-as-user ledger
(the connector declares `sends_as_user`). The first extension is Feishu / Lark (`openmate-feishu`, connector
`matefeishu`); WeChat and DingTalk come later. The repository is itself a Persona extension market: add
`github:NginxL/openmate` in Persona. The Feishu link goes through LarkAgentX, an unofficial reverse-engineered web
protocol that you install yourself, at commit 97ad48e69e34bd8c78ee30f795b7c3759dd81659 (checked at start; install it
with `pip install -e <checkout>`, plus python-socks when the system proxy is SOCKS), in a Python you name in the
setting `larkx_python`: the extension re-executes itself there with `-I`. Turn the sandbox off for it on a Mac manager.
It may break or trigger account risk controls. OpenMate is not affiliated with Feishu or Lark and does not distribute
LarkAgentX. The core and the LarkAgentX backend are complete and tested (against a stand-in of LarkAgentX, plus an
offline smoke test against the real one); end-to-end checks with a real account are under way. Run the tests with
`scripts/test.sh` (standard library only).
