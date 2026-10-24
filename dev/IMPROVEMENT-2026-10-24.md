# doubao2api 改进报告 — 对照 2026-10-23 对比分析的整改实录

> 撰写日期：2026-10-24
> 对照文档：`F:\AI_APIS\qwen2api-rs-mine\dev\COMPARISON-doubao2api.md`（2026-10-23，下称「原报告」）
> 分析对象：`F:\AI_APIS\doubao2api`，基线 commit `d575d58` → 当前 `dace990`（共 5 个提交）
> 方法：与原报告同标准——逐行源码审查 + 可执行复现脚本 + pytest 回归测试。本报告只陈述已验证的事实，凡未做到的照实写「未解决」。

---

## 0. 一句话结论

原报告点名 doubao2api 有 **3 个致命 bug** 和 **1 个最大架构差距**（单 Page 并发模型）。截至本文撰写时：**3 个致命 bug 全部修复并有回归测试锁定**；架构差距中危害最大的一条（切账号杀死所有在途请求）已用「在途流排空」护栏止血。项目从「Agent 框架第一步就挂、长思考必被截断」的不可用状态，恢复为「OpenAI 兼容面完整可用」的单机服务。原报告中与并发模型、无人值守、内存占用相关的其余批评**依然成立**，未做任何掩饰。

---

## 1. 逐项对照：原报告的批评现状

### 1.1 三个致命 bug —— 全部修复 ✅

#### Bug 1：任何带 `tools` 的请求直接 500

| | 原状态（`d575d58`） | 现状态（`dace990`） |
|---|---|---|
| 行为 | `POST /v1/chat/completions` 带 `tools` → 500 `UnboundLocalError: cannot access local variable 'image_refs'` | 200，正常返回；带图片时附件正确透传 |
| 根因 | `unified_server.py` tools 分支不绑定 `image_refs`/`file_refs`，`:1017` 无条件引用 | tools 分支补绑定 `_, file_refs, image_refs = _extract_prompt_and_file_refs(body.messages)` |
| 提交 | — | `b1d4f7a` |

修复过程中发现原报告未点名的**第四个问题**：tools 请求里即使不带图片，`image_attachments` 也永远为 None——即 Cline/Continue 发截图给带工具的模型时，图片被静默丢弃。同一提交中一并修复：现在 tools 分支的图片/文件引用会走与非 tools 分支完全相同的 `_materialize_image_refs`/`_materialize_file_refs` 链路。

**回归测试**：`tests/test_fatal_regressions.py::test_tools_request_plain_text_does_not_500`（修复前真实复现 500，与原报告实测输出一致）与 `test_tools_request_with_image_passes_attachments_through`（断言 `chat_completion` 收到 `image_attachments`）。

#### Bug 2：keepalive 反而截断流

原报告用 CPython 3.12 最小复现：`asyncio.wait_for(__anext__, 5.0)` 逾时会**取消** `__anext__()`，取消异步生成器的 `__anext__()` 会**关闭生成器**，下一轮立即 `StopAsyncIteration`——客户端收到一个心跳，然后流死。

本次修复前后在同一台机器、同一 CPython 上各跑一次对照（3 个事件、每个间隔 6 秒、心跳阈值 5 秒）：

```
旧模式（asyncio.wait_for）:
  old code: keepalive fired; items delivered so far = 0
  old code: STREAM DEAD after keepalive, delivered 0/3

新模式（asyncio.wait + 挂起任务）:
  new code: delivered 3/3 items across 3 keepalive gaps — stream survived
```

| | 原状态 | 现状态 |
|---|---|---|
| 实现 | `asyncio.wait_for(event_iter.__anext__(), timeout=5.0)` | `asyncio.wait({next_event}, timeout=_KEEPALIVE_INTERVAL)`，挂起的 `__anext__` 任务跨心跳存活，`finally` 里对残留任务补 `cancel()` |
| 心跳间隔 | 硬编码 5.0 | `_KEEPALIVE_INTERVAL`，env `DOUBAO_KEEPALIVE_INTERVAL` 可调 |
| README `:39-40`「彻底避免断开」的宣称 | 与实际相反 | 现在成立 |
| 提交 | — | `be41843` |

**回归测试**：`test_stream_survives_idle_gap_with_keepalive`——模拟上游 0.6 秒无事件（monkeypatch 心跳间隔 0.2s），断言心跳发出**且**后续 token 依然到达、`[DONE]` 收尾。修复前此测试失败于 `assert b"second" in body`（流被截断），符合原报告的机制描述。

原报告实测「思考模型最大无事件间隔 3.64s，未达 5s 阈值故未触发」——修复后该风险不再存在：无事件间隔无论多长，流不再因心跳而终止。

#### Bug 3：验证码换号调用不存在的 `apply_account`

