# 给在这个仓库里干活的 Agent

OpenMate 是 Persona 的扩展（飞书、微信）。这个仓库的任务**做到上线并实测才算完成**，不是开了 PR 就停。
仓库归用户本人所有。下面的合并、发版、部署到用户自己的 Persona，用户已经授权，不用再问。
只有这几件事要停下来等用户：新版本多要了权限（Persona 里显示「需要批准」）、要以用户身份往外发消息、要默认打开一个新功能。

## 交付流程

1. **开发**：在新分支上改，跑 `bash scripts/test.sh`，三组测试全部通过。改了行为就补测试，并确认新测试在旧代码上会失败。
2. **PR**：推分支，向 `main` 开 PR，用中文写清做了什么、怎么验证的、没做什么。
3. **审查**：本机有 codex CLI 时，用它只读审查这次的 diff（`codex exec -m gpt-6.2-astra -c 'model_reasoning_effort="ultra"' -s read-only --skip-git-repo-check --ephemeral …`）。修掉真实问题，修完再审一轮，确认没有带进回归。
4. **合入和发版**：
   - `git fetch`，rebase 到最新的 `main`。
   - 版本号取 `main` 上的当前版本加 1。只改了一个扩展就只升那个扩展；改了 `core/`，两个扩展都要升。
   - 跑 `python3 -I scripts/package.py`，再用 `--check --published origin/main` 校验通过，然后提交。
   - 先把这些提交强推回 PR 的分支（`git push --force-with-lease origin HEAD:<分支>`），再把**同一批提交**快进推到 `main`（`git push origin HEAD:main`）。提交号不变，GitHub 才会把 PR 标成已合并，Persona 的待办才会走到「合入」。不要关 PR、不要在 GitHub 上另做 squash 或 rebase 合并。
   - 推 `main` 被拒，说明别人先推了：重新 fetch、rebase、取号、打包，再按上一步推一遍。已经发布的包永远不改。
   - 推完用 `gh pr view <PR> --json state` 确认是 `MERGED`。
5. **部署**：Persona 不给 Agent 刷新扩展市场的权限（只有完整凭据能调），不要用别的办法绕。hub 每 6 小时（再加 0–30 分钟）自己读一次市场，读到新版本就在空闲时自动升级。
   - 用后台命令等升级：每 5 分钟看一次正在运行的扩展版本（`ps -axwwo command= | grep -oE "openmate-(feishu|wechat)/[0-9]+\.[0-9]+\.[0-9]+/" | sort -u`（结尾的 `/` 让它只认安装目录，不认别的命令里写到的版本字样），只取版本号，不要打印完整命令行），到了你发的版本或更高就算升上去了，最多等 7 小时。manager 日志里的 `upgraded itself` 不一定每次都有，不要只看它。等的时候不要占着别的事。
   - 日志里出现「需要批准」（needs permission）：停在这一步，告诉用户要批准什么。
   - 7 小时还没升上去：交付行写「已合未上线」，卡点写「hub 还没读到新版本」。
6. **验证**：在真实环境里看到效果，并记下证据。
   - 页面类改动：`POST /api/v1/extensions/<扩展>/-/ui/<页面>` 通过 hub 渲染，确认能打开、内容对、不超过 300 项。
   - 收发和数据类改动：用一条真实消息走一遍，或者查库确认。
   - 扩展日志在 manager 的日志里，查一下没有新的报错。
7. **收尾**：最后一行按 Persona 的交付格式写：
   `交付：<已上线并实测 | 停在 PR | 已合未上线 | 默认关待启用 | 不涉及上线>；卡点：…；谁来解：…`

## 规矩

- 这是公开仓库：提交身份用 `NginxL <8777777+NginxL@users.noreply.github.com>`，不写任何公司内部的内容、地址和名字。
- 只参考思路，不抄 lark-bridge、larkx、LarkAgentX、openlark 的代码；只能调用 LarkAgentX 的公开接口。
  - 唯一例外（用户 2026-11-24 同意）：`larkagentx.py` 的 `_HeaderedRequests` / `newer_post`。在开了话题模式的群里发新消息被飞书以 5801 拒绝时，只在那一次重发里把请求头 `x-command-version` 换成 `NEWER_COMMAND_VERSION`。不要扩大它的范围，也不要去掉它。
- 不打印、不提交任何密钥和凭据。
- 微信扩展和原生组件（`extensions/openmate-wechat/`、`native/`）也按上面的流程一路做完，不用等别人审。改了 `native/` 的 Rust 代码，本机跑 `cargo test --locked`（在 `native/matewechat-native` 里），CI 的 Rust 测试也要通过。
- 实测时以用户身份发消息：飞书只在「OpenMate 测试」群里发；微信还没有测试群，要发微信消息先停下来问用户。
