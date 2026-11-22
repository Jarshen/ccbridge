# ccbridge

在 Windows 上把一个 CloudCLI 的 Claude Code 聊天和一个 Codex Desktop 聊天配成一组。两端各自保留原生会话、历史和工具审批，可以互发任务和结果。界面和发信命令中把 Codex 一侧称为 Astra。

## 首次使用

1. 在 **Codex Desktop** 和 **CloudCLI Desktop** 各完成一个测试回合。CloudCLI 的本地服务包须为 **1.37.9**，Claude 能正常回复即可。桥复用它的 Electron、Claude Agent SDK 和数据库驱动。Codex 需要支持 `queue --thread … --message …` 的 CLI，桥优先查找 Desktop 安装的版本，也支持 PATH 中的版本。已验证的 CLI 为 `0.171.0-alpha.13`。
2. 准备 **Python 3（含 Tk，已测 3.14）**。将本仓库克隆或解压到一个可写的固定目录。ccbridge 的 Python 端使用标准库。TaskQuay、隧道、单独的 Node 服务均可省去。
3. 等 CloudCLI 当前任务结束，正常退出它的普通本地服务。双击 **双会话工作台.cmd**，等首页显示服务就绪。如果已有服务占用端口，检查 CloudCLI 的 *Keep Local Server Running After Quit* 设置，再重新打开工作台。
4. 在 **会话配对** 中选择两个测试聊天，确认并保存。新配对会向两端发送一次分工和发信说明。**我的配对** 可重新打开原聊天，也可以保存多组独立配对。[默认分工](daily/pairing-brief.md)
5. 在 Claude 输入 **发给 Astra：请把 17 加 25，算完把总数发给 CC。** 在两个原聊天核对题目和回信，并在面板查看两个方向的记录。队列接收表示已经提交，收件端产生的结果可用来核对任务是否执行。

关闭窗口后，后台仍可工作。等任务完成再双击 **关闭.cmd** 即可停止该副本启动的带桥服务。配对、信件和日志保存在 `daily/data/`。换绑遇到“结果待核对”时，先到原聊天核对，再填写处理说明。

## 正在工作的 Desktop 怎样收信

普通配对使用原生队列。需要 Claude 在 Desktop 忙时插话时，可以为指定配对启用 MCP 收件箱。在 Windows 双窗口实测中，工作进程仍在运行时 Desktop 就能领取正文，随后在同一回合采用它。收件箱需要启用 Code Mode，且宿主的 MCP 调用元数据包含线程和回合身份。

从仓库根目录打开 PowerShell，在已登录的 Desktop 用户下执行。将最后一行的配对号、线程 UUID 替换成面板中这一组的值。这里使用的 `codex` 可以换成 Desktop 所带 CLI 的绝对路径。

```powershell
$python = py -3 -c "import sys; print(sys.executable)"
$inbox = (Resolve-Path .\daily\paired_inbox.py).Path
codex mcp add daily_pair_inbox -- $python -B -X utf8 $inbox serve
& $python $inbox enable --binding <配对号> --thread <Desktop线程UUID>
```

在能看到新工具的 Desktop 回合中，让 Astra “在后台 Code Mode cell 中先 yield_control，再用 daily_pair_inbox.watch 等来信，ready 时 notify，继续做前台任务，收到通知后在当前回合 take 正文”。每次 watch 最多等待 45 秒，可在当前回合续订。启用后，该配对的单向来信留在收件箱，处于空闲的 Desktop 需要有回合开始监听才能领取；已经定向给某个回合的信只由该回合领取。前台进程可以继续运行。

## 创建搭档和恢复

- **Desktop → Claude** 可在面板选择未配对的 Desktop，新建同目录 Claude；也可以在目标 Desktop 线程里调用 `daily/provision.py create --id <UUID> --title <标题>`。
- **Claude → Desktop** 可在 Claude 输入 **新建 Desktop 配对**，在打开的 Desktop 草稿中发送预填首信。桥核对项目目录和申请号后绑定。可在 Claude 输入 **取消 Desktop 配对申请**。
- 需要核对前端版本时运行 `py -3 daily/launcher.py --verify-frontend`。新安装默认打开随源码校验的新版首页。手动回滚可用 `--rollback-frontend`，重新启用可用 `--promote-frontend <发布的SHA256>`。[发布摘要](daily/release-frontend.json)

## 开发验收

在 Windows PowerShell 的仓库根目录运行以下命令。Python 测试包含 Tk 界面，桌面会短暂出现测试窗口。JS 测试使用 CloudCLI 的 Electron 以匹配原生 SQLite 驱动，并使用临时数据库和测试会话。

```powershell
$cases = Get-ChildItem daily/test_*.py | ForEach-Object { "daily." + $_.BaseName }
py -3 -B -X utf8 -m unittest @cases
$env:ELECTRON_RUN_AS_NODE = '1'
$env:DAILY_PYTHON = py -3 -c "import sys; print(sys.executable)"
$electron = Join-Path $env:LOCALAPPDATA 'Programs/@cloudcli-aicloudcli-desktop/CloudCLI.exe'
& $electron --test daily/test-worker.mjs daily/test-provision.mjs
```

双击入口也支持从终端运行 `& '.\双会话工作台.cmd' --acceptance`，在独立本机端口和空索引上检查首次启动。日常配对和此模式各用独立数据库。验收结束后运行 `py -3 daily/launcher.py --acceptance --stop`。