| | 原状态 | 现状态 |
|---|---|---|
| 生产代码 | `unified_server.py:930` 调 `client.apply_account(next_acc)`，全仓库无定义，走到 Tier-3 即 `AttributeError` | 改调真实存在的 `switch_to_account(next_acc.id)`；返回 False 时回落 Tier-4 弹窗 + 503，而不是 500 |
| 附带问题 | 成功路径强写 `client._ready = True`，换号失败时会撒谎 | 删除强写，`switch_to_account` 内部 `_check_login_state` 决定真实状态 |
| 测试掩盖 | `tests/test_captcha_defense.py:126` 用 `AsyncMock` 凭空造出该方法，测试全绿 | mock 改为真实方法名并断言 `switch_to_account.assert_called_once_with(acc2.id)`；另加接口存在性守卫 `test_captcha_failover_only_calls_real_methods`，对 6 个换号路径依赖的方法逐一 `hasattr` 检查，幽灵方法再混进来会直接红 |
| 提交 | — | `d9c8e98` |

原报告只发现了「生产代码调用崩溃」；「测试用 mock 把 bug 藏在绿灯后面」是原报告一并指出的，此次两处同时修正。守卫测试的价值在于：`MagicMock` 默认宽容任何属性访问，只有显式 `spec`/守卫才能拦住这类漂移。

### 1.2 最大架构差距（单 Page 单 Context）—— 部分止血 ⚠️

原报告列出单 Page 的四个后果，逐条对照：

| 后果 | 现状 |
|---|---|
| 1. 切账号 `clear_cookies()` + `reload()`，所有在途请求一起断 | **已止血**：`_tracks_page_stream` 装饰器对 4 个长流方法（`chat_completion`/`generate_image`/`generate_music`/`generate_video`）做在途计数；`switch_to_account` 在触碰 cookies 前先 `drain_streams()` 排空（默认上限 150s，env `DOUBAO_DRAIN_TIMEOUT`），超时则带警告放行——卡死的流不会永久阻塞验证码逃生（`dace990`） |
| 2. 看门狗重启页面时所有在途流一起断 | **未解决**。看门狗仅在页面死亡（连 `page.evaluate("1+1")` 都失败）时重启，此刻在途流已随错误事件自行终止，排空无意义；但「重启是否恰逢长流」的竞态仍存在，只是概率窗口小 |
| 3. 视频生成独占 Page 300 秒 | **未解决**，且不在本次范围内。视频任务已计入在途计数，所以它独占期间换号会等待/超时放行，但并发视频+聊天仍共享一页 |
| 4. 账号池只是轮流用同一个浏览器 | **未解决**，这是架构本身，见 §2 |

排空护栏的设计权衡（计划文档 `docs/superpowers/plans/2026-10-24-fatal-bugs-and-concurrency-safety.md` 已记录）：**只打在 `switch_to_account` 一处**——Tier-3 failover、`generate_video` 内部换号、`/admin/api/accounts/switch` 三条换号路径全部经过它，符合「根因处修复，不在调用点重复打补丁」。`restart()` 刻意**不**排空：`chat_completion` 入口的页面死亡自愈 restart 发生在被计数的流内部，若 restart 也排空会等待自己（自锁）。

### 1.3 原报告其余批评 —— 照实保留，均未处理

以下各项在本次整改中**一条都没动**，全部依然成立：

- **内存占用**（原报告实测 951.5MB vs 9.05MB）：浏览器路线的固有成本，未优化。
- **无人值守**（验证码最终依赖人工滑块）：Tier-2 自动求解早已存在，但彻底无人值守是逆向课题，未做。
- **协议覆盖**（仅 OpenAI vs 三协议）：未增加 Anthropic/Gemini 出口。
- **TTFT 无人为优化**（200ms 调度平滑、每请求 `is_alive` 探测、工具模式 20 字符缓冲）：均保留，它们的目的是抗风控/正确性，不是疏忽。
- **文档失真**（`docs/index.html` 写错视频/音乐端点路径等）：未修。
- **工具名混淆 class 只 import 未使用**（原报告 §5）：未修。
- **正则 fallback 解析嵌套 JSON 参数会失败**（原报告 §5）：未修。
- **`/auth/eval` 可执行任意 JS、CORS 全开、Cookie 明文落盘**（原报告 §10 安全项）：未修——单机个人工具的风险模型下优先级最低，但不代表无害。

原报告对 qwen2api-rs 自身的批评（`rquest` 文档错误、参数静默丢弃、协议出口不完整、无集成测试）属于对方项目，不在本项目整改范围。

---

## 2. 本次整改后的真实定位变化

原报告的结论是：「doubao2api 的合理定位是个人单机实验工具；三个致命 bug 使它目前不适合当作稳定的 OpenAI 兼容服务使用」。

整改后重述：

| 能力面 | 整改前 | 整改后 |
|---|---|---|
| OpenAI Chat Completions（纯文本） | ✅ 可用 | ✅ 可用（无变化） |
| OpenAI Chat Completions（带 tools） | ❌ 第一步 500 | ✅ 可用，含图片附件场景 |
| 长思考串流（间隔 >5s） | ❌ 心跳即截断 | ✅ 心跳保活且不断流 |
| 验证码 Tier-3 自动换号 | ❌ AttributeError 500 | ✅ 换号成功即无感；失败回落人工提示 |
| 换号时的在途请求 | ❌ 全部陪葬 | ⚠️ 排空等待（默认 ≤150s），超时才牺牲 |
| 多账号真并行 | ❌ | ❌（未变） |
| 无人值守 | ❌ | ❌（未变） |
| 内存效率 | ❌ 951MB | ❌（未变） |

