# ccbridge

在 Windows 上把一个 CloudCLI 的 Claude Code 聊天和一个 Codex Desktop 聊天配成一组。两端各自保留原生会话、历史和工具审批，可以互发任务和结果。界面和发信命令中把 Codex 一侧称为 Astra。

## 首次使用

要核对本机准备情况，从工作台根目录在 PowerShell 运行 `& '.\双会话工作台.cmd' --doctor`。它检查 Python/Tk、CloudCLI Desktop 及服务包、两端会话索引、面板校验和支持 queue 的 Codex CLI。附加 `--project "项目目录"` 可检查该目录的收件 hook 配置；信任状态以 Codex 的 `/hooks` 为准。自检同时显示独立 Claude CLI 能发现的交互终端数。

1. 在 **Codex Desktop** 和 **CloudCLI Desktop** 各完成一个测试回合。CloudCLI 的本地服务包须为 **1.37.9**，Claude 能正常回复即可。桥复用它的 Electron、Claude Agent SDK 和数据库驱动。Codex 需要支持 `queue --thread … --message …` 的 CLI，桥优先查找 Desktop 安装的版本，也支持 PATH 中的版本。已验证的 CLI 为 `0.171.0-alpha.13`。
2. 准备 **Python 3（含 Tk，已测 3.14）**。将本仓库克隆或解压到一个可写的固定目录。ccbridge 的 Python 端使用标准库。TaskQuay、隧道、单独的 Node 服务均可省去。
3. 等 CloudCLI 当前任务结束，正常退出它的普通本地服务。双击 **双会话工作台.cmd**，等首页显示服务就绪。如果已有服务占用端口，检查 CloudCLI 的 *Keep Local Server Running After Quit* 设置，再重新打开工作台。
4. 在 **会话配对** 中选择两个测试聊天，确认并保存。新配对会向两端各发送一次分工和发信说明。桥优先读取接收方项目目录中的同名文件，缺省时读取工作台根目录的版本。**我的配对** 可重新打开原聊天，也可以保存多组独立配对。[Claude 分工](配对分工-Claude.md) · [Astra 分工](配对分工-Astra.md)
5. 在 Claude 输入 **发给 Astra：请把 17 加 25，算完把总数发给 CC。** 在两个原聊天核对题目和回信，并在面板查看两个方向的记录。队列接收表示已经提交，收件端产生的结果可用来核对任务是否执行。

关闭窗口后，后台仍可工作。等任务完成再双击 **关闭.cmd** 即可停止该副本启动的带桥服务。配对、信件和日志保存在 `daily/data/`。换绑遇到“结果待核对”时，先到原聊天核对，再填写处理说明。

## 正在工作的 Desktop 怎样收信

新配对自动选择投递方式。当前回合有监听时优先交给监听；有近期工具活动且项目已启用收件 hook 时，可在工具结束后领取；没有接收方时回落到原生队列。

- **在工具结束时收件**：在「我的配对」选中目标，点击 Astra 下方的「设置项目收件」。首次按提示在 Codex CLI 的 `/hooks` 中审核工作台的两条命令，在 Desktop 新回合使用。解除该目录最后一组配对时，可以选择停用这两条命令。
- **发信后等对方回复**：在已配对的 Desktop 活动回合中运行 `py -3 daily/relay.py send --binding <配对号> --id <本封UUID> --text "请核对结果并回信" --follow`。同一事务登记发信和监听；命令按 JSON 行输出本回合领取的来信。在其他项目使用脚本的绝对路径。
- **用 MCP 后台监听**：以 Desktop 用户身份在工作台根目录注册下面的收件工具。在能看到工具的回合中，用后台 Code Mode cell 调用 `watch`，收到编号后在同回合 `take`，然后继续监听。已有严格收件箱保留原策略；若要让某个旧配对使用自动投递，可运行 `py -3 daily/paired_inbox.py enable --auto --binding <配对号> --thread <Desktop线程UUID>`。

