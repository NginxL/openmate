# OpenMate

OpenMate 是一组开源（MIT）的 [Persona](https://github.com/nwverse/persona) 扩展，把聊天软件接进 Persona：私聊和群消息进收件箱和知识库，Persona 的助手和 Agent 可以在聊天软件里替你出面。助手以你身份主动发的每一条消息都要你先在手机上确认。有四种消息不再逐条确认，发出后会记进 Persona 的“以你身份发出的消息”记录（发出后扩展立刻退出等少数情况下，可能缺记一条）：你自己发起的 `/agent` 请求的回复；你勾选的群里有人真正 @ 你时的自动代答；飞书文档评论里 @ 你时的文档代答（默认关闭，要单独打开）；Persona 渠道在你指定的一个会话里的回答和问题（默认没有指定，一条都不发）。

第一个扩展是**飞书**（`openmate-feishu`）。第二个是**微信**（`openmate-wechat`，一期做 `/agent`、群里自动代答和收图片等媒体，见下文）。钉钉以后再做。

> **现状**：一期的平台无关核心（协议、存储、收件箱、会话页、知识库归档、扫码登录流程、写操作、`/agent`）和连接飞书的 LarkAgentX 后端（一期 1b）都已经完成，有完整的自动测试（后端用假的 LarkAgentX 测，另有不联网、不登录的真实 LarkAgentX 冒烟测试）。作者的账号每天在真实使用：飞书和微信的扫码登录、收发消息、`/agent`、群里自动代答、今天页都在用；文档代答还没有在真实账号上跑过。
>
> 飞书数据防泄漏（DLP）审核扣下的消息，飞书不会把正文发给这个客户端，OpenMate 也不会去绕过：被拦截的写明“被安全策略拦下”，审核中的过一会儿再读。

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
| 知识库选群 | 飞书声明了 Persona 的“历史”能力：在 Persona 的“设置 → 知识库选择 → 单项设置 → 添加聊天”里选来源“飞书（OpenMate）”，能搜到这个扩展存过的会话，添加后打开“全部保存”，这个群以后的消息就全部进知识库（不打开时只存 @我 和我发言前后的消息）。旧消息可以用 Persona 的历史导入补进来，只能补扩展登录以来存下的部分 |
| 只读工具 | `matefeishu_status`、`matefeishu_chats`、`matefeishu_messages`（可按时间段和关键词查，会自己往前补读历史）、`matefeishu_search`（搜人和群）、`matefeishu_docs`（通过管理端的 lark-cli 只读集成搜文档）、`matefeishu_starkmesh`（中转站额度：还剩多少号、多久重置、哪些号不可用，早报用它）、`matefeishu_media`（按消息 id 以现在登录的账号把图片或文件下载到本机临时目录（每次重新下载，25 MB 以内），返回本地路径；`matefeishu_messages` 和 /agent 上下文里带图片或文件的消息写着「看图：matefeishu_media message=…」） |
| 写操作 | `matefeishu_send`、`matefeishu_send_dm`：每次都在手机上确认全文和发给谁；同一个请求只执行一次；结果不确定时绝不重发 |
| `/agent` | 你自己在飞书里发 `/agent …`，交给 Agent 处理并以你的身份回复到提问的地方：话题里问的回到话题；话题里勾了“也发到群里”的，回复同样回到话题、也发到群里，并引用这条请求；群聊主界面问的（包括引用回复），在主界面引用回复这条请求；私聊里是引用回复，AI 标记放在回复最后一行（话题折叠时直接显示答案；设置“AI 标记位置”可改回第一行）；同一话题里接着发会进同一个会话；助手已经把活派给 Agent（建了待办、Agent 还在做）时，话题里你自己的追问直接转给那个 Agent，话题里回一句“已转给正在做这件事的 Agent”，结果照常经助手回到话题（写“问 Persona”“问助手”、任务结束了、Agent 不在了，就照旧交给助手；设置“话题里的追问直接转给干活的 Agent”可关）；`/stop`、`/停止`、`/停` 可以停下。请求上的表情说明状态：🤔 处理中，✅ 做完了，❌ 失败或超时（停下就撤掉 🤔；三个表情都能在设置里换）。中途进度只占一条消息、原地更新，最终结果另发一条新回复（会提醒你），那条进度消息标成已完成。回复不经手机确认，每一条都记进 Persona 的“以你身份发出的消息”记录，所以安装时 Persona 会提示这个扩展会“以你的身份发消息” |
| Persona 渠道 | 在 Persona 的“设置 › 渠道”里显示为“飞书（OpenMate）”（绑定、账号、开关跟着连接器走），不用装官方 larkx 也能把飞书当成 Persona 的一个渠道。只在你指定的一个会话里生效：扩展设置里的“Persona 渠道会话”填会话 id（问 Persona 助手“飞书里 xx 群的 chat id”即可，要是这个扩展收到过消息的会话），请用只有你自己的群；默认留空，留空时渠道什么都不收也不发。那个会话就是你和 Persona 的对话，相当于官方飞书机器人的私聊：你自己说的每一句都交给 Persona 统一的渠道流程（开头的 `/agent` 可写可不写；默认进主对话，可在“设置 › 渠道”改目标；`/new`、`/status` 等 Persona 渠道命令也能用），回答以你的身份回到提问处（话题里问的回到话题，主界面问的引用回复）；`/stop`、`/停止`、`/停` 打断渠道正在跑的对话。Agent 要你回答或批准时（渠道驱动的对话，或“设置 › 渠道”里打开了“所有 Agent 的问题”），问题以你的身份发到这个会话，直接回复就是作答；你在 App 等别处答了，这条问题会自动撤回。别的会话里的 `/agent`、进度原地更新、自动代答、话题追问转给 Agent 都和原来一样 |
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

## 微信扩展（一期）

`openmate-wechat`（连接器 `matewechat`）做这几件事，前两件和飞书的同名功能一致：

- **`/agent`**：你自己在微信任意会话里（手机或任何设备）发 `/agent …`，Agent 用你的全部权限处理，以你的身份把结果回复到原会话。要先在设置里打开“启用 /agent”。微信不能编辑消息、没有表情回应：接单时先引用你的消息发一句“接单提示”（默认“🤖 处理中...”，可改可关，不撤回），然后只回复一次最终结果，没有中途进度。
- **群里自动代答**：在 Persona 的“微信”页里勾选的群，有人真正 @ 你（不是手打名字）时，AI 用访客会话以你的身份引用并 @ 对方直接回复一次（不发“接单提示”，那句只给你自己的 /agent）；只 @所有人、私聊、公众号不代答；每群每小时、每天有上限。
- **Persona 渠道**：和飞书一样，在“设置 › 渠道”里显示为“微信（OpenMate）”，只在扩展设置“Persona 渠道会话”指定的一个会话里收发（默认留空，什么都不收也不发）。那个会话里你自己说的每一句都交给 Persona。微信撤回不了消息：你在 App 里答了的问题会留在会话里。
- **图片、语音、视频、文件**：收到后自动下载到扩展的数据目录（`media/`），消息文字里写着本地路径，`matewechat_messages` 和 `/agent` 的上下文里都能看到，图片可以直接给 AI 读：`[图片] 本地文件：/…/123.jpg`。语音存成 WAV（设置“语音转文字命令”后附上文字）；视频给封面、25 MB 以内的视频文件，装了 ffmpeg 时再抽 3 张截图；文件 25 MB 以内下载并保留原名。合并转发的聊天记录直接展开成文字。下载不了（过期、超过 25 MB、微信专有格式等）时保留占位并写明原因；`matewechat_media` 可以按消息 id 重新下载。本地副本 30 天后清理。下载用的 CDN 地址和密钥只存在扩展自己的数据库里，不进 hub、日志或消息文字。需要 matewechat-native 0.1.10 或更新；在此之前收到的消息没有留下下载信息，看不到内容。本地副本总共超过 2 GB 时先删最旧的。普通文件按消息里的大小和 MD5 校验。已知限制：微信 CDN 下载请求按微信协议以明文 HTTP 发送（与官方客户端相同，文件本身 AES 加密）；同时最多下载两个附件，网速极慢时后面的附件可能等下载名额超时，过后可用 `matewechat_media`（again=true）再取。

连接微信的是本仓库 `native/matewechat-native`（Rust）编译出的小程序，它用 [thisnick/agent-wechat](https://github.com/thisnick/agent-wechat)（MIT）的协议库 `wechat-protocol`（crates.io 2.7.0，只用公开接口）。扩展首次启动时从本仓库的 GitHub release 下载它，并按扩展里钉死的 SHA-256 校验；第三方许可全文见 `native/THIRD-PARTY-NOTICES.md`。只支持 Apple 芯片的 Mac，请在手机旁边的 Mac 上用，不要放在服务器上。和飞书扩展一样，装好后要在 hub 机器上关掉它的沙箱（`personad extension sandbox openmate-wechat off`）：沙箱里它打不开自己的数据库，也起不了连微信的程序。

## 风险，请先读

- **LarkAgentX 不是官方接口。** 它用的是逆向出来的飞书网页协议，随时可能失效，也可能触发账号风控（例如被要求重新登录或被限制使用）。如果用的是公司账号，请先自己判断是否合适。
- **与飞书 / Lark 官方无关。** OpenMate 是独立的开源项目，不是飞书或 Lark 的产品，也没有得到它们的认可；图标是本项目自己画的。
- **不分发 LarkAgentX。** 本仓库和发布包里都不包含 LarkAgentX，需要你自己安装并遵守它的条款。
- **微信扩展用的也是非官方协议。** 它以一台新电脑的身份登录你的微信：你已登录的电脑版微信可能会下线；账号被限制的可能无法排除，请自己判断。与腾讯官方无关，“微信”是腾讯的商标。
- 所有发出的消息都以你本人的身份出现；写操作每次都需要你在手机上确认。下面四种消息不再逐条确认，发出后会记进 Persona 的“以你身份发出的消息”记录（极少数情况下可能缺记一条）：
  - `/agent`：只响应你自己发的消息；
  - 群里自动代答：只在你勾选的群、有人真正 @ 你时，用访客会话（读不到你的私人知识库）回复，每群每天有上限；
  - 文档代答：飞书文档评论里 @ 你时回复，默认关闭，要在设置里单独打开；它经 lark-cli 发出，发之前核对 lark-cli 登录的账号和飞书登录的账号同名（目前只能比对显示名，同名的另一个账号拦不住）。
  - Persona 渠道：只发到你在设置里指定的“Persona 渠道会话”（默认没有，就一条都不发），内容是 Persona 对你在那里说的话的回答、Agent 要你回答或批准的问题和 Persona 的提示；在“设置 › 渠道”里打开“通知”“同步”后还有推送。

## 开发

```text
core/openmate_core/            各平台共用：JSON 行协议、SQLite 存储、收件箱 / 会话页 / 归档、写操作日志、/agent、hub API 客户端、测试用 FixtureBackend
extensions/openmate-feishu/    飞书扩展：清单、启动脚本（按 larkx_python 重新启动）、LarkAgentX 后端、LarkAgentX 版本检查、助手说明、图标
extensions/openmate-wechat/    微信扩展：清单、启动脚本、matewechat-native 后端（JSON 行）、下载与 SHA-256 校验、助手说明、图标
native/matewechat-native/      连接微信的 Rust 程序：扫码登录、在线收消息、发文字（真实 @ 和引用）、查名字、下载媒体
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
answer in the chat app as you. Every message the assistant sends as you is confirmed on your phone first. Four kinds
of message go out unconfirmed, and are recorded in Persona's sent-as-user ledger after they go (one can be missing when
the extension exits right after sending; the connector declares `sends_as_user`):
the replies to your own `/agent` requests; the automatic answers when someone really @-mentions you in a group you
marked (a guest conversation, capped per group and day); and, off unless you turn it on, the answers to a Feishu
document comment that @-mentions you (through lark-cli, only while lark-cli's account has the same display name: another account of the same name
is not told apart yet); and what Persona's channel (listed under Settings › Channels) says in the one chat you
name in the setting `channel_chat` (your conversation with Persona, as a bot's direct chat is): the answers to what you say there, and the questions and
approvals an Agent waits on (taken back in Feishu once answered elsewhere); no chat is named by default, and then
nothing is sent. The first extension is Feishu / Lark (`openmate-feishu`, connector
`matefeishu`); the second is WeChat (`openmate-wechat`, connector `matewechat`: phase one is `/agent`, answering
real @-mentions in the groups you mark, and pictures, voice notes, videos, files and forwarded chat records fetched
into local files the assistant can read, over this repository's `native/matewechat-native`, built on thisnick/agent-wechat's
MIT `wechat-protocol` library, downloaded from this repository's release and pinned by SHA-256; unofficial protocol,
Apple-silicon Macs only); DingTalk comes later. The repository is itself a Persona extension market: add
`github:NginxL/openmate` in Persona. The Feishu link goes through LarkAgentX, an unofficial reverse-engineered web
protocol that you install yourself, at commit 97ad48e69e34bd8c78ee30f795b7c3759dd81659 (checked at start; install it
with `pip install -e <checkout>`, plus python-socks when the system proxy is SOCKS), in a Python you name in the
setting `larkx_python`: the extension re-executes itself there with `-I`. Turn the sandbox off for it on a Mac manager.
It may break or trigger account risk controls. OpenMate is not affiliated with Feishu or Lark and does not distribute
LarkAgentX. The core and the LarkAgentX backend are complete and tested (against a stand-in of LarkAgentX, plus an
offline smoke test against the real one), and in daily use on the author's own accounts (sign-in, messages, `/agent`,
group answers, the Today page; the document answers have not run on a real account yet). Messages Feishu's data-loss
policy holds back never reach this client and are not worked around: a blocked one says so. Run the tests with
`scripts/test.sh` (standard library only).