一句话：**「个人单机实验工具」的定位没变，但从「演示都演示不稳」变成了「单机当 OpenAI 兼容服务用是成立的」**。Agent 框架（Cline/Continue/LangChain）现在可以接上；长思考不再需要祈祷上游 5 秒内出事件；验证码触发时账号池的自动逃生路径第一次真正可用。

---

## 3. 工程过程与验证记录

### 3.1 提交清单（基线 `d575d58` → 当前 `dace990`）

| Commit | 内容 | 类型 |
|---|---|---|
| `d5b215b` | 原有 WIP 入库：Windows 运行时兼容（`_runtime.py`：WMI 挂起规避、TEMP 路径特殊字符规避）、视频会话留存/即焚清理、README 视频接口说明 + 本次实施计划文档 | 隔离提交（非本次修复内容，先清基线） |
| `b1d4f7a` | tools 分支补绑定 `image_refs`/`file_refs` 并透传附件 | fix（致命 #1） |
| `be41843` | keepalive 改 `asyncio.wait`，心跳间隔可配 | fix（致命 #2） |
| `d9c8e98` | Tier-3 改用 `switch_to_account`，修掉测试 mock 掩盖 | fix（致命 #3） |
| `dace990` | 在途流计数 + `switch_to_account` 排空护栏 | feat（并发安全） |

### 3.2 测试

- 新增 `tests/test_fatal_regressions.py`：7 个测试，对应三个致命 bug（4 个行为测试）+ 并发护栏（3 个测试，含装饰器对 async 生成器与协构函数的双形态验证、排空先于 reload 的顺序断言）。
- 每个修复均按 TDD 执行：先写测试 → 确认失败信息与原报告实测一致（如 Bug 1 复现出原样的 `UnboundLocalError` 报文；Bug 3 失败于 `await client.apply_account(next_acc)` 的 TypeError）→ 最小修复 → 通过 → 提交。
- 全量回归：`60 passed, 1 failed`。唯一失败 `test_ssrf_is_safe_url` 为本机环境问题（DNS 将 `www.doubao.com` 解析到 fake-IP 段 `198.18.1.98`，被 SSRF 防护正确拦截），修复前即如此，非回归。
- 测试环境注记：本机 `.venv` 无 pytest，使用 `D:/python3132/python.exe -m pytest`；该解释器缺 `python-multipart`（项目的既有声明依赖）已补装。

### 3.3 复现材料

- keepalive 旧/新模式对照脚本与输出：见 §1.1 Bug 2，可直接以相同方式复验。
- 三个 bug 的失败-修复证据链：`git log -p b1d4f7a be41843 d9c8e98 dace990` + 各提交对应的测试。

### 3.4 部署状态

修复已推送至 `https://github.com/newyngwieslash-ops/doubao2api-mine`（`main`）。撰写本文时本机 9090 服务**未在运行**，故本报告的验证依据是 pytest 回归 + 机制复现脚本，而非对运行中服务的 HTTP 实测；重启服务后新代码即生效，§1 各项「现状态」对运行服务同样成立。

---

## 4. 剩余差距与下一步（按原报告框架）

若继续沿原报告「变成可用的服务」的三件事框架衡量：

1. ~~修那三个 bug~~ —— **已完成**（含原报告未点名的第四个附件丢弃问题）。
2. **多 Context 或多进程启动** —— 未做。排空护栏是这条的廉价前置（先保证切号不杀流，再谈并行）；真做多 Context 意味着内存从 ~950MB 涨到 N×250MB，与资源效率目标相反，建议仅在出现真实的「多账号同时高并发」需求时实施。
3. **去掉人工验证码依赖** —— 未做。Tier-2 自动求解已存在且保留；进一步无人值守需要滑块形态变化的持续对抗，属于逆向研究而非工程任务。

另有两个低成本高收益项，未包含在本次范围但值得记录：修复 `docs/index.html` 的端点路径笔误（按文档调用会 404）；处理 `tool_calling.py` 正则 fallback 对嵌套 JSON 参数的解析失败。

---

## 附：与原报告的结论对话

原报告：*「doubao2api 的浏览器路线在理论上更抗协议改版，但它的实现方式把这个优势完全抵销了，而且我实测发现三个致命 bug。」*

本文回应：三个致命 bug 已全部闭环，且被回归测试钉死。「实现抵销架构优势」的部分——单 Page 并发模型——完成了第一步止血（切号不杀流），但架构本身未重写，原报告对并发能力、内存、无人值守的批评继续有效。这个项目现在的诚实定位是：**一台开着浏览器窗口的机器上，可用的豆包 OpenAI 兼容桥接器**；比原报告时点多了「能接 Agent、能扛长思考、验证码能逃逸」三件实事。
