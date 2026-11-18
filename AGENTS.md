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
   - 跑 `python3 -I scripts/package.py`，再用 `--check` 校验通过，然后提交、推到 `main`。
   - 推送被拒，说明别人先推了：重新 fetch、rebase、取号、打包，再推。已经发布的包永远不改。
5. **部署**：刷新装了 OpenMate 的扩展市场：`GET /api/v1/extensions/-/markets` 找到它，再 `POST /api/v1/extensions/-/markets/<market>/refresh`。GitHub raw 有大约 5 分钟缓存，等扩展自动升到新版本。如果显示「需要批准」，就停在这一步，告诉用户去批准什么。
6. **验证**：在真实环境里看到效果，并记下证据。
   - 页面类改动：`POST /api/v1/extensions/<扩展>/-/ui/<页面>` 通过 hub 渲染，确认能打开、内容对、不超过 300 项。
   - 收发和数据类改动：用一条真实消息走一遍，或者查库确认。
   - 扩展日志在 manager 的日志里，查一下没有新的报错。
7. **收尾**：最后一行按 Persona 的交付格式写：
   `交付：<已上线并实测 | 停在 PR | 已合未上线 | 默认关待启用 | 不涉及上线>；卡点：…；谁来解：…`

## 规矩

- 这是公开仓库：提交身份用 `NginxL <8777777+NginxL@users.noreply.github.com>`，不写任何公司内部的内容、地址和名字。
- 只参考思路，不抄 lark-bridge、larkx、LarkAgentX、openlark 的代码；只能调用 LarkAgentX 的公开接口。
- 不打印、不提交任何密钥和凭据。
- 微信扩展和原生组件（`extensions/openmate-wechat/`、`native/`）由另一位维护者负责。只改那里的 PR，开出来以后在描述里写明，等对方审。