```powershell
$python = py -3 -c "import sys; print(sys.executable)"
$inbox = (Resolve-Path .\daily\paired_inbox.py).Path
codex mcp add daily_pair_inbox -- $python -B -X utf8 $inbox serve
```

## 发件与回执

日常通信只发单向信。即使发送方沿用旧命令 `--mode required --wait 60`，也会立即返回登记编号。接收方需要传回结论时另发一封；`--follow` 只监听后续来信。升级前已经登记的正式请求继续按原编号结算。

「消息记录」的详情会给出下一步，命令行查回执时对应 JSON 中的 `next_action`。例如原生入队后可以用原编号查询进展；发送或监听输出结果不明时，先核对原聊天。使用 `send --follow` 时，桥先为该回合保留唯一领取权，正文成功写入监听输出后才登记领取回执。输出中断会留下待核对记录，原信不会重复投递。

在 CloudCLI Chat 中让 Claude 调用 `mcp__daily_pair__send` 可取得登记编号。也支持助手答复末行的 `ASTRA {"text":"正文"}`；同一运行中通过 steer 追加任务时，各次完成的答复分别登记。独立 Claude CLI 可以从配对项目目录运行 `py -3 <工作台>/daily/relay.py cc-send --binding <配对号> --native-cc <Claude原生ID> --id <本封UUID> --text-file 正文.txt`。正文文件使用 UTF-8，同一封重试沿用原 ID。

用「消息记录 → 按编号查找」查询信件或发送错误。**已登记**表示桥已存信，**已入队**表示 Desktop 已接受原生提交，**已送达**可查看目标回合或领取依据。要判断任务是否完成，在接收方聊天核对实际结果。

## 创建搭档和恢复

- **Desktop → Claude** 可在面板选择未配对的 Desktop，新建同目录 Claude；也可以在目标 Desktop 线程里调用 `daily/provision.py create --id <UUID> --title <标题>`。
- **Claude → Desktop** 可在 Claude 输入 **新建 Desktop 配对**，在打开的 Desktop 草稿中发送预填首信。桥核对项目目录和申请号后绑定。可在 Claude 输入 **取消 Desktop 配对申请**。
- 需要核对前端版本时运行 `py -3 daily/launcher.py --verify-frontend`。新安装默认打开随源码校验的新版首页。手动回滚可用 `--rollback-frontend`，重新启用可用 `--promote-frontend <发布的SHA256>`。[发布摘要](daily/release-frontend.json)

## 单侧换新

需要更换配对中的一端时，可记录已确认的方向、证据和未完事项，由新会话核对材料后接管；另一端沿用原聊天。只有交接状态为 `committed` 才算完成。[单侧换新说明](daily/换新说明.md)

## 开发验收

在 Windows PowerShell 的仓库根目录运行以下命令。Python 测试包含 Tk 界面，桌面会短暂出现测试窗口。JS 测试使用 CloudCLI 的 Electron 以匹配原生 SQLite 驱动，并使用临时数据库和测试会话。

```powershell
$cases = Get-ChildItem daily/test_*.py | ForEach-Object { "daily." + $_.BaseName }
py -3 -B -X utf8 -m unittest @cases
$env:ELECTRON_RUN_AS_NODE = '1'
$env:DAILY_PYTHON = py -3 -c "import sys; print(sys.executable)"
$electron = Join-Path $env:LOCALAPPDATA 'Programs/@cloudcli-aicloudcli-desktop/CloudCLI.exe'
& $electron --test daily/test-handoff.mjs daily/test-worker.mjs daily/test-provision.mjs
```

双击入口也支持从终端运行 `& '.\双会话工作台.cmd' --acceptance`，在独立本机端口和空索引上检查首次启动。日常配对和此模式各用独立数据库。验收结束后运行 `py -3 daily/launcher.py --acceptance --stop`。
